# QRA 文档地图

> 给新 session / 新 agent 的第一句话：**按角色走下表的第一行，五分钟内能开工。**
> 原则：每类信息只有一个权威源；「历史记录」可以读，但别当现状执行。

## 按场景导航

| 你是谁 / 要干嘛 | 读什么（顺序） |
|---|---|
| **新 session 接手**（第一次碰这个项目） | 仓库根 `HANDOFF_新session必读.md` → 本页 → `docs/architecture.md` → `docs/reference.md` |
| **日常开发**（改代码/加功能） | `docs/development.md`（工作流+铁律+提交协议）→ 相关 ADR → `docs/troubleshooting.md` 查已知坑 |
| **改架构 / 理解架构** | `docs/architecture.md` → `docs/decisions/`（为什么这么定）→ `docs/融合架构_v1.0_2026-08-14.md`（历史蓝图） |
| **上游同步 / 嫁接面** | `docs/vendor_sync_log.md`（账本）→ `docs/decisions/D009_*.md` → `src/qra/vendor_sync.py` GRAFT_PATHS |
| **跑评测 / 改评测** | `bench/README.md` → `docs/decisions/D003_*.md` |
| **CI / 发布** | `docs/ci.md` → `docs/development.md` 提交与推送协议 |
| **查历史脉络**（这项目怎么走到今天的） | `docs/拍板记录_2026-08-14.md` → `docs/W1_底座就位完成记录*.md` 等 W 系列 → `docs/机理研究_*.md` |

## 权威源清单（当前状态）

| 文档 | 权威程度 | 内容 |
|---|---|---|
| `docs/architecture.md` | ★ 权威 | 当前架构（分层/执行线/目录/插件面/钉针） |
| `docs/development.md` | ★ 权威 | 开发工作流、铁律、测试体系、提交推送协议、脱敏红线 |
| `docs/ci.md` | ★ 权威 | CI 管道与本地/CI 矩阵 |
| `docs/reference.md` | ★ 权威 | 命令/工具/API/env/config 速查 |
| `docs/troubleshooting.md` | ★ 权威 | 已知坑（症状→根因→解法） |
| `docs/console_全面审计_2026-08-17.md` | #74 路线图 | console 三通道审计（健壮性 F-01~F-17 / 设计 D-01~D-07 / 命令 15vs95）与 5 批修复计划，批次落地前为执行权威 |
| `docs/vendor_sync_log.md` | ★ 权威（账本） | 每次上游同步 + 嫁接面核对 + 回滚点 |
| `docs/decisions/`（D001-D010） | ★ 权威（决策） | ADR 制度，状态 accepted |
| `bench/README.md` | ★ 权威（评测口径） | 30 题设计口径与文件 |
| `HANDOFF_新session必读.md`（根目录，gitignored） | 新 session 入口 | 现状摘要 + 环境 + 铁律（无敏感信息） |

## 历史记录（读来溯源，不作为现状执行）

| 文档 | 内容 |
|---|---|
| `docs/拍板记录_2026-08-14.md` | 底座选择全史（四仓库精读 → v3-v8 五次推倒 → 融合架构拍板） |
| `docs/融合架构_v1.0_2026-08-14.md` | v1.0 执行蓝图（A/B/C 三级嫁接清单，后续 ADR 对其有增补） |
| `docs/立项书_QRA_v0.1.md` | 立项背景（比亚迪 AI4S 岗位驱动） |
| `docs/W1_*` ~ `docs/W9-12_*` | 周完成记录（含开源发布检查单） |
| `docs/机理研究_*.md` ×5 | prime/CC/dsh/arc-code 源码与论文研究笔记 |
| `docs/archive_废弃方案/` | v3-v8 废案（**勿参照执行、勿 import**） |
| `src/qra/archive_legacy/` | 自研骨架遗留代码（废案，勿 import） |
| `reports/` | metrics.md（成功率/时延/调试记录）、评测报告 |

## 维护义务（谁改了什么必须同步）

| 改动 | 必须同步 |
|---|---|
| 架构/目录/插件面/执行线 | `docs/architecture.md` |
| 工作流/门禁/提交协议 | `docs/development.md` + `scripts/verify_qra.sh` 头注释 |
| CI 管道 | `docs/ci.md` |
| 工具参数/命令/env/config | `docs/reference.md` |
| 新坑 | `docs/troubleshooting.md`（先加条目再修） |
| 上游同步/嫁接面 | `docs/vendor_sync_log.md`（必做，D009） |
| 不可逆决策 | `docs/decisions/DNNN_*.md` + 记忆 |
| 需求完成 | `hermes_output/cc/2026-MM-DD-qra-*.md`（CC-Hermes 同步铁律） |

一致性由 `scripts/check_docs.py` 兜底（链接与路径存在性），内容一致性靠
「改完顺手改文档」的纪律——门禁与 CI 都拦不住「没写文档」。
