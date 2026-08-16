# 架构总览

> 权威源：本文是「QRA 现在长什么样」的唯一答案。改动架构后**必须同步更新本文**。
> 决策为什么这么定 → `docs/decisions/`；上游同步的账本 → `docs/vendor_sync_log.md`；
> 每个文件的用法细节 → `docs/reference.md`；历史蓝图 → `docs/融合架构_v1.0_2026-08-14.md`。

## 一句话

QRA = **hermes-agent 骨架（核心循环，零改动）+ QRA 嫁接层（全部能力）+ 量化领域层**。
骨架负责「怎么思考、怎么记、怎么执行」，嫁接层负责「量化研究怎么做」。

## 分层图

```
┌─────────────────────────────────────────────────────────────────┐
│ 入口层（bin/qra，一个入口三种模式）                                │
│   裸调/console → src/qra/console/    CoT 全展示终端（自建显示层） │
│   qra -z …    → scripts/run_qra.sh   hermes oneshot 单发        │
│   qra sync …  → src/qra/vendor_sync.py  三上游同步               │
├─────────────────────────────────────────────────────────────────┤
│ 嫁接层（.hermes/plugins/，全走 hermes 官方插件面，D002 零核心改动）│
│   qra        行情 quote / 信号 signal / 检索 kb_fts / 同步 sync   │
│   qra_verify 声称账本 + 4 类确定性检查器 + 回合末守卫             │
│   qra_python 持久内核：会话级 Jupyter + qra_runtime（子代理/文件店）│
│   qra_refine 评审门（background_review 三钩子，无工具）           │
│   qra_memory Mem0 式记忆 provider（三重去重 + 叙事链）            │
├─────────────────────────────────────────────────────────────────┤
│ 底座（vendor/hermes-agent，pin 11c5aae，独立 git，零修改）        │
│   核心循环 / 会话 DB / 记忆框架 / 审批 / 工具注册 / SKILL 机制     │
│   引擎可执行：.venv-v7/bin/hermes                               │
├─────────────────────────────────────────────────────────────────┤
│ 上游研究仓（vendor/，gitignored，钉针由 qra sync 管理）           │
│   prime（essence）· dsh（essence）· hermes（managed）            │
├─────────────────────────────────────────────────────────────────┤
│ 评测层（bench/）：30 题 ×3 域，动态 gold + 机械评分 + 幻觉双口径   │
└─────────────────────────────────────────────────────────────────┘
```

## 执行线（运行时数据流）

### 1. console 交互（主入口，`qra` 裸调）

```
qra → bin/qra → scripts/qra_console.sh → python -m src.qra.console.main
  ├─ 构建 AIAgent（hermes 公开类，与 oneshot/tui_gateway 同款先例）
  ├─ 工具集 = cli 平台默认 18 内置集 ∪ {"qra"}（QRA 插件全系注册在 qra 集）
  ├─ 用户输入三分流（src/qra/console/commands.py）：/命令 · ! shell · 普通提问
  ├─ /yolo 默认开（session 级）；/model 双路由（deepseek ↔ opus@127.0.0.1:8789）
  └─ /loop 进程内调度器：空闲后以 last prompt 自动继续（CC 对齐）
```

### 2. 单发（`qra -z "问题"`）

```
qra -z → scripts/run_qra.sh → hermes oneshot
  工具集动态解析：_get_platform_tools({}, "cli") ∪ {"qra"}
  凭据：ANTHROPIC_TOKEN env → ~/.claude/settings.json 兜底，零落盘
```

### 3. 持久内核 + 递归子代理（qra_python / qra_runtime）

```
console 会话 → 模型调 qra_python 工具
  → 会话级 Jupyter 内核（jupyter_client + ipykernel，HERMES_HOME/qra_python/）
      内核预装 qra_runtime（以 QRA_AGENT_DIR 钉死插件目录）
  → 内核代码 await qra.run(goal)
  → iopub comm 消息（target="qra.host.request"，control 通道回执，
     type-last 防劫持：payload 的 type 字段最后合并）
  → 宿主路由 → hermes subagent_lifecycle 接纳子代理（admission）
  → 子代理独立会话跑 goal（模型双路由）
  → 内核轮询 qra.subagent_result → status/summary/error
```

**QRA 增强**（prime 没有的）：死内核自动重启 + 快照复活、执行中 5s 探活、
LRU 逐出、空闲关停、审计 jsonl（kernel_history/{sid}.jsonl）。
**刻意分歧**（记入 D007 P2）：快照 debounce 15s+30s（prime 是 1500ms，量化
大 DataFrame 不能每笔快照）；输出截断 4000 字符（prime 65536）。

### 4. 上游同步（`qra sync`）

```
qra sync [upstream] [mode] → src/qra/vendor_sync.py
  upstream: hermes | prime | dsh（缺省 hermes）
  hermes = managed：fetch → ff-only 合并 → 嫁接面核对（命中即拒）→ 六层门禁 → 失败回滚
  prime/dsh = essence：fetch → ff-only 合并 → diff 报告（嫁接面命中 → needs_regraft
              → 人工逐行审 diff 判重移植）→ 推进 VERSION 钉针。不打门禁、不自动合并。
  durable 账本：docs/vendor_sync_log.md（每次同步一条，含回滚点）
```

## 目录地图

| 路径 | 内容 | 状态 |
|---|---|---|
| `bin/qra` | 命令入口（符号链接安全解析） | 活跃 |
| `scripts/run_qra.sh` | 传统单发入口 | 活跃 |
| `scripts/qra_console.sh` | console 入口（env + 凭据 + 启动） | 活跃 |
| `scripts/verify_qra.sh` | 六层回归门禁（改代码必跑） | 活跃 |
| `scripts/_smoke_console.py` | console 全链路冒烟 | 活跃 |
| `scripts/_smoke_qra_run.py` | qra.run 递归链路 e2e 冒烟 | 活跃 |
| `scripts/build_kb.py` / `load_kb_docs.py` | KB 建库 / 灌库（FTS5 trigram） | 活跃 |
| `scripts/check_cost.py` | 成本周检（¥350/周阈值） | 活跃 |
| `scripts/run_qra_daily.sh` | 日报一键生产（qra_daily skill） | 活跃 |
| `scripts/vendor_sync.sh` | 兼容薄壳（真身在 src/qra/vendor_sync.py） | 兼容保留 |
| `scripts/scan_credentials.sh` | 零凭据扫描（push 前必跑，CI 同款） | 活跃 |
| `scripts/check_docs.py` | 文档链接/路径核对（CI 同款） | 活跃 |
| `src/qra/console/` | 显示层 + 命令面 + 输入层 + 双路由 | 活跃 |
| `src/qra/vendor_sync.py` | 三上游同步核心 | 活跃 |
| `src/qra/config_guard.py` | 配置 schema 硬校验（dsh 精华 P1） | 活跃 |
| `src/qra/agents/` | AutoGen 三人小组（D005，JD 学习轨道） | 学习轨道 |
| `src/qra/archive_legacy/` | 自研骨架遗留（v7 前废案，勿 import） | 归档 |
| `.hermes/config.yaml` | 模型/插件/记忆/审批/覆盖配置 | 活跃（入库） |
| `.hermes/plugins/*/` | 五个插件（源码入库） | 活跃 |
| `.hermes/skills/qra_daily/` | 日报技能（唯一入库 skill） | 活跃 |
| `.hermes/*` 其余 | 运行时状态（state.db/sessions/kernel/…） | gitignored |
| `bench/` | QRA-Bench 30 题评测 | 活跃 |
| `kb_sources/` | 方法论文档源（已脱敏，两 KB 可重建） | 活跃 |
| `data/` | 运行时数据（kb_fts.db/账本/审计） | gitignored |
| `reports/` | metrics / 评测报告 | 活跃 |
| `docs/decisions/` | ADR D001-D010 | 决策记录 |
| `docs/vendor_sync_log.md` | 上游同步 durable 账本 | 权威账本 |
| `docs/W*.md` / `docs/机理研究_*.md` / `docs/拍板记录*` / `docs/融合架构_v1.0*` | 周记录 / 研究 / 历史 | 历史记录 |
| `docs/archive_废弃方案/` | v3-v8 废案（勿参照执行） | 归档 |
| `vendor/` | 上游仓 + 研究快照（见下） | gitignored |

### vendor/ 内部

| 子目录 | 内容 | 钉针 |
|---|---|---|
| `vendor/hermes-agent` | 底座（NousResearch/hermes-agent） | 11c5aae（VERSION 文件） |
| `vendor/prime` | prime-agent（essence 上游） | 06e4a19d |
| `vendor/dsh` | deepseek-harness（essence 上游） | 47f94385 |
| `vendor/hermes/` `vendor/claude/` | 早期研究用文件快照（非完整 clone） | — |

## 插件面登记

| 插件 | 形态 | 注册名 | toolset | 依赖 |
|---|---|---|---|---|
| qra | 工具集 ×4 | qra_quote / qra_signal / qra_kb_fts / qra_sync | qra | 新浪行情源、kb FTS5 |
| qra_verify | 工具 + 回合末守卫 | qra_verify | qra | 账本 sqlite |
| qra_python | 工具 + 宿主桥 | qra_python | qra | jupyter_client/ipykernel/dill |
| qra_refine | background_review 三钩子（无工具） | — | — | hermes 评审门常量改写 + fail-loud |
| qra_memory | 记忆 provider（不在 plugins.enabled） | — | — | 记忆 sqlite |

**toolset 铁律**：新插件必须注册 `toolset="qra"`（或让 `_get_platform_tools`
默认集覆盖它）——否则工具静默不可见（2026-08-16 实坑，见
`docs/troubleshooting.md` 与 vendor_sync_log #11）。

## 上游钉针与嫁接面

钉针唯一事实源：`vendor/<name>/VERSION`；durable 账本：`docs/vendor_sync_log.md`。
新嫁接（import/改写 hermes 内部模块）**必须**登记进 `src/qra/vendor_sync.py`
的 GRAFT_PATHS，否则 `qra sync` 的嫁接面核对拦不住上游漂移。当前清单规模：
hermes 33 项、prime 7 项、dsh 4 项（以 vendor_sync.py 代码为准）。

## 铁律（改动前必读，全文引用自 ADR）

1. **D002**：vendor/hermes-agent 零修改。一切改动在 `src/qra/` 或 `.hermes/plugins/`。
2. **D009**：新嫁接必入 GRAFT_PATHS + vendor_sync_log.md 登记。
3. **验证铁律**：任何产出验证 2-3 次通过才说完成（`scripts/verify_qra.sh` + 冒烟）。
4. **零凭据铁律**：明文 key 永不入库，env 注入，push 前跑 `scripts/scan_credentials.sh`。
