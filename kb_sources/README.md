# KB 源文档（91 篇）

QRA 知识库 `data/kb_fts.db` 的可重建源。灌库入口：
`scripts/load_kb_docs.py kb_sources/cheetah/*.md kb_sources/methodology/*.md kb_sources/claude_memory/*.md kb_sources/prime_memory.md`

| 目录 | 来源 | 篇数 |
|------|------|------|
| `cheetah/` | 猎豹量化模型演进文档（~/hermes_output/quant/，2026-08-14 快照） | 12 |
| `methodology/` | 研究方法论家族（~/hermes_output/methodology/ + data/qra_kb/） | 13 |
| `claude_memory/` | Claude Code 记忆（工作知识，2026-08-17 迁移，脱敏） | 65 |
| `prime_memory.md` | Prime Agent 记忆导出（工作知识，2026-08-17 迁移，脱敏） | 1 |

## 重建命令

```bash
.venv-v7/bin/python scripts/load_kb_docs.py kb_sources/cheetah/*.md kb_sources/methodology/*.md kb_sources/claude_memory/*.md kb_sources/prime_memory.md
```

（doc_name=文件名；重灌语义：同 doc_name 旧切片先删再插，索引显式 rebuild。）

## 迁移来源与脱敏（2026-08-17）

- 来源：Prime Agent `~/.prime/agent/harness/harness_state.json` 的 memory 条目（59 条工作知识）、Claude Code `~/.claude/projects/-Users-huyaning/memory/` 的 markdown（65 篇工作知识）。
- 脱敏：电话号码/邮箱/微信号/API key 已替换为占位符。
- 隐私内容（画像/八字/财务/王涵/性心理等）**已排除**，不在此目录——它们在本地 `.hermes/memories/用户画像完整版.md`（受 .gitignore 保护，永不进开源仓库）。

## ⚠️ 开源前脱敏复查（W12 义务）

- 这些文档含个人投资方法论与模型迭代记录，**开源前必须逐篇复查**：
  个人信息、持仓、金额、账户、平台账号等一律脱敏或移除后再发布。
- 未复查通过前，kb_sources/ 内容不随开源仓库发布。
- 猎豹模型代码本身（cheetah_*.py）不在此目录，开源范围另议。
