# 遗留代码归档（自研 agent 骨架时代）

> 归档时间：2026-08-15。这些模块是 v5-v8 架构期"自研 agent 骨架"的代码，
> 融合架构（D002）落地后**零引用、不再运行**。保留作历史参考，不要在新代码中 import。

| 文件 | 时代 | 被什么取代 |
|---|---|---|
| main.py / main_v7.py | 自研入口（v2/v7 推倒重来） | `bin/qra` → hermes 引擎（vendor）+ QRA 定制层 |
| agent.py / llm.py / state.py | 自研 LLM Agent 骨架 | vendor/hermes-agent 的 AIAgent |
| memory.py / memory_compat.py | 自研记忆层（字符硬预算） | hermes 记忆 provider 机制 |
| build_kb.py | KB 灌库脚本 | `.hermes/plugins/qra/kb.py`（工具插件内建） |
| refine.py | 双评审门精炼回路（v5） | 待 D008 落地时在插件侧重做（qra_refine） |
| tools/（registry/market/kb） | 自研工具注册表 | `.hermes/plugins/qra/`（quote/signal/kb/sync 插件工具） |
| qra_config.yaml | v8 时代全局配置（model/memory/database/skills） | 零引用；现行架构参数由 bin/qra + scripts/ 运行时直传，无配置文件 |

**当前活跃代码图**（唯一执行线）：

```
bin/qra                     # 命令入口（console / sync / -z 单发）
scripts/run_qra.sh          # 传统单发：hermes 引擎 oneshot + 插件
scripts/qra_console.sh      # CoT 全展示终端（D007 Phase 1）
scripts/verify_qra.sh       # 四层回归门禁
src/qra/console/main.py     # console 显示层（流式 Live + CC 式 UI）
src/qra/vendor_sync.py      # 上游同步核心（D009，CLI 与工具双入口）
src/qra/agents/             # AutoGen 三人小组（D005，run_team / run_compare）
.hermes/plugins/qra/        # 工具插件（quote/signal/kb/sync）
bench/                      # QRA-Bench 评测（30 题）
```
