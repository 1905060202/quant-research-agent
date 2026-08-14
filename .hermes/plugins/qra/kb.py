"""QRA 知识库检索工具：FTS5 trigram（中文友好）全文检索。

数据库由 scripts/build_kb.py 生成（W2 已重建为标准外部内容模式）：
- documents(id, doc_name, chunk, created_at) —— 内容本体
- docs_fts_trigram —— content='documents', tokenize='trigram'，
  支持中英文 ≥3 字符子串检索（CJK 分词友好）

检索策略：
1. 查询串 ≥3 字符 → trigram MATCH（精确短语匹配）
2. 短查询 / MATCH 无结果 → LIKE 子串兜底（trigram 对 <3 字符无能为力）
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

# 插件文件位于 <项目>/.hermes/plugins/qra/kb.py，parents[3] 即项目根
DEFAULT_KB_PATH = Path(__file__).resolve().parents[3] / "data" / "kb_fts.db"
SNIPPET_CHARS = 200


def _resolve_path(raw: str | None) -> Path:
    """路径优先级：调用参数 path > 环境变量 QRA_KB_PATH > 项目 data/kb_fts.db。"""
    if raw:
        return Path(raw).expanduser()
    env = os.environ.get("QRA_KB_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    return DEFAULT_KB_PATH


def _search_trigram(conn: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    """trigram 精确短语检索（query ≥3 字符才有效）。"""
    rows = conn.execute(
        """
        SELECT d.id, d.doc_name,
               snippet(docs_fts_trigram, 0, '<b>', '</b>', ' … ', 24) AS snip
        FROM docs_fts_trigram f
        JOIN documents d ON d.id = f.rowid
        WHERE docs_fts_trigram MATCH ?
        ORDER BY bm25(docs_fts_trigram)
        LIMIT ?
        """,
        (json.dumps(query, ensure_ascii=False).strip('"'), limit),
    ).fetchall()
    return rows


def _search_like(conn: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    """LIKE 子串兜底：短查询或 trigram 无结果时用。"""
    pattern = f"%{query}%"
    rows = conn.execute(
        """
        SELECT id, doc_name, chunk
        FROM documents
        WHERE chunk LIKE ? OR doc_name LIKE ?
        LIMIT ?
        """,
        (pattern, pattern, limit),
    ).fetchall()
    out = []
    for row_id, doc_name, chunk in rows:
        idx = chunk.find(query)
        start = max(0, idx - 60)
        snip = chunk[start : start + SNIPPET_CHARS]
        if start > 0:
            snip = " … " + snip
        out.append((row_id, doc_name, snip))
    return out


def qra_kb_fts(args: dict, **_kw) -> str:
    """QRA 知识库检索工具 handler：返回 JSON。

    Args:
        args: {"query": 检索词(必填), "limit": 5(默认), "path": 可选}
    """
    query = str(args.get("query", "") or "").strip()
    if not query:
        return json.dumps({"error": "缺少 query 参数：要检索什么？"}, ensure_ascii=False)
    try:
        limit = int(args.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 10))

    path = _resolve_path(args.get("path"))
    if not path.is_file():
        return json.dumps(
            {"error": f"知识库不存在：{path}（先跑 scripts/build_kb.py）"},
            ensure_ascii=False,
        )

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return json.dumps({"error": f"知识库打不开：{e}"}, ensure_ascii=False)

    try:
        rows = []
        method = "trigram"
        if len(query) >= 3:
            try:
                rows = _search_trigram(conn, query, limit)
            except sqlite3.Error:
                rows = []  # MATCH 语法异常或查询层错误，落兜底
        if not rows:
            method = "like_fallback"
            rows = _search_like(conn, query, limit)
        if not rows:
            return json.dumps(
                {"query": query, "hits": 0, "note": "知识库没有命中，可能超出收录范围"},
                ensure_ascii=False,
            )
        hits = [
            {"id": r[0], "doc_name": r[1], "snippet": r[2][:SNIPPET_CHARS]}
            for r in rows
        ]
        return json.dumps(
            {"query": query, "hits": len(hits), "method": method, "results": hits},
            ensure_ascii=False,
        )
    finally:
        conn.close()
