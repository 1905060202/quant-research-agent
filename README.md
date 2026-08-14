# QRA · Quant Research Agent

多智能体量化研究日报系统 —— 从猎豹信号到日报+验证卡的全自动流水线。

## 状态：🚀 已立项（2026-08-14）· 开发中

## 快速导航
- 立项书：`立项书_QRA_v0.1.md`
- 架构：`docs/architecture.md`（开发中）
- 指标：`reports/metrics.md`（W1 起记录）

## 项目结构
```
quant_research_agent/
├── 立项书_QRA_v0.1.md
├── docs/          # 架构/设计/论文精读笔记
├── src/qra/
│   ├── agents/    # Planner/Retriever/Analyst/Reflector
│   ├── rag/       # 向量库/切片/重排
│   ├── tools/     # 行情API/信号接口/文档生成
│   └── __init__.py
├── tests/
├── data/          # 知识库语料（脱敏）
└── reports/       # 指标/日报样例/验证卡
```
