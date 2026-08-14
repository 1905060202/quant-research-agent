#!/usr/bin/env python3
"""重建 QRA 知识库：标准外部内容模式 + FTS5 trigram 全文索引。

用法：
    scripts/build_kb.py [db路径]     # 默认 data/kb_fts.db

行为：
- 旧库存在 → 先读出 documents 表内容，重建后原样写回（不丢数据）
- 新库 → 建空 schema
- 索引：tokenize='trigram'，中英文 ≥3 字符子串可检索（CJK 分词友好）
- content='documents' 外部内容模式

⚠️ 实测教训（SQLite 3.51 / Python 3.12）：
- FTS5 外部内容表在本环境不自动创建同步触发器（ai/ad/au 一个都没有），
  直接 INSERT documents 不会进索引，MATCH 恒为空
- 必须显式跑 INSERT INTO fts(fts) VALUES('rebuild') 灌索引
- 验证不能数 SELECT COUNT(*) FROM fts（那数的是内容行，永远等于
  documents 行数，假阳性）——要用真实 MATCH 验证

W5-8 会用猎豹方法论文档重新灌库，此脚本就是灌库入口。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "kb_fts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_name TEXT NOT NULL,
    chunk TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts_trigram USING fts5(
    chunk,
    tokenize='trigram',
    content='documents',
    content_rowid='id'
);
"""


def _cleanup_sidecars(db_path: Path) -> None:
    """清掉 WAL/SHM 残留，防止旧库边车文件污染新库。"""
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink()


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    db_path = db_path.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 迁移旧数据（只抢救 documents 本体）
    legacy: list[tuple[str, str]] = []
    if db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            legacy = conn.execute("SELECT doc_name, chunk FROM documents").fetchall()
            conn.close()
        except sqlite3.Error:
            legacy = []
        _cleanup_sidecars(db_path)
        db_path.rename(db_path.with_suffix(".db.bak"))

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        if legacy:
            conn.executemany(
                "INSERT INTO documents (doc_name, chunk) VALUES (?, ?)", legacy
            )
        # 关键：外部内容表必须显式 rebuild，索引才真正灌入
        conn.execute("INSERT INTO docs_fts_trigram(docs_fts_trigram) VALUES('rebuild')")
        conn.commit()

        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        # 真实验证：取首个 3 连中英文/数字子串做 MATCH，命中即索引生效
        # （不能直接取前 3 字符——"###"、"```" 这类特殊字符会撞 trigram 语法错）
        import re

        ok = True
        n = 0
        probe = "-"
        if total:
            for (chunk,) in conn.execute("SELECT chunk FROM documents ORDER BY id"):
                m = re.search(r"[一-鿿A-Za-z0-9]{3,}", chunk)
                if m:
                    probe = m.group(0)[:3]
                    break
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM docs_fts_trigram "
                    "WHERE docs_fts_trigram MATCH ?",
                    (probe,),
                ).fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            ok = n > 0
        print(f"知识库就绪：{db_path}")
        print(f"  documents 行数: {total}")
        print(f"  索引 MATCH 探针({probe if total else '-'}): {n if total else '-'} 条  "
              f"{'✅ 索引生效' if ok else '❌ 索引空，MATCH 全空'}")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
