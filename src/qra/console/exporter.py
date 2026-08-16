"""/export 落盘：会话导出 md / jsonl（HERMES_HOME/exports/）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def export_dir() -> Path:
    try:
        from tools.memory_tool import get_memory_dir
        d = get_memory_dir().parent / "exports"
    except Exception:
        d = Path.home() / ".hermes" / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def export_session(db, session_id: str, fmt: str = "md",
                   out_dir: Path | None = None) -> Path:
    """导出会话为 md / jsonl 文件，返回落盘路径。

    消息数受 SessionDB.resolved_max_export_messages 守卫（vendor 同款上限）。
    """
    meta = db.get_session(session_id) or {}
    max_msgs = getattr(db, "resolved_max_export_messages", None)
    msgs = (db.get_messages(session_id, limit=max_msgs)
            if max_msgs else db.get_messages(session_id))
    d = out_dir or export_dir()
    fmt = fmt.lower()
    path = d / f"session_{session_id}.{fmt}"

    if fmt == "jsonl":
        with open(path, "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")
        return path

    # md（默认）
    title = meta.get("title") or "(未命名)"
    lines = [
        f"# 会话导出 · {title}",
        "",
        f"- 会话 ID：`{session_id}`",
        f"- 模型：{meta.get('model') or '—'}",
    ]
    if meta.get("started_at"):
        lines.append(f"- 开始：{_ts(meta.get('started_at'))}")
    if meta.get("ended_at"):
        lines.append(f"- 结束：{_ts(meta.get('ended_at'))}")
    lines += ["", "---", ""]
    for m in msgs:
        role = m.get("role") or "?"
        content = m.get("content")
        if isinstance(content, list):
            content = "\n\n".join(
                str(b.get("text") or "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
        lines += [f"## {role}", "", str(content or ""), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
