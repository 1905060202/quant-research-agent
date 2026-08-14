"""QRA 工具 qra_sync：agent 对话内同步 hermes 上游（D009）。

复用 src/qra/vendor_sync.py 单一核心（按文件路径加载，避免与
hermes 插件加载机制的包名冲突——核心是纯 stdlib 模块，无依赖）。
用户说"同步 hermes""更新上游"时 agent 调用本工具。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_core():
    """按文件路径加载 src/qra/vendor_sync.py，命名独立防冲突。"""
    src = Path(__file__).resolve().parents[3] / "src" / "qra" / "vendor_sync.py"
    spec = importlib.util.spec_from_file_location("_qra_vendor_sync_core", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def qra_sync(args: dict, **_kw) -> str:
    """同步 hermes 上游到 QRA vendor（嫁接面核对 + ff-only 快进 + 回归门禁）。

    Args:
        args: {"mode": "full"（默认）| "report"}。full=完整同步并跑四层门禁
              （约 5 分钟，门禁失败自动回滚旧钉针）；report=只拉取核对不落地。
    """
    mode = str(args.get("mode", "full")).strip().lower() if isinstance(args, dict) else "full"
    if mode not in ("full", "report"):
        mode = "full"
    try:
        core = _load_core()
        r = core.sync(mode)
    except Exception as e:  # 任何异常都不该让 agent 崩（插件工具兜底约定）
        return json.dumps({"error": f"同步执行异常：{type(e).__name__}: {e}"},
                          ensure_ascii=False)

    if r.get("already_latest"):
        return json.dumps({
            "synced": False,
            "message": f"已是最新，无需同步（当前钉针 {r['new_pin'][:8]}）",
            "pin": r["new_pin"][:8],
        }, ensure_ascii=False)
    if r.get("error"):
        return json.dumps({
            "synced": False,
            "error": r["error"],
            "rolled_back": bool(r.get("rolled_back")),
            "old_pin": (r.get("old_pin") or "")[:8],
        }, ensure_ascii=False)
    payload = {
        "synced": True,
        "old_pin": r["old_pin"][:8],
        "new_pin": r["new_pin"][:8],
        "commits": r.get("commits"),
        "commits_summary": r.get("commits_summary", []),
        "changed_files": r.get("changed_files"),
        "graft_hits": r.get("graft_hits", []),
        "merged": bool(r.get("merged")),
    }
    if r["mode"] == "full":
        payload["gate_rc"] = r.get("gate_rc")
        payload["message"] = (
            "同步完成，四层回归门禁全绿（编译/单测9项/真实API问答×2/交互×2）"
            if r.get("gate_rc") == 0
            else f"门禁失败 rc={r.get('gate_rc')}，已自动回滚旧钉针"
        )
    else:
        payload["message"] = "预检完成：嫁接面零命中，未落地（要同步请用 mode=full）"
    return json.dumps(payload, ensure_ascii=False)
