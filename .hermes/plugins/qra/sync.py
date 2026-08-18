"""QRA 工具 qra_sync：agent 对话内多上游 vendor 同步（D009 §7）。

复用 src/qra/vendor_sync.py 单一核心（按文件路径加载，避免与
hermes 插件加载机制的包名冲突——核心是纯 stdlib 模块，无依赖）。
用户说"同步 hermes / prime / dsh""更新上游"时 agent 调用本工具。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_core():
    """按文件路径加载 src/qra/vendor_sync.py，命名独立防冲突。

    必须在 exec_module 前把模块注册进 sys.modules：vendor_sync.py 顶层
    的 @dataclass（UpstreamConfig）在 Python 3.9 的 dataclasses 实现里会
    执行 sys.modules.get(cls.__module__).__dict__——模块未注册返回 None
    直接 AttributeError（'NoneType' object has no attribute '__dict__'）。
    """
    import sys

    src = Path(__file__).resolve().parents[3] / "src" / "qra" / "vendor_sync.py"
    name = "_qra_vendor_sync_core"
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass 装饰器需要模块在 sys.modules 中
    spec.loader.exec_module(mod)
    return mod


def qra_sync(args: dict, **_kw) -> str:
    """多上游 vendor 同步（hermes 自动合并+门禁；prime/dsh 钉针+diff 报告）。

    Args:
        args: {"upstream": "hermes"（默认）| "prime" | "dsh",
               "mode": "full"（默认）| "apply" | "report"}。
              hermes full=完整同步并跑六层门禁（约 5-7 分钟，门禁失败自动
              回滚旧钉针）；report=只拉取核对不落地。
              prime/dsh=本质源：推进 vendor 钉针 + diff 报告，不自动合并到
              QRA 代码；上游动了嫁接面文件 → needs_regraft=True 待人工重移植。
    """
    d = args if isinstance(args, dict) else {}
    mode = str(d.get("mode", "full")).strip().lower()
    upstream = str(d.get("upstream", "hermes")).strip().lower()
    if mode not in ("full", "apply", "report"):
        mode = "full"
    if upstream not in ("hermes", "prime", "dsh"):
        upstream = "hermes"
    try:
        core = _load_core()
        r = core.sync(mode, upstream)
    except Exception as e:  # 任何异常都不该让 agent 崩（插件工具兜底约定）
        return json.dumps({"error": f"同步执行异常：{type(e).__name__}: {e}"},
                          ensure_ascii=False)

    if r.get("already_latest"):
        return json.dumps({
            "synced": False,
            "message": f"{upstream} 已是最新，无需同步（当前钉针 {r['new_pin'][:8]}）",
            "pin": r["new_pin"][:8],
            "upstream": upstream,
        }, ensure_ascii=False)
    if r.get("error"):
        return json.dumps({
            "synced": False,
            "error": r["error"],
            "rolled_back": bool(r.get("rolled_back")),
            "old_pin": (r.get("old_pin") or "")[:8],
            "upstream": upstream,
        }, ensure_ascii=False)
    payload = {
        "synced": True,
        "upstream": upstream,
        "old_pin": r["old_pin"][:8],
        "new_pin": r["new_pin"][:8],
        "commits": r.get("commits"),
        "commits_summary": r.get("commits_summary", []),
        "changed_files": r.get("changed_files"),
        "graft_hits": r.get("graft_hits", []),
        "merged": bool(r.get("merged")),
    }
    if r.get("needs_regraft"):
        payload["needs_regraft"] = True
        payload["message"] = (
            "钉针已推进但上游动了嫁接面文件：QRA 侧代码未动，"
            "待人工 diff + 重移植后再跑门禁闭环"
        )
        return json.dumps(payload, ensure_ascii=False)
    if upstream in ("prime", "dsh"):
        payload["message"] = (
            "本质源钉针已推进（diff 报告见 commits_summary）；"
            "嫁接面零命中，QRA 侧代码无需动"
        )
        return json.dumps(payload, ensure_ascii=False)
    if mode == "full":
        payload["gate_rc"] = r.get("gate_rc")
        payload["message"] = (
            "同步完成，六层回归门禁全绿（编译/单测/真实API问答×2/交互pty/命令pty/持久内核38）"
            if r.get("gate_rc") == 0
            else f"门禁失败 rc={r.get('gate_rc')}，已自动回滚旧钉针"
        )
    else:
        payload["message"] = "预检完成：嫁接面零命中，未落地（要同步请用 mode=full）"
    return json.dumps(payload, ensure_ascii=False)
