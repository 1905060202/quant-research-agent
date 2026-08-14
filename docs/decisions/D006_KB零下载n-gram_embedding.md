# D006：KB 向量检索用零下载 n-gram TF embedding

- 状态：accepted
- 日期：2026-08-14

## 背景

LangGraph W2 要把 bigram 字面检索升级为 Chroma 向量检索。默认 embedding（all-MiniLM）
需下载 79MB 模型：实测 hf-mirror 64KB/s（40+ 分钟）、hf 官方不通。中文模型（bge/miniLM
多语版）更大，不可行。主项目 KB 用 SQLite FTS5 trigram——是另一个体系，不在对比范围。

## 决策

自定义 EmbeddingFunction 注入 chromadb：CJK unigram+bigram、ASCII 词 token，
TF 对数加权 + L2 归一化，余弦 TopK。词汇表从语料 fit（df≥2，cap 10000）。

## 备选项

- 等待模型下载：不可接受的等待，且下载不保证成功
- 放弃向量检索留在 FTS/bigram：W2 学习目标就是向量检索机制，放弃=任务失败
- 调外部 embedding API：无可用凭据/预算

## 后果

- 机制完整（分块→向量→相似度→TopK 与真实系统一致），语义泛化弱：
  "ES 调参"检索不到"进化策略"类同义表达——已知边界，写入 kb.py 文档字符串
- embedding 是注入点：网络恢复/拿到 bge API 后换实现，检索代码零改动
- 探针实测：『多假设竞争』→ v2.1 距离 0.400 强命中；跨概念同义检索排序弱
