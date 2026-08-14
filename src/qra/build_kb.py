"""KB 灌库：kb_sources 方法论文档 → 切块 → 词汇表 fit → Chroma 向量库。

用法（在 src/qra/ 下运行）：
    .venv/bin/python build_kb.py

切分复用主项目 load_kb_docs.py 的已验证逻辑：标题分节，600 字，段落打包。
词汇表：全语料特征频率统计，df>=2 且取 top 10000，存 data/kb_vocab.json。
灌库后跑真实探针验证（不是 count 计数——count 数的是行数不是命中）。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import chromadb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.kb import NGramTFEmbedding, _tokenize, _CHROMA_DIR, _VOCAB, _DATA  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
SOURCES = PROJECT / "kb_sources"
_MAX_CHUNK = 600
_MAX_VOCAB = 10000
_MIN_DF = 2


def _split_headings(text: str) -> list[str]:
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


def _chunk(text: str) -> list[str]:
    chunks: list[str] = []
    for section in _split_headings(text):
        if len(section) <= _MAX_CHUNK:
            chunks.append(section)
            continue
        lines = section.splitlines()
        title = next((ln for ln in lines if re.match(r"^#{1,4}\s", ln)), "")
        body = "\n".join(ln for ln in lines if not re.match(r"^#{1,4}\s", ln))
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        cur = ""
        for p in paras:
            if len(cur) + len(p) + 1 <= _MAX_CHUNK - len(title) - 2:
                cur = f"{cur}\n\n{p}" if cur else p
            else:
                if cur:
                    chunks.append(f"{title}\n{cur}" if title else cur)
                cur = p
        if cur:
            chunks.append(f"{title}\n{cur}" if title else cur)
    return chunks


def main() -> int:
    files = sorted((SOURCES / "methodology").glob("*.md")) + sorted(
        (SOURCES / "cheetah").glob("*.md")
    )
    if not files:
        print("❌ kb_sources 下没有文档")
        return 2

    # 1. 切块
    chunks: list[tuple[str, str, int]] = []  # (source, text, index)
    for f in files:
        for i, c in enumerate(_chunk(f.read_text(encoding="utf-8"))):
            chunks.append((f.name, c, i))
    print(f"切块：{len(files)} 篇 → {len(chunks)} 块（≤{_MAX_CHUNK} 字）")

    # 2. 词汇表 fit：df>=2 且按频率截断
    df: Counter[str] = Counter()
    for _, text, _ in chunks:
        df.update(set(_tokenize(text)))
    vocab_list = [f for f, c in df.most_common(_MAX_VOCAB) if c >= _MIN_DF]
    vocab = {f: i for i, f in enumerate(vocab_list)}
    _DATA.mkdir(parents=True, exist_ok=True)
    _VOCAB.write_text(json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
    print(f"词汇表：{len(vocab)} 维（df≥{_MIN_DF}，截断 {_MAX_VOCAB}）")

    # 3. 灌库（重建：先删旧集合）
    client = chromadb.PersistentClient(path=str(_CHROMA_DIR))
    try:
        client.delete_collection("methodology")
    except Exception:
        pass
    col = client.create_collection(
        "methodology",
        embedding_function=NGramTFEmbedding(vocab),
        metadata={"hnsw:space": "cosine"},
    )
    ids = [f"c{i}" for i in range(len(chunks))]
    col.add(
        ids=ids,
        documents=[t for _, t, _ in chunks],
        metadatas=[{"source": s, "chunk": i} for s, _, i in chunks],
    )
    print(f"灌库完成：{col.count()} 块")

    # 4. 真实探针：不是 count（count 数行数不是命中），是 query 命中
    for probe in ["多假设竞争", "ES 调参", "止损"]:
        res = col.query(query_texts=[probe], n_results=1)
        doc = res["documents"][0][0] if res["documents"] and res["documents"][0] else ""
        src = res["metadatas"][0][0]["source"] if res["metadatas"] and res["metadatas"][0] else ""
        dist = res["distances"][0][0] if res["distances"] else 1.0
        print(f"探针『{probe}』→ {src}（余弦距离 {dist:.3f}）{doc[:40]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
