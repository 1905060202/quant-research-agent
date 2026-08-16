"""QRA vendor 同步核心（D009 §7）：多上游 pin 管理。

两个同步形态：
- managed（hermes）——基底上游：ff-only 快进 + 嫁接面清单硬拦截 + 回归门禁，
  门禁失败自动回滚旧钉针。
- essence（prime / dsh）——本质源上游：vendor 克隆只是「源头材料」，QRA 侧
  的移植活在 src/qra/ 与 .hermes/plugins/ 里（如 qra_runtime 完全体移植自
  prime 的 rlm）。同步 = 推进 vendor 钉针 + diff 报告；上游动了嫁接面文件 →
  结果标记 needs_regraft=True，人工 diff、重移植到 QRA 侧、门禁跑通后闭环。
  本质源推进不打门禁（新代码未进 QRA 运行面，直到重移植完成）。

单一 Python 实现，三入口复用：
- CLI：`bin/qra sync [<upstream>] [full|apply|report]`（默认 hermes full）
- 工具：插件 qra_sync（agent 对话内可调，full 模式门禁失败自动回滚）

设计铁律（docs/decisions/D009_vendor同步流程.md）：
- vendor 内零本地修改：同步 = ff-only 快进；VERSION 是 QRA 自己的 pin 文件。
- 上游纯线性推进（钉针永远是远端分支的直系祖先），ff-only 永无冲突。
- 嫁接面清单：上游动了 QRA 依赖的文件 → managed 拒绝自动落地；essence 标记待重移植。
- "新嫁接必须入清单"：新增上游内部依赖时同步追加对应 GRAFT_PATHS。
- 每次同步后登记 docs/vendor_sync_log.md。

本模块只依赖标准库 + git CLI，不 import hermes 内部（CLI 与插件工具零路径魔法的前提）。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # src/qra/vendor_sync.py → ROOT
GATE_SCRIPT = ROOT / "scripts" / "verify_qra.sh"
GATE_TIMEOUT = 900  # 六层门禁含真实 API E2E，约 5-7 分钟


@dataclass(frozen=True)
class UpstreamConfig:
    """一条上游的同步配置。kind: managed=自动合并+门禁；essence=钉针+diff 报告。"""

    name: str
    vendor: Path
    repo: str
    branch: str
    kind: str
    graft_paths: tuple[str, ...]
    hint: str


# ---------------------------------------------------------------- 嫁接面清单
# 各上游「QRA 触碰过的内部文件」（外部依赖面）。上游改动其中任何一项 →
# managed：拒绝自动落地；essence：标记待重新移植。新增嫁接时同步追加。

# hermes（基底）：qra_console / qra_refine / qra_* 插件依赖的 hermes 内部模块。
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
    "hermes_cli/bang_shell.py",        # qra_console: ! 直达（is/parse/check/run）
    "hermes_cli/session_listing.py",   # qra_console: /resume /sessions 列表
    "hermes_cli/cli_commands_mixin.py",  # qra_console: /resume 官方序列母本
    "tools/approval.py",               # 审批面板 + session 级 yolo 开关
    "tools/terminal_tool.py",          # qra_console: set_approval_callback / guards
    "tools/todo_tool.py",              # qra_console: /resume 重建 TodoStore
    "tools/memory_tool.py",            # qra_console: get_memory_dir（/memory /export）
    "agent/agent_runtime_helpers.py",  # qra_console: switch_model 快照回滚
    "agent/conversation_compression.py",  # qra_console: /compact 序列
    "agent/memory_manager.py",         # qra_console: on_session_switch
    "agent/model_metadata.py",         # qra_console: estimate_request_tokens_rough
    "gateway/session_context.py",      # qra_console: set_current_session_id（补登）
    "hermes_constants.py",             # qra_console: 会话源/环境常量
    "agent/background_review.py",      # qra_refine: _XX_REVIEW_PROMPT 常量
    "agent/plugin_llm.py",             # qra_* 插件注册面
    "agent/plugin_stream_hooks.py",
    "hermes_cli/plugins.py",
    "hermes_cli/plugin_capabilities.py",
    "hermes_cli/plugin_index.py",
]

# prime（本质源）：完全体移植的源头（v0.7.2@83a0f9f9，2026-08-16 移植）。
# 前 3 项是 qra_runtime 三文件的直接母本；后 4 项是 comm 桥协议的宿主侧
# 实现（target "qra.host.request"、control 通道回执、comm open type-last），
# 协议语义被 QRA 移植，协议变更必须人工复核重移植。
PRIME_GRAFT_PATHS = [
    "prime-agent-runtime/src/rlm/__init__.py",                     # → qra_runtime/__init__.py
    "prime-agent-runtime/src/rlm/harness.py",                      # → qra_runtime/harness.py
    "packages/coding-agent/skills/agent-message/src/agent_message/__init__.py",  # → qra_runtime/agent_message.py
    "packages/coding-agent/src/core/kernel/index.ts",              # 内核会话管理宿主侧
    "packages/coding-agent/src/core/kernel/bootstrap.ts",          # 内核启动自检
    "packages/coding-agent/src/core/tools/ipython.ts",             # 内核桥宿主侧
    "packages/coding-agent/src/core/agent-session.ts",             # host.request 分发表
]

# dsh（本质源）：P1 精华「fail-loud 启动自检 + 配置 schema 硬校验」的
# canonical 源（借底座形态：QRA 侧实现是 config_guard.py + qra_python 启动
# 自检，非逐行移植；这些文件是 diff 复核的溯源点）。
DSH_GRAFT_PATHS = [
    "packages/boot/app-boot/src/index.ts",          # 启动自检：失败即拒
    "packages/boot/app-boot/src/invariant.ts",      # invariant 守卫模式
    "packages/settings/settings-file/src/index.ts", # 配置文件加载+校验
    "packages/settings/settings/src/types.ts",      # 配置 schema 类型
]


def _hint(name: str, repo: str) -> str:
    return (
        f"vendor/{name} 不存在。初始化（README / D009 §7）：\n"
        f"  git clone https://github.com/{repo} vendor/{name}\n"
        f"  cd vendor/{name} && git remote rename origin upstream\n"
        f"  git rev-parse HEAD > VERSION"
    )


UPSTREAMS: dict[str, UpstreamConfig] = {
    "hermes": UpstreamConfig(
        name="hermes",
        vendor=ROOT / "vendor" / "hermes-agent",
        repo="NousResearch/hermes-agent",
        branch="main",
        kind="managed",
        graft_paths=tuple(GRAFT_PATHS),
        hint=_hint("hermes-agent", "NousResearch/hermes-agent"),
    ),
    "prime": UpstreamConfig(
        name="prime",
        vendor=ROOT / "vendor" / "prime",
        repo="PrimeIntellect-ai/prime-agent",
        branch="main",
        kind="essence",
        graft_paths=tuple(PRIME_GRAFT_PATHS),
        hint=_hint("prime", "PrimeIntellect-ai/prime-agent"),
    ),
    "dsh": UpstreamConfig(
        name="dsh",
        vendor=ROOT / "vendor" / "dsh",
        repo="deepseek-ai/deepseek-harness",
        branch="master",
        kind="essence",
        graft_paths=tuple(DSH_GRAFT_PATHS),
        hint=_hint("dsh", "deepseek-ai/deepseek-harness"),
    ),
}


def _proxy_open() -> bool:
    """本地代理 127.0.0.1:7890 是否可用（大陆直连 GitHub 时断时续）。"""
    try:
        with socket.create_connection(("127.0.0.1", 7890), timeout=1):
            return True
    except OSError:
        return False


def _git(cfg: UpstreamConfig, *args: str, timeout: int = 300) -> str:
    cmd = ["git"]
    if _proxy_open():
        cmd += ["-c", "http.proxy=http://127.0.0.1:7890"]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, cwd=cfg.vendor, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git {' '.join(args)} 超时（{timeout}s）") from e
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _fetch_upstream(cfg: UpstreamConfig) -> str:
    """fetch 远端分支，返回新钉针全 SHA。瞬时 SSL 抖动重试一次。"""
    import time
    for attempt in (1, 2):
        try:
            _git(cfg, "fetch", "upstream", cfg.branch)
            return _git(cfg, "rev-parse", f"upstream/{cfg.branch}")
        except RuntimeError:
            if attempt == 2:
                raise
            time.sleep(5)
    raise AssertionError("unreachable")


def _changed_files(cfg: UpstreamConfig, old_pin: str, new_pin: str) -> list[str]:
    """钉针区间变更文件列表；浅历史缺旧钉针时回退 GitHub compare API。"""
    if subprocess.run(
        ["git", "cat-file", "-e", f"{old_pin}^{{commit}}"],
        cwd=cfg.vendor, capture_output=True,
    ).returncode == 0:
        return _git(cfg, "diff", "--name-only", old_pin, new_pin).splitlines()
    # 浅取兜底：compare API（限流时返回空 = 核对失效，宁可拒绝不可漏检）
    url = f"https://api.github.com/repos/{cfg.repo}/compare/{old_pin}...{cfg.branch}"
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


def _commits_info(cfg: UpstreamConfig, old_pin: str, new_pin: str) -> tuple[int, list[str]]:
    """(commit 数, oneline 摘要前 10 条)。旧钉针不在本地历史时返回 (0, [])。"""
    if subprocess.run(
        ["git", "cat-file", "-e", f"{old_pin}^{{commit}}"],
        cwd=cfg.vendor, capture_output=True,
    ).returncode != 0:
        return 0, []
    lines = _git(cfg, "log", "--oneline", f"{old_pin}..{new_pin}").splitlines()
    return len(lines), lines[:10]


def sync(mode: str = "full", upstream: str = "hermes") -> dict:
    """执行同步。返回结构化结果 dict。

    upstream: hermes（managed，默认）/ prime / dsh（essence）。
    mode: full（managed=拉+核对+快进+VERSION+门禁，失败自动回滚；
                essence=拉+核对+快进+VERSION，不打门禁）
          apply（拉+核对+快进+VERSION，不跑门禁）
          report（只拉+核对，不落地）
    """
    cfg = UPSTREAMS.get(upstream)
    if cfg is None:
        return {"ok": False, "upstream": upstream, "mode": mode,
                "error": f"未知上游 '{upstream}'（可选：{', '.join(UPSTREAMS)}）"}
    if mode not in ("full", "apply", "report"):
        return {"ok": False, "upstream": upstream, "mode": mode,
                "error": f"未知模式 '{mode}'（可选：full/apply/report）"}
    if cfg.kind == "essence":
        return _sync_essence(cfg, mode)
    return _sync_managed(cfg, mode)


def _sync_managed(cfg: UpstreamConfig, mode: str) -> dict:
    """managed 同步（hermes）：嫁接面命中=拒绝；full 门禁失败自动回滚旧钉针。"""
    version_file = cfg.vendor / "VERSION"
    result = {"ok": False, "upstream": cfg.name, "mode": mode,
              "old_pin": None, "new_pin": None}
    if not cfg.vendor.is_dir():
        result["error"] = cfg.hint
        return result

    try:
        # 1. fetch
        old_pin = version_file.read_text().strip() if version_file.exists() else "unknown"
        result["old_pin"] = old_pin
        new_pin = _fetch_upstream(cfg)
        result["new_pin"] = new_pin
        if old_pin == new_pin:
            result.update(ok=True, already_latest=True, graft_hits=[], gate_rc=None)
            return result

        # 2. 嫁接面核对
        changed = _changed_files(cfg, old_pin, new_pin)
        hits = [p for p in cfg.graft_paths if p in changed]
        count, summary = _commits_info(cfg, old_pin, new_pin)
        result.update(commits=count, commits_summary=summary,
                      changed_files=len(changed), graft_hits=hits)
        if hits:
            result["error"] = "上游改动了 QRA 嫁接面文件，自动同步已拒绝：" + ", ".join(hits)
            return result

        # 3-4. ff-only 快进 + VERSION
        if mode in ("full", "apply"):
            _git(cfg, "merge", "--ff-only", f"upstream/{cfg.branch}")
            version_file.write_text(new_pin + "\n")
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
            _git(cfg, "checkout", old_pin)
            version_file.write_text(old_pin + "\n")
            result["rolled_back"] = True
            result["error"] = (f"门禁失败（rc={gate_rc}），已自动回滚到 {old_pin}。"
                               "输出尾部：" + " | ".join(tail))
            return result
    result["ok"] = True
    return result


def _sync_essence(cfg: UpstreamConfig, mode: str) -> dict:
    """essence 同步（prime/dsh）：推进 vendor 钉针 + diff 报告，不自动改 QRA 侧代码。

    上游动了嫁接面文件 → needs_regraft=True：人工 diff 后重移植到
    src/qra/ 或 .hermes/plugins/，再跑门禁闭环。不打门禁——新代码未进
    QRA 运行面（vendor 只是源头材料），直到重移植完成。
    """
    version_file = cfg.vendor / "VERSION"
    result = {"ok": False, "upstream": cfg.name, "mode": mode,
              "old_pin": None, "new_pin": None}
    if not cfg.vendor.is_dir():
        result["error"] = cfg.hint
        return result

    try:
        # 1. fetch + diff 报告
        old_pin = version_file.read_text().strip() if version_file.exists() else "unknown"
        result["old_pin"] = old_pin
        new_pin = _fetch_upstream(cfg)
        result["new_pin"] = new_pin
        if old_pin == new_pin:
            result.update(ok=True, already_latest=True, graft_hits=[], needs_regraft=False)
            return result

        changed = _changed_files(cfg, old_pin, new_pin)
        hits = [p for p in cfg.graft_paths if p in changed]
        count, summary = _commits_info(cfg, old_pin, new_pin)
        result.update(commits=count, commits_summary=summary,
                      changed_files=len(changed), graft_hits=hits,
                      needs_regraft=bool(hits))

        # 2. 钉针推进（vendor 克隆只是源头材料，快进即可）
        if mode in ("full", "apply"):
            _git(cfg, "merge", "--ff-only", f"upstream/{cfg.branch}")
            landed = _git(cfg, "rev-parse", "HEAD")
            version_file.write_text(landed + "\n")
            result["merged"] = True
            result["new_pin"] = landed
        result["ok"] = True
        return result
    except RuntimeError as e:
        result["error"] = str(e)
        return result


# ---------------------------------------------------------------- CLI 入口
_USAGE = """QRA 命令：qra sync —— 多上游 vendor 同步（D009 §7）

用法：
  qra sync                            # hermes 完整同步（默认）：拉取→嫁接面核对→快进→VERSION→门禁
  qra sync <upstream> [<mode>]        # upstream: hermes | prime | dsh
                                      # mode: full（默认）| apply（跳过门禁）| report（预检不落地）

- hermes（managed）   ：嫁接面命中=拒绝自动落地；full 门禁失败自动回滚旧钉针。
- prime / dsh（essence）：只推进 vendor 钉针 + diff 报告，不自动合并到 QRA 代码；
  上游动了嫁接面文件 → 标记「待重新移植」，人工重移植后再跑门禁闭环。
"""


def main(argv: list[str]) -> int:
    if any(a in ("-h", "--help", "help") for a in argv):
        print(_USAGE)
        return 0
    upstream, mode, rest = "hermes", "full", list(argv)
    if rest and rest[0] in UPSTREAMS:
        upstream = rest.pop(0)
        if rest and rest[0] in ("full", "apply", "report"):
            mode = rest.pop(0)
    elif rest and rest[0] in ("full", "apply", "report"):
        mode = rest.pop(0)
    elif rest:
        print(f"未知上游或模式 '{rest[0]}'。\n{_USAGE}")
        return 1
    if rest:
        print(f"多余参数：{' '.join(rest)}\n{_USAGE}")
        return 1

    r = sync(mode, upstream)
    if r.get("error") and not r.get("rolled_back"):
        print("❌", r["error"])
        return 2 if r.get("graft_hits") else 1
    if r.get("already_latest"):
        print(f"✅ {upstream} 已是最新：{r['new_pin'][:8]}")
        return 0
    print(f"{upstream} 旧钉针: {r['old_pin'][:8] if r['old_pin'] else '?'}  →  新钉针: {r['new_pin'][:8]}")
    print(f"变更: {r.get('commits', '?')} commits / {r.get('changed_files', '?')} 文件")
    for line in (r.get("commits_summary") or []):
        print("  " + line)
    print(f"嫁接面: {'零命中 ✓' if not r.get('graft_hits') else '⚠️ ' + ', '.join(r['graft_hits'])}")
    if mode == "report":
        print("（report 模式：未落地。执行 qra sync <upstream> 推进钉针）")
        return 0
    if r.get("merged"):
        print("✅ ff-only 快进 + VERSION 已更新")
    if r.get("needs_regraft"):
        print("⚠️ 上游动了嫁接面文件：QRA 侧代码未动，待人工重移植后再跑门禁闭环")
    if upstream in ("prime", "dsh"):
        print("（essence 源：QRA 侧代码未动——重移植完成前不会影响运行面）")
        return 0
    if mode == "full":
        rc = r.get("gate_rc")
        if rc == 0:
            print("✅ 六层回归门禁全绿")
            return 0
        print(f"❌ 门禁失败（rc={rc}）——已自动回滚到旧钉针")
        for line in (r.get("gate_tail") or []):
            print("  " + line)
        return 3
    print(f"完成（{mode} 模式）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
