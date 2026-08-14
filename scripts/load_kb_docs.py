#!/usr/bin/env python3
"""猎豹量化文档灌入 QRA 知识库（documents 外部内容表 + trigram 索引重建）。

用法：
    scripts/load_kb_docs.py <md文件> [md文件...]   # 按 doc_name 替换/追加
    scripts/load_kb_docs.py --list                 # 列出当前 documents 的 doc_name

行为：
- doc_name = 文件名，重灌语义：先 DELETE 该 doc_name 旧 chunks 再插新切片
- 切片：## / ### 标题分节；节超 600 字符按段落拆（标题前缀保留在首片）
- 索引：外部内容模式在本环境不自动建同步触发器（W2 实测教训），
  必须显式 INSERT INTO docs_fts_trigram(docs_fts_trigram) VALUES('rebuild')
- 验证：真实 MATCH 探针（不能 COUNT fts 数内容行——假阳性），
  每篇文档抽 trigram 安全子串 MATCH 确认命中

⚠️ 铁律：切片前确认源文档不含个人持仓/隐私（开源仓库约束，W12 发布前复查）。
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "kb_fts.db"

# trigram 安全探针：3+ 连续中英文字母/数字（"###"、"```" 会撞语法错）
_PROBE_RE = re.compile(r"[一-鿿A-Za-z0-9]{3,}")
_MAX_CHUNK = 600


def _split_headings(text: str) -> list[str]:
    """按 #{1,4} 标题行分节，返回节列表（含标题行）。"""
    lines = text.splitlines()
    sections: list[str] = []
    cur: list[str] = []
    for ln in lines:
        if re.match(r"^#{1,4}\s", ln) and cur:
            sections.append("\n".join(cur).strip())
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        sections.append("\n".join(cur).strip())
    return [s for s in sections if s]


def _pack_paragraphs(body: str, max_chars: int) -> list[str]:
    """段落打包，每片 ≤ max_chars，不切断段落。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    pieces: list[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) + 1 <= max_chars:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                pieces.append(cur)
            cur = p
    if cur:
        pieces.append(cur)
    return pieces


def _chunk(text: str, max_chars: int = _MAX_CHUNK) -> list[str]:
    """分节 → 超长节按段落拆，标题保留在首片。"""
    chunks: list[str] = []
    for section in _split_headings(text):
        if len(section) <= max_chars:
            chunks.append(section)
            continue
        lines = section.splitlines()
        title = next((ln for ln in lines if re.match(r"^#{1,4}\s", ln)), "")
        body = "\n".join(ln for ln in lines if not re.match(r"^#{1,4}\s", ln))
        pieces = _pack_paragraphs(body, max_chars - len(title) - 2)
        for i, p in enumerate(pieces):
            chunks.append(f"{title}\n{p}" if i == 0 and title else p)
    return chunks


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT doc_name, COUNT(*) FROM documents GROUP BY doc_name "
                "ORDER BY doc_name"
            ).fetchall()
            for name, n in rows:
                print(f"{n:4d}  {name}")
            print(f"共 {len(rows)} 篇文档")
            return 0
        finally:
            conn.close()

    files = [Path(a).expanduser().resolve() for a in sys.argv[1:]]
    if not files:
        print(__doc__)
        return 2
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        print(f"❌ 文件不存在：{missing}")
        return 2

    conn = sqlite3.connect(DB_PATH)
    try:
        for f in files:
            name = f.name
            conn.execute("DELETE FROM documents WHERE doc_name=?", (name,))
            chunks = _chunk(f.read_text(encoding="utf-8", errors="replace"))
            conn.executemany(
                "INSERT INTO documents (doc_name, chunk) VALUES (?, ?)",
                [(name, c) for c in chunks],
            )
            print(f"灌入 {name}: {len(chunks)} chunks")
        # 外部内容表显式重建索引（含 DELETE 同步）
        conn.execute(
            "INSERT INTO docs_fts_trigram(docs_fts_trigram) VALUES('rebuild')"
        )
        conn.commit()

        # 验证：每篇文档抽 trigram 安全子串 MATCH
        fail = 0
        for f in files:
            chunks = [r[0] for r in conn.execute(
                "SELECT chunk FROM documents WHERE doc_name=? ORDER BY id LIMIT 8",
                (f.name,))]
            probe = "-"
            for c in chunks:
                m = _PROBE_RE.search(c)
                if m:
                    probe = m.group(0)[:3]
                    break
            n = conn.execute(
                "SELECT COUNT(*) FROM docs_fts_trigram "
                "WHERE docs_fts_trigram MATCH ?", (probe,)).fetchone()[0]
            ok = n > 0
            fail += 0 if ok else 1
            print(f"  探针 {f.name} ({probe}): {n} 命中 {'✅' if ok else '❌'}")

        # 术语级验证：猎豹独有词必须能搜到新文档。
        # ⚠️ 含连字符/空格的词要加双引号做短语查询，裸写是 FTS5 运算符
        for term in ("数据泄露", '"walk-forward"', "regime", "雪崩"):
            n = conn.execute(
                "SELECT COUNT(*) FROM docs_fts_trigram "
                "WHERE docs_fts_trigram MATCH ?", (term,)).fetchone()[0]
            print(f"  术语 {term}: {n} 命中")
        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        print(f"documents 总行数: {total} · 探针失败: {fail}")
        return 1 if fail else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
