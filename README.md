# QRA · Quant Research Agent

**量化研究智能体**：基于 [hermes-agent](https://github.com/NousResearch/hermes-agent) 底座的定制层——把"行情数据 → 信号 → 方法论文档 → 日报 + 验证卡"做成一条可评测、可复算的自动流水线。

## 能力

| 层 | 实现 | 说明 |
|---|---|---|
| 工具 | `qra_quote` / `qra_signal` / `qra_kb_fts` | 新浪实时行情、猎豹信号摘要（诚实标注数据新鲜度）、方法论文档 FTS 检索 |
| 记忆 | `qra_memory`（Mem0 式 ADD 协议） | 三重去重（会话/精确/近似≥0.85）+ 价格锚放宽 + 叙事链；显式检索回忆 |
| 验证 | `qra_verify`（claims 账本） | 数据报价直连复核 / 文件存在 / 文件包含 / 数值范围 4 检查器 + pre_verify 强制续跑 |
| 日报 | `qra_daily` 技能 | 信号→行情→方法论→记忆→撰写→验证卡 6 步，3 条可验证预测 |
| 评审门 | `qra_refine` | prime-agent 准入门移植（官方兼容钩，vendor 零改动），拒绝→零写入 |
| 评测 | `bench/` | 30 题×3 域，动态 gold + 机械评分 + 幻觉双口径，可任意时刻复算 |

## 快速开始

```bash
# 1. 底座（pin 17d6a7d，见 vendor/README 或上游仓库）
git clone https://github.com/NousResearch/hermes-agent.git vendor/hermes-agent
cd vendor/hermes-agent && git checkout 17d6a7d && cd ../..

# 2. 依赖 + 底座安装（uv 环境）
uv venv .venv-v7 --python 3.12
uv pip install -e vendor/hermes-agent --no-deps --python .venv-v7/bin/python

# 3. 凭据：环境变量注入（零落盘，HANDOFF 文件已 gitignore）
#    ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL

# 4. 运行单发问答
./scripts/run_qra.sh -z "查一下贵州茅台现价"

# 5. 跑评测（30 题，动态 gold 自动重采行情）
.venv-v7/bin/python bench/run_qra.py
.venv-v7/bin/python bench/scorer.py --results bench/results/qra
```

## 评测结果（2026-08-14 复算）

| 系统 | 30 题 | 幻觉率 | 时延 |
|---|---|---|---|
| QRA | 100% | 0/30 | 41s |
| Claude Code（同题对照） | 100% | 0/30 | 79s |

长程记忆：跨会话写入→回忆→判重 3/3。bench v1.0 量的是基础胜任（回归验收），
能力上限对比需难题层——诚实边界见 `docs/W6_QRA-Bench评测完成记录_2026-08-14.md`。

## 架构

融合架构：**Hermes 骨架 + grafts**。所有 QRA 能力走官方协议挂接（插件 / 记忆 provider /
spawn 兼容钩），vendor 核心零改动，上游更新无损跟进。铁律：嫁接不得修改核心循环。

```
hermes-agent（vendor/，独立 git）
  ├── .hermes/config.yaml          模型/插件/记忆 provider 配置
  ├── .hermes/plugins/qra/         quote / signal / kb_fts
  ├── .hermes/plugins/qra_memory/  Mem0 式记忆 provider
  ├── .hermes/plugins/qra_verify/  claims 账本 + 确定性检查器
  └── .hermes/plugins/qra_refine/  评审门（准入门三段流水线移植）
bench/          评测（30 题 + 长程记忆 + 双系统对比）
scripts/        运行入口 / KB 灌库 / 成本周检
src/qra/        JD 学习轨道：LangGraph agent + Chroma 向量检索 + AutoGen 三人小组
docs/           拍板记录 / 周完成记录 / ADR 决策 / 论文笔记
kb_sources/     方法论文档源（已脱敏，两 KB 可重建）
```

## 目录

- **决策记录**：`docs/decisions/`（ADR，D001 底座翻盘起）
- **指标追踪**：`reports/metrics.md`（成功率/时延/调试记录/源码吸收）
- **成本周检**：`scripts/check_cost.py`（¥350/周阈值，连续 2 周超 → 停机复查）
- **学习轨道**：LangGraph 图式编排 vs AutoGen 对话式编排对比实验（`src/qra/agents/`）

## 许可证

MIT（见 LICENSE）。`vendor/hermes-agent/` 为上游仓库副本（NousResearch，
其许可证以该仓库为准），不属于本仓发布物；本仓核心为 `.hermes/plugins/`、
`bench/`、`scripts/`、`src/`、`docs/`、`kb_sources/`。

## 安全

- 零凭据铁律：全部凭据走环境变量，任何明文 key 不入库（git 历史亦已核验）
- `HANDOFF_新session必读.md` 含敏感信息，gitignored
- 开源前脱敏复查记录：kb_sources 14 处人称替换，两 KB 同步重灌
