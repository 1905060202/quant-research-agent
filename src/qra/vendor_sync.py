"""QRA vendor 同步核心（D009）：hermes 上游 pin 更新。

单一 Python 实现，双入口复用：
- CLI：`bin/qra sync [full|apply|report]`（QRA 原生命令，默认 full）
- 工具：插件 qra_sync（agent 对话内可调，full 模式门禁失败自动回滚）

设计铁律（docs/decisions/D009_vendor同步流程.md）：
- vendor 内零本地修改：同步 = ff-only 快进；VERSION 是 QRA 自己的 pin 文件。
- 上游纯线性推进（已验证钉针永远是 main 的直系祖先），ff-only 永无冲突。
- 嫁接面清单 GRAFT_PATHS：上游动了 QRA 依赖的 hermes 内部文件 → 拒绝自动落地。
- "新嫁接必须入清单"：新增 hermes 内部依赖时同步追加 GRAFT_PATHS。
- 每次同步后登记 docs/vendor_sync_log.md。

本模块只依赖标准库 + git CLI，不 import hermes 内部（CLI 与插件工具零路径魔法的前提）。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # src/qra/vendor_sync.py → ROOT
VENDOR = ROOT / "vendor" / "hermes-agent"
VERSION_FILE = VENDOR / "VERSION"
UPSTREAM_REPO = "NousResearch/hermes-agent"
GATE_SCRIPT = ROOT / "scripts" / "verify_qra.sh"
GATE_TIMEOUT = 900  # 四层门禁含真实 API E2E，约 5-7 分钟

# ---------------------------------------------------------------- 嫁接面清单
# QRA 触碰过的 hermes 内部文件（外部依赖面）。上游改动其中任何一项 →
# 拒绝自动落地，必须人工看 diff 并适配 QRA 侧代码（D009 §2）。
# 新增嫁接（src/qra 或 .hermes/plugins 依赖 hermes 内部模块）时同步追加。
GRAFT_PATHS = [
    "run_agent.py",                    # qra_console: AIAgent 构造+回调
    "hermes_state.py",                 # qra_console: SessionDB
    "hermes_cli/config.py",            # console 模型/provider 解析
    "hermes_cli/models.py",            # detect_provider_for_model / 前缀剥离
    "hermes_cli/model_normalize.py",   # normalize_model_for_provider
    "hermes_cli/model_switch.py",
    "hermes_cli/fallback_config.py",
    "hermes_cli/mcp_startup.py",
    "hermes_cli/runtime_provider.py",
    "hermes_cli/tools_config.py",
    "hermes_cli/oneshot.py",           # _normalize_toolsets / 最小参数集先例
    "hermes_cli/main.py",              # -z 启动守卫等 CLI 路径
    "cli.py",                          # CLI 入口（run_qra.sh 走这里）
    "hermes_cli/auth.py",
    "tools/approval.py",               # 审批面板（交互模式工具确认）
    "agent/background_review.py",      # qra_refine: _XX_REVIEW_PROMPT 常量
    "agent/plugin_llm.py",             # qra_* 插件注册面
    "agent/plugin_stream_hooks.py",
    "hermes_cli/plugins.py",
    "hermes_cli/plugin_capabilities.py",
    "hermes_cli/plugin_index.py",
]

BOOTSTRAP_HINT = (
    "vendor/hermes-agent 不存在。初始化（README/D009）：\n"
    f"  git clone --branch main https://github.com/{UPSTREAM_REPO} vendor/hermes-agent\n"
    "  cd vendor/hermes-agent && git remote rename origin upstream\n"
    "  git rev-parse upstream/main > VERSION"
)


def _proxy_open() -> bool:
    """本地代理 127.0.0.1:7890 是否可用（大陆直连 GitHub 时断时续）。"""
    try:
        with socket.create_connection(("127.0.0.1", 7890), timeout=1):
            return True
    except OSError:
        return False


def _git(*args: str, timeout: int = 300) -> str:
    cmd = ["git"]
    if _proxy_open():
        cmd += ["-c", "http.proxy=http://127.0.0.1:7890"]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, cwd=VENDOR, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git {' '.join(args)} 超时（{timeout}s）") from e
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _fetch_upstream() -> str:
    """fetch upstream/main，返回新钉针全 SHA。瞬时 SSL 抖动重试一次。"""
    import time
    for attempt in (1, 2):
        try:
            _git("fetch", "upstream", "main")
            return _git("rev-parse", "upstream/main")
        except RuntimeError:
            if attempt == 2:
                raise
            time.sleep(5)
    raise AssertionError("unreachable")


def _changed_files(old_pin: str, new_pin: str) -> list[str]:
    """钉针区间变更文件列表；浅历史缺旧钉针时回退 GitHub compare API。"""
    if subprocess.run(
        ["git", "cat-file", "-e", f"{old_pin}^{{commit}}"],
        cwd=VENDOR, capture_output=True,
    ).returncode == 0:
        return _git("diff", "--name-only", old_pin, new_pin).splitlines()
    # 浅取兜底：compare API（限流时返回空 = 核对失效，宁可拒绝不可漏检）
    url = f"https://api.github.com/repos/{UPSTREAM_REPO}/compare/{old_pin}...main"
    handlers = []
    if _proxy_open():
        handlers.append(urllib.request.ProxyHandler({"https": "http://127.0.0.1:7890"}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        raise RuntimeError(f"本地历史缺旧钉针且 GitHub compare API 不可用（{e}）"
                           f"——无法核对嫁接面，拒绝同步。") from e
    return [f["filename"] for f in data.get("files", [])]


def _commits_info(old_pin: str, new_pin: str) -> tuple[int, list[str]]:
    """(commit 数, oneline 摘要前 10 条)。旧钉针不在本地历史时返回 (0, [])。"""
    if subprocess.run(
        ["git", "cat-file", "-e", f"{old_pin}^{{commit}}"],
        cwd=VENDOR, capture_output=True,
    ).returncode != 0:
        return 0, []
    lines = _git("log", "--oneline", f"{old_pin}..{new_pin}").splitlines()
    return len(lines), lines[:10]


def sync(mode: str = "full") -> dict:
    """执行同步。返回结构化结果 dict。

    mode: full（拉+核对+快进+VERSION+门禁，失败自动回滚）
          apply（拉+核对+快进+VERSION，不跑门禁）
          report（只拉+核对，不落地）
    """
    result = {"ok": False, "mode": mode, "old_pin": None, "new_pin": None}
    if not VENDOR.is_dir():
        result["error"] = BOOTSTRAP_HINT
        return result

    try:
        # 1. fetch
        old_pin = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "unknown"
        result["old_pin"] = old_pin
        new_pin = _fetch_upstream()
        result["new_pin"] = new_pin
        if old_pin == new_pin:
            result.update(ok=True, already_latest=True, graft_hits=[], gate_rc=None)
            return result

        # 2. 嫁接面核对
        changed = _changed_files(old_pin, new_pin)
        hits = [p for p in GRAFT_PATHS if p in changed]
        count, summary = _commits_info(old_pin, new_pin)
        result.update(commits=count, commits_summary=summary,
                      changed_files=len(changed), graft_hits=hits)
        if hits:
            result["error"] = "上游改动了 QRA 嫁接面文件，自动同步已拒绝：" + ", ".join(hits)
            return result

        # 3-4. ff-only 快进 + VERSION
        if mode in ("full", "apply"):
            _git("merge", "--ff-only", "upstream/main")
            VERSION_FILE.write_text(new_pin + "\n")
            result["merged"] = True
    except RuntimeError as e:
        # git/网络瞬时失败（GitHub 间歇性 SSL 抖动常见）：干净报错，永不裸抛
        result["error"] = str(e)
        return result

    # 5. 门禁（full 失败自动回滚到旧钉针：保证系统永远停在已知良好状态）
    if mode == "full":
        try:
            r = subprocess.run([str(GATE_SCRIPT)], cwd=ROOT, capture_output=True,
                               text=True, timeout=GATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            r = None
        gate_rc = -1 if r is None else r.returncode
        tail = ("" if r is None else r.stdout.strip().splitlines()[-6:])
        result["gate_rc"] = gate_rc
        result["gate_tail"] = tail
        if gate_rc != 0:
            _git("checkout", old_pin)
            VERSION_FILE.write_text(old_pin + "\n")
            result["rolled_back"] = True
            result["error"] = (f"门禁失败（rc={gate_rc}），已自动回滚到 {old_pin}。"
                               "输出尾部：" + " | ".join(tail))
            return result
    result["ok"] = True
    return result


# ---------------------------------------------------------------- CLI 入口
_USAGE = """QRA 命令：qra sync —— 同步 hermes 上游（D009）

用法：
  qra sync            # 完整同步：拉取→嫁接面核对→快进→VERSION→四层门禁（默认）
  qra sync report     # 只拉取+核对，不落地（预检）
  qra sync apply      # 拉取+核对+快进+VERSION，跳过门禁（急用）
"""


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    mode = argv[0] if argv else "full"
    if mode not in ("full", "apply", "report"):
        print(f"未知模式 {mode}。\n{_USAGE}")
        return 1

    r = sync(mode)
    if r.get("error") and not r.get("rolled_back"):
        print("❌", r["error"])
        return 2 if r.get("graft_hits") else 1
    if r.get("already_latest"):
        print(f"✅ 已是最新：{r['new_pin'][:8]}")
        return 0
    print(f"旧钉针: {r['old_pin'][:8] if r['old_pin'] else '?'}  →  新钉针: {r['new_pin'][:8]}")
    print(f"变更: {r.get('commits', '?')} commits / {r.get('changed_files', '?')} 文件")
    for line in (r.get("commits_summary") or []):
        print("  " + line)
    print(f"嫁接面: {'零命中 ✓' if not r.get('graft_hits') else '⚠️ ' + ', '.join(r['graft_hits'])}")
    if mode == "report":
        print("（report 模式：未落地。执行 qra sync 完整同步）")
        return 0
    if r.get("merged"):
        print("✅ ff-only 快进 + VERSION 已更新")
    if mode == "full":
        rc = r.get("gate_rc")
        if rc == 0:
            print("✅ 四层回归门禁全绿")
            return 0
        print(f"❌ 门禁失败（rc={rc}）——已自动回滚到旧钉针")
        for line in (r.get("gate_tail") or []):
            print("  " + line)
        return 3
    print(f"完成（{mode} 模式）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
