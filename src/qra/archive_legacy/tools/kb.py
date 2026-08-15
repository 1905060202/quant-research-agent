"""知识库检索工具：Chroma 向量检索（W2 升级：bigram 字面检索 → 语义向量检索）。

Embedding：字符 n-gram TF 向量（零下载离线方案）。
- 网络受限（hf-mirror 64KB/s、官方不通），MiniLM/bge 模型下载不可行
- 用自定义 EmbeddingFunction 注入：CJK unigram+bigram、ASCII 词 token，
  TF 加权 + L2 归一化，余弦相似度检索
- 机制与真实向量检索完全一致（分块→向量→相似度→TopK），
  语义能力弱于 transformer embedding——生产环境应换 bge-m3 类 API/本地模型
  （embedding 是注入点，换模型不动检索代码）

词汇表由 build_kb.py 从语料构建（vocab.json），缺失时报错引导重建。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import EmbeddingFunction

_DATA = Path(__file__).resolve().parents[1] / "data"
_CHROMA_DIR = _DATA / "chroma"
_VOCAB = _DATA / "kb_vocab.json"
_DOC_RE = re.compile(r"[\w一-鿿]+")


def _tokenize(text: str) -> list[str]:
    """提取特征：CJK 单字 + 连续 CJK bigram + ASCII 词（数字/英文串）"""
    feats: list[str] = []
    cjk_run: list[str] = []
    for tok in _DOC_RE.findall(text.lower()):
        for ch in tok:
            if "一" <= ch <= "鿿":
                cjk_run.append(ch)
                feats.append(ch)  # unigram
            else:
                if cjk_run:
                    feats.extend("".join(cjk_run[i : i + 2]) for i in range(len(cjk_run) - 1))
                    cjk_run = []
        if cjk_run:
            feats.extend("".join(cjk_run[i : i + 2]) for i in range(len(cjk_run) - 1))
            cjk_run = []
        if any(not ("一" <= c <= "鿿") for c in tok):  # 含 ASCII 的词整体
            feats.append(tok)
    return feats


class NGramTFEmbedding(EmbeddingFunction):
    """字符 n-gram TF 向量。vocab.json 由 build_kb.py 从语料 fit 出（带频率截断）。"""

    def __init__(self, vocab: dict[str, int]):
        self.vocab = vocab
        self.dim = len(vocab)

    def name(self) -> str:
        return "ngram-tf-zh"

    def __call__(self, input):  # chromadb 传 Documents 或 str
        docs = [input] if isinstance(input, str) else list(input)
        out = []
        for doc in docs:
            tf: dict[int, float] = {}
            for f in _tokenize(doc):
                idx = self.vocab.get(f)
                if idx is not None:
                    tf[idx] = tf.get(idx, 0.0) + 1.0
            if not tf:
                # 全 OOV：退化为等长单位向量，余弦相似度对全部文档相等（诚实行为）
                out.append([1.0 / (self.dim ** 0.5)] * self.dim)
                continue
            vec = [0.0] * self.dim
            norm = 0.0
            for idx, cnt in tf.items():
                w = 1.0 + math.log(cnt)  # TF 对数缩放
                vec[idx] = w
                norm += w * w
            norm = math.sqrt(norm)
            out.append([v / norm for v in vec])
        return out


_client = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        if not _VOCAB.is_file():
            raise RuntimeError("词汇表缺失：先运行 .venv/bin/python build_kb.py 灌库")
        vocab = json.loads(_VOCAB.read_text(encoding="utf-8"))
        _client = chromadb.PersistentClient(path=str(_CHROMA_DIR))
        _collection = _client.get_or_create_collection(
            "methodology",
            embedding_function=NGramTFEmbedding(vocab),
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def search(args: dict) -> str:
    """向量检索：query → TopK=3，返回片段 + 来源文件"""
    q = str(args["query"])
    col = _get_collection()
    if col.count() == 0:
        return "知识库为空：先运行 .venv/bin/python build_kb.py 灌库"
    res = col.query(query_texts=[q], n_results=3)
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    if not docs:
        return "没有检索到相关内容"
    parts = []
    for i, (doc, meta) in enumerate(zip(docs, metas), 1):
        src = (meta or {}).get("source", "未知")
        score = (1 - dists[i - 1]) if dists and i - 1 < len(dists) else None
        score_s = f"（相似度 {score:.2f}）" if score is not None else ""
        parts.append(f"[{i}] {src}{score_s}：{doc[:200]}")
    return "\n".join(parts)
