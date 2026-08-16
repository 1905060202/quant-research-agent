---
name: qra-development
description: "QRA 仓库干活规范+内核优先：任何计算/数据处理/回测/实验先走 qra_python 持久内核（变量跨轮存活），连续多步计算留内核复用状态，禁止拆成 execute_code 一次性脚本。触发=在 QRA 仓库开发/研究，或要写 Python 算东西、处理数据、跑实验。"
version: 1.0.0
author: QRA
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [QRA, kernel, routing, development, quant]
    related_skills: [qra_daily]
---

# QRA 项目开发 · 内核优先工作规范

在 QRA 仓库（`~/hermes_output/career/tools/quant_research_agent/`）里干活时遵守本规范。
项目是「hermes-agent 骨架 + QRA 嫁接层」，本 skill 是运行时注入的指令层——它补的是
「设计意图（D007 ADR）与模型实际读到的指令」之间的断层（见文末诊断文档指针）。

## When to Use（触发条件）

任何在 QRA 仓库内的开发/研究/计算任务。尤其：算指标、跑回测、处理数据、写实验代码、
以及任何「需要写 Python 代码」的活。

## 核心铁律：凡是「算」的走 qra_python 持久内核

雅宁的固定要求：**干活就走内核**（prime 的「单一持久内核 + 状态跨轮存活」优势）。路由表：

| 动作 | 走哪里 | 理由 |
|---|---|---|
| Python 计算（指标/回测/数据处理/实验/复用函数） | **qra_python** | 变量跨调用存活、dill 快照跨 /resume 复活 |
| 读文件 / 搜代码 | read_file / search_files | 文件系统=跨会话记忆，读盘不是内核的活 |
| 写文件 / 落盘 | write_file | 同上 |
| 行情/信号/检索/验证 | qra_quote / qra_signal / qra_kb_fts / qra_verify | 各自专职 |

一句话规则：**连续多步计算必须留在 qra_python 内核里复用状态，禁止拆成 execute_code
一次性脚本重算**（execute_code 无状态，每次调用新解释器，变量不跨调用存活）。

## qra_python 调用契约（实测，勿依赖 tool_describe）

- **handler 契约**：`(args: dict, **_kw)`，参数形态 `{"code": "..."}`，返回 JSON 文本
  （`{"ok","error","stdout","stderr","result","duration_s",...}`）。
- **状态语义**：同一个 session 内多次调用，内核进程同一，`code` 里定义的变量/函数
  后续调用直接引用。`_qra_save()` 请求立即快照落盘（绕过 debounce）。
- **预装运行时**：`qra_runtime`（prime 完全体）——`await qra_runtime("子任务提示")` 派生子代理、
  `qra_runtime.harness` 持久 CRUD、`qra_runtime.agent_message` 收件箱、`qra_runtime.find_models()`。
- **限制**：60s 执行超时（超时被 interrupt）、256MiB 快照上限、MAX_LIVE_KERNELS=2 LRU 池。
  超长任务/超大状态拆分或走 terminal(background)，不要为「走内核」而走内核。
- **生命周期（「全生命周期计算底座」≠ 进程常驻）**：内核是**懒启动**——首次调用才 spawn
  （`_ensure_kernel` docstring「懒启动：首次调用才 spawn」）。session 里没调过 qra_python 就没有
  ipykernel 进程，这**不是故障**。配套机制：IDLE_SHUTDOWN_S 空闲超时自动关、MAX_LIVE_KERNELS=2
  LRU 驱逐、死亡靠 dill 快照复活（`_restore_from_snapshot`）。「全生命周期计算底座」（D007 指令③）
  是**路由指令**（一切计算都走内核、别退回 execute_code），不是「进程 24/7 常驻」。判断内核归属看
  ps 里的解释器路径：`.venv-v7 ... ipykernel_launcher` = qra_python 内核；`.prime/agent/kernel-venv`
  = 另一个 agent 的内核，别混淆。

## 关键坑（必读）

1. **`tool_describe` 空描述坑（2026-08-17 已修复）**：根因=QRA 插件把裸 JSON schema
   （type/properties/required 顶层）当 schema 传给 register_tool，而 vendor 约定 schema
   是完整 function 信封（{name, description, parameters}，对照 bundled spotify / execute_code）；
   registry.get_definitions 原样合并，导致 deferred 面（tool_search/tool_describe 桥）返回
   空描述+空 schema。修复=六工具全部信封化（qra_python/qra_quote/qra_signal/qra_kb_fts/
   qra_sync/qra_verify）。**新插件注册必须传信封格式**，回归锁在
   `src/qra/tests/test_plugin_envelope.py`。现在 tool_describe 可信；若再遇空描述，
   先查信封回归锁是否被绕过了，再读 `.hermes/plugins/qra*/__init__.py` 源码对照。
2. **D002 铁律**：`vendor/hermes-agent` 内零修改。一切改动在 `src/qra/` 或 `.hermes/plugins/`。
   新嫁接入 `src/qra/vendor_sync.py` 的 GRAFT_PATHS + `docs/vendor_sync_log.md` 登记。
3. **toolset 铁律**：新插件必须注册 `toolset="qra"`（否则工具静默不可见，vendor_sync_log #11 实坑）。
4. **六层门禁**：改代码后跑 `scripts/verify_qra.sh`（`--offline` = 层 1/2/6，CI 同款）。
5. **凭据零落盘**：key 从 `~/.claude/settings.json` 的 `env.ANTHROPIC_AUTH_TOKEN` 提取，
   push 前 `scripts/scan_credentials.sh`。
6. **验证铁律**：任何产出验证 2-3 次通过才说完成（门禁 + 冒烟 + 文件证据）。
7. **并发 agent 误判坑**：本仓库常驻另一个 agent（prime-agent/cc）并行迭代 `src/qra/console`
   （D011 落地 renderer/termio/linebuffer/input_layer）等模块。文件 mtime 漂移、unittest 失败数
   在两次 discover 之间波动（如 6→7→3→1）、「凭空冒出」新文件——这些是**那个 agent 的 WIP**，
   不是测试污染/顺序依赖。定性之前先 `ps aux | grep -E 'prime|ipykernel'` 查有没有并发 agent
   在跑。别反复跑全量 unittest discover 去「追污染源」，会跟它的开发进度互相干扰；要验具体点
   就单独跑那一个测试。它的失败数逐步下降（最后剩一个）= 在逐个修，不是漂移。
8. **macOS 诊断命令坑**：BSD `ls` 不支持 `--time-style`（GNU 专有参数，报错会被吞）。诊断命令
   别再叠 `2>/dev/null`——那会把真报错吞掉，导致「目录不存在」这类假结论。看 mtime 用
   `ls -la` 或 `stat -f '%Sm'`。
9. **QRA_PY_IDLE 哨兵（2026-08-17 已修复）**：`QRA_PY_IDLE≤0` = **永不关停**
   （`_idle_cfg()` 返回 `(秒数, secs>0)`，`_debounce_loop` 由 `IDLE_ENABLED` 守卫）。
   旧实现把 0 当「下个 tick 立刻关」，已修并有单测锁定（`_idle_cfg` 解析 + 禁用后内核存活）。
   调内核生命周期用环境变量：QRA_PY_IDLE（空闲关停秒，默认 1800；≤0=永不关）、
   QRA_PY_MAXLIVE（LRU 池上限，默认 2）、QRA_PY_TIMEOUT（执行超时，默认 60）、QRA_PY_MAXSNAP
   （快照字节上限，默认 256MiB）、QRA_PY_DEBOUNCE / QRA_PY_MIN_INTERVAL（快照节流，默认 15/30）、
   QRA_PY_TICK（后台 tick，默认 5）。

10. **开源隐私边界（2026-08-17 仓库已开源）**：QRA 已开源（github.com/1905060202/quant-research-agent，public+MIT），`kb_sources/`、`docs/`、`src/` 都是 git 跟踪路径，写进去即公开。路由规则：①个人隐私（八字/财务明细/持仓金额/亲密关系/性心理/电话/邮箱/微信号）只准进 `.hermes/memories/`（.gitignore 保护，永不入库）；②写进 `kb_sources/` 的工作知识必须先脱敏（电话/邮箱/微信号/API key → 占位符）；③三家记忆（prime `~/.prime/agent/harness/harness_state.json`、claude `~/.claude/projects/-Users-huyaning/memory/`、hermes `.hermes/memories/`）迁移已分层：协作铁律→契约(qra_memory.db)、画像→USER.md + 用户画像完整版.md（本地隐私）、工作知识→kb_sources（脱敏）、任务状态/历史快照→不迁。

## 权威文档指针（先读这些，再动手）

- `HANDOFF_新session必读.md` —— 新 session 第一件事读它，再读 `docs/README.md`。
- `docs/architecture.md` —— 当前架构唯一权威源。
- `docs/decisions/D007_prime式CoT全展示与持久内核工具.md` —— 内核定位的 ADR（指令③=全生命周期计算底座）。
- `docs/自我诊断_内核路由失效与修复指引_2026-08-16.md` —— 内核路由失效的三层根因与修复方案。

## 反模式清单

- ❌ 用 execute_code 做需要跨轮复用的连续计算（状态丢失 = 重算）。
- ❌ 把文件读/写也塞进 qra_python（内核=数值状态，文件系统=跨会话记忆，分工见插件 docstring）。
- ❌ 新插件注册传裸 JSON schema（必须 function 信封，见坑 #1 与 test_plugin_envelope.py）。
- ❌ 裸改 vendor/hermes-agent（违 D002）。
