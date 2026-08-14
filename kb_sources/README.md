# KB 源文档（25 篇）

QRA 知识库 `data/kb_fts.db` 的可重建源。灌库入口：`scripts/load_kb_docs.py kb_sources/cheetah/*.md kb_sources/methodology/*.md`。

| 目录 | 来源 | 篇数 |
|------|------|------|
| `cheetah/` | 猎豹量化模型演进文档（~/hermes_output/quant/，2026-08-14 快照） | 12 |
| `methodology/` | 研究方法论家族（~/hermes_output/methodology/ + data/qra_kb/） | 13 |

## 重建命令

```bash
.venv-v7/bin/python scripts/load_kb_docs.py kb_sources/cheetah/*.md kb_sources/methodology/*.md
```

（doc_name=文件名；重灌语义：同 doc_name 旧切片先删再插，索引显式 rebuild。）

## ⚠️ 开源前脱敏复查（W12 义务）

- 这些文档含个人投资方法论与模型迭代记录，**开源前必须逐篇复查**：
  个人信息、持仓、金额、账户、平台账号等一律脱敏或移除后再发布。
- 未复查通过前，kb_sources/ 内容不随开源仓库发布。
- 猎豹模型代码本身（cheetah_*.py）不在此目录，开源范围另议。
