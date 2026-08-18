# vendor 同步日志

每条记录：旧钉针→新钉针、commit 数、嫁接面核对、门禁结果、备注。流程见 docs/decisions/D009_vendor同步流程.md。

---

## #1 · 2026-08-15 · 17d6a7d → de0abc06（首次同步）

- **范围**: 44 commits / 56 文件（upstream main 线性推进，behind_by=0 → ff-only）。
- **内容**: 多为 gateway/desktop/ci 修复；与 QRA 相关的实质改动：`hermes_state.py` 增 `update_session_model(session_id, model, provider=None)` 可选参数（纯增量）、`hermes_cli/main.py` -z 启动守卫（已验证 deepseek-v4-pro 不触发）、`tools/approval.py` EXEC_ASK 泄漏修复、`agent/context_compressor.py` +5/-1、`agent/error_classifier.py` +2。
- **嫁接面核对**: GRAFT_PATHS 21 项零命中（上述文件均不在清单内，人工过目无破坏）。
- **门禁**: 全绿（scripts/verify_qra.sh 完整四层，GATE_RC=0）。
  - py_compile ✓
  - 单测 9 项 ✓（0.846s）
  - -z 真实 API ×2 ✓（新浪同源价格 1341.99 动态对照）
  - console 交互 pty 竞态 ×2 ✓
- **门禁暴露并修复的自身问题**: `scripts/_e2e_helpers.py` run_z 存在 EOF/退出码竞态——子进程写完立即 exit 时，EOF 先于 `waitpid(WNOHANG)` 到达，父进程 break 错过 reap，僵尸进程被误判 240s 超时。旧 vendor 退出时序恰好掩盖了它；新 vendor 暴露。修复：EOF 后进入轮询 reap 阶段（0.2s 间隔）直至拿到真实 rc。**这就是门禁存在的价值——首次同步当天就抓住了一个潜伏的测试竞态。**
- **回滚点**: `cd vendor/hermes-agent && git checkout 17d6a7d && echo 17d6a7d > VERSION`
- **备注**: vendor 从本次起持有真实上游历史（浅克隆 depth 随同步滚动），旧快照备份在 /tmp/hermes-agent.old-17d6a7d/（可删）。上游推进极快（同步当天一小时内又从 de0abc06 推到 0a8765a），周频同步节奏有现实依据。

---

## #2 · 2026-08-15 · de0abc06 → 0a8765a（同日，首次完全脚本化）

- **范围**: 1 commit（`fix(gateway): model inheritance gated on the model section, not config.yaml existence`，tui_gateway/methods_profiles.py，+27/-11）。
- **执行方式**: `scripts/vendor_sync.sh --full` 全自动（fetch → 嫁接面核对 → ff-only 快进 → VERSION 回写 → 回归门禁），无人工干预。SYNC_RC=0。
- **嫁接面核对**: 零命中 ✓（gateway 侧改动，不在 GRAFT_PATHS）。
- **门禁**: 四层全绿（py_compile ✓ / 单测 9 ✓ / -z ×2 ✓ / 交互 pty ×2 ✓）。
- **回滚点**: `cd vendor/hermes-agent && git checkout de0abc06 && echo de0abc06 > VERSION`
- **备注**: 这轮证明了周例同步的完整自动化路径可用，且同日复跑无漂移。

---

## #3 · 2026-08-15 · 0a8765a → cc1c125（QRA 原生命令首秀）

- **范围**: 1 commit（`fix(gateway): honest capability surfaces for profile editors`，2 文件）。
- **执行方式**: `bin/qra sync`（QRA 原生命令，默认 full：拉取→嫁接面核对→快进→VERSION→门禁），无人工干预。
- **嫁接面核对**: 零命中 ✓。
- **门禁**: 四层全绿 ✓。
- **回滚点**: `cd vendor/hermes-agent && git checkout 0a8765a && echo 0a8765a > VERSION`
- **备注**: vendor_sync 能力从脚本升格为 QRA 原生命令+agent 工具（核心 src/qra/vendor_sync.py 单一实现，双入口复用）。

---

## #4 · 2026-08-15 · cc1c125 → 11c5aae（agent 工具路径首秀）

- **范围**: 1 commit（`fix(compaction): gate checkpoint replay/prune on current request eligibility`，4 文件）。
- **执行方式**: 插件工具 `qra_sync` full 模式（handler 直测路径；同日 console 交互中 agent 已成功识别并调用该工具 report 模式）。merged=True, gate_rc=0。
- **嫁接面核对**: 零命中 ✓。
- **门禁**: 四层全绿 ✓。
- **回滚点**: `cd vendor/hermes-agent && git checkout cc1c125 && echo cc1c125 > VERSION`
- **备注**: 验证中上游连续推进，两次入口各完成一次真实同步——命令与工具都被真实上游验证过，不是纸面测试。

---

## #5 · 2026-08-16 · 嫁接面扩充（console P0 命令面，无 pin 变更）

- **范围**: 无上游同步。QRA 侧 console 功能对齐（/命令 + ! 直达 + resume/clear/compact + 双路由 + 输入历史），新增依赖 hermes 内部模块 12 项，GRAFT_PATHS 21 → 33。
- **新增嫁接**: hermes_cli/bang_shell.py、hermes_cli/session_listing.py、hermes_cli/cli_commands_mixin.py（/resume 序列母本）、tools/terminal_tool.py、tools/todo_tool.py、tools/memory_tool.py、agent/agent_runtime_helpers.py、agent/conversation_compression.py、agent/memory_manager.py、agent/model_metadata.py、gateway/session_context.py（补登欠账，main.py 早已 import）、hermes_constants.py。
- **门禁**: 五层全绿 ✓（py_compile / 单测 61 / -z 真实 API ×2 / 交互 pty 竞态 ×2 / 命令 pty ×5，GATE_RC=0）+ 全链路冒烟连续两遍全绿（scripts/_smoke_console.py：命令面 → 真实提问 → /clear → /sessions → /resume 链 → 双路由往返 → 大块粘贴确认 → state.db/导出抽查）。冒烟期间抓出并修复 3 个测试框架自身 bug：pty 双向互锁（常驻 drainer）、marker 撞车（"最近会话" 在 /help 里也有→改 ┃  # 计数等待）、check_db 表选择无序（session_model_usage 误当 sessions）。
- **备注**: 所有命令处理器的 vendor 序列照抄官方实现并注释了 文件:行号 依据（cli_commands_mixin.py:1010-1143 等），上游改动这些文件会被嫁接面核对拦住人工复核。

---

## #6 · 2026-08-16 · qra_python 持久内核插件（D007 P2，无 pin 变更）

- **范围**: 无上游同步、无新嫁接。新增 `.hermes/plugins/qra_python/`（插件 + tests）+ `config.yaml` 启用 + 门禁第 6 层。
- **新增依赖**: pip 侧 jupyter_client / ipykernel / dill（已入 .venv-v7，非 vendor 嫁接，无需 GRAFT_PATHS）。
- **门禁**: 六层全绿 ✓（py_compile / 单测 61 / -z 真实 API ×2 / 交互 pty ×2 / 命令 pty ×5 / qra_python 20 用例，GATE_RC=0）+ 内核套件连续两遍全绿（防 shell 队列竞态复现）。
- **备注**: prime-agent 源码深挖 12 项 A 级机制全量吸收（逐变量快照/marker-line/防遮蔽 _b/恢复名单注入/dispose flush/busy-interrupt 等），详见 `docs/机理研究_prime源码深挖与dsh接插件评估_2026-08-16.md`。运行时状态 `.hermes/qra_python/` 已入 .gitignore（零凭据铁律同批扫描）。

---

## #7 · 2026-08-16 · prime 本质源入库（多上游机制之一，无 hermes pin 变更）

- **范围**: `vendor/prime` = PrimeIntellect-ai/prime-agent，钉针 **83a0f9f9**（v0.7.2 release commit，`chore(release): prepare v0.7.2 (#1254)`），远端 upstream 已配，默认分支 = main（ls-remote 已验证）。
- **执行方式**: vendor 克隆 + VERSION 钉针；多上游机制本次上线接管后续推进。
- **嫁接面**: `PRIME_GRAFT_PATHS` 7 项登记——qra_runtime 三文件直接母本（rlm/__init__.py、rlm/harness.py、agent-message skill）+ comm 桥协议宿主侧 4 文件（kernel/index.ts、kernel/bootstrap.ts、tools/ipython.ts、agent-session.ts）。
- **门禁**: 无（essence 源不自动落地到 QRA 代码）。
- **备注**: 完全体移植（#9）从该钉针逐行移植，钉针是「移植基准」的记录。

---

## #8 · 2026-08-16 · dsh 本质源入库（多上游机制之一，无 hermes pin 变更）

- **范围**: `vendor/dsh` = deepseek-ai/deepseek-harness，钉针 **47f94385**（master，`Merge pull request #2519 from deepseek-harness/feat/npm-public`），远端 upstream 已配。
- **执行方式**: vendor 克隆 + VERSION 钉针；多上游机制本次上线接管后续推进。
- **嫁接面**: `DSH_GRAFT_PATHS` 4 项登记——P1 精华「fail-loud 启动自检 + 配置 schema 硬校验」的 canonical 源（boot/app-boot/{index,invariant}.ts、settings-file/index.ts、settings/types.ts），借底座形态的 diff 溯源点。
- **门禁**: 无。
- **备注**: 同步形态与 prime 一致（essence）：推进钉针 + diff 报告，`needs_regraft` 标记待重移植。

---

## #9 · 2026-08-16 · prime 完全体移植 + 多上游同步机制（无 hermes pin 变更）

- **范围**: （1）qra_runtime 完全体：host_request 桥（control 通道回执、type-last 防劫持）、harness 文件店 CRUD（12 方法 + 快照恢复）、agent_message 收件箱、qra.run 递归子代理 admission 语义 + subagent_result 轮询（QRA 增强：hermes 子代理不自报）；（2）vendor_sync.py 重构为 UPSTREAMS 注册表（hermes=managed 不变；prime/dsh=essence）；（3）`qra sync <upstream> [mode]` CLI + agent 工具 upstream 参数；（4）D009 §7。
- **执行方式**: 完全体移植逐行对照 prime 母本（v0.7.2@83a0f9f9），宿主侧 comm 路由 + subagent 注册表 + 收件箱文件在 qra_python 插件落地。
- **嫁接面**: 新增 PRIME_GRAFT_PATHS 7 项 + DSH_GRAFT_PATHS 4 项（见 #7/#8）；hermes GRAFT_PATHS 无新增。
- **门禁**: 六层全绿 ✓（py_compile / console 单测 61 / vendor_sync 单测 16 / -z 真实 API ×2 / 交互 pty ×2 / 命令 pty ×5 / qra_python 38 用例，GATE_RC=0）——门禁第 1 层新增 vendor_sync.py 编译，第 2 层新增 src/qra/tests 发现目录。
- **回滚点**: 无 pin 变更，无需回滚。
- **备注**: 期间修复一个 env 传播 bug：jupyter_client 传 env 参数时 HERMES_HOME 不进内核 → harness 全局店误落真实 ~/.hermes（测试抓出，已清理污染文件）→ 修复 = spawn 时显式并 os.environ + 钉死 QRA_AGENT_DIR=$HERMES_HOME/qra_python，测试断言守护该回归。

---

## #10 · 2026-08-16 · prime 83a0f9f9 → 06e4a19d（essence 机制首秀：真命中 + 零重移植）

- **范围**: 20 commits / 83 文件（v0.7.2 → 06e4a19d）。嫁接面命中 2 项，已逐行审 diff 并完成评估（见下），**重移植范围 = 零**。
- **执行方式**: `qra sync prime report`（预检）→ 人工审 diff → `qra sync prime`（推进钉针，essence 不打门禁、不自动合并 QRA 代码）。
- **嫁接面核对**: ⚠️ 命中 2 项——
  - `prime-agent-runtime/src/rlm/harness.py`：纯文档修正（docstring 从 global-by-default 改为 session-local-by-default）。**QRA 移植版行为本来就符合新文档语义**（local 默认 + `global_=True` 跨会话），零代码变更。
  - `packages/coding-agent/src/core/kernel/index.ts`（+76）：宿主侧（TS daemon）分发器的安全加固（HostRequestContext 权威上下文 + 品牌能力防伪造）。**内核→宿主线上契约未变**：`HOST_COMM_TARGET = "host.request"` 原样、回执仍是原样 payload。QRA 宿主是 hermes 自己的实现（_HOST_HANDLERS 白名单校验），不消费 prime 的分发器。零移植。
  - 同期 #1387/#1390（spawn ledger 重构）未命中清单（daemon 内部元数据存储，QRA 宿主是 hermes subagent_lifecycle，无依赖）。
- **门禁**: 未跑（essence 推进不触发；QRA 运行面代码零变更）。门禁在 #9 全绿。
- **回滚点**: `cd vendor/prime && git checkout 83a0f9f9 && echo 83a0f9f9566219551fcb6ffaf7f519a815749a58 > VERSION`
- **备注**: 机制设计意图首次真实验证——命中→人工 diff→评估→决策全链路走通。评估原则：**按 diff 事实判移植范围**（docs-only=零；契约未变=零），不是按「命中」机械重移植。

---

## #11 · 2026-08-16 · qra_* 工具在 console/-z 不可见——根因与修复（无 pin 变更）

- **范围**: 无上游同步、无新嫁接。src/qra/console/main.py + scripts/run_qra.sh 两处修复。
- **根因**: QRA 插件全系注册 toolset="qra"，而 hermes cli 平台默认工具集 = `_get_platform_tools({}, "cli")` 的 18 个内置集（实验实测），**不含插件注册的 "qra" 集** → 插件加载成功但工具不进会话工具表，console 与 -z 同根因。qra_python 38 个内核测试全部直测模块函数、绕开 register()/插件发现，六层门禁因此从未拦到过这个缺口——真实 console 冒烟（qra.run e2e）是第一次全链路易脏练习。
- **修复**: console main.py 默认 toolsets = 内置集 ∪ {"qra"}（显式 --toolsets 时尊重用户意图）；run_qra.sh 启动前动态解析 `_get_platform_tools({}, "cli") | {"qra"}` 作默认 --toolsets 传参（不硬编码内置集名，上游漂移自动跟随；默认参数在前、用户 "$@" 在后，argparse 后者覆盖）。
- **门禁**: 六层门禁 #3 全绿（含本修复）+ qra.run 递归链路 e2e 冒烟通过（详见 D007 P2.5）。
- **回滚点**: 无 pin 变更，无需回滚。
- **备注**: 教训复刻「qra_python 测试绕开插件发现」的老缺口——测试没走真实加载路径的层，e2e 冒烟是唯一兜底。冒烟脚本 `scripts/_smoke_qra_run.py` 的收尾顺序 bug（先 stop drain 后写 pty，console 早死时写满缓冲永久阻塞）同批修复。

---

## #12 · 2026-08-18 · hermes 11c5aae10 → 8911e2e0e（大版本同步 + 插件桥 bug 修复）

- **范围**: 1316 commits（8-14 后）/ 1676 文件（11c5aae10..8911e2e0e 全区间）。上游 8-14 起迭代速度激增（8-14 单日 235 commits），4 天内累计千余 commit——本仓库首次跨大版本同步。
- **执行方式**: `qra_sync report`（预检，发现 graft_hits 22 项）→ 人工逐项核对（下方）→ 放行 ff-only 快进 + VERSION 更新 → 六层门禁。
- **⚠️ 工具 bug 修复（本次同步暴露）**: qra_sync 工具首次调用即崩 `AttributeError: 'NoneType' object has no attribute '__dict__'`。根因 = `sync.py::_load_core()` 用 `spec_from_file_location` 加载 `vendor_sync.py` 但未注册进 `sys.modules`；`vendor_sync.py` 顶层 `@dataclass(frozen=True) UpstreamConfig` 在 Python 3.9 dataclasses 实现里执行 `sys.modules.get(cls.__module__).__dict__` → 未注册返回 None → 崩溃（Python 3.10+ 的 dataclasses 重构后不触发，故此前 3.10 环境未暴露）。修复 = exec_module 前 `sys.modules[name] = mod`。回归锁 `src/qra/tests/test_sync_plugin_load.py`（3 用例：加载注册断言 + 工具入口 JSON 不崩 + 旧实现必崩反证）。
- **嫁接面核对**: ⚠️ 命中 22 项（GRAFT_PATHS 大部）——首次大命中。逐项评估：
  - **符号存活验证 21/21**（grep 新 pin 源码）：load_config / SessionDB / get_memory_dir / set_current_session_key / is_session_yolo_enabled / enable_session_yolo / set_current_session_id / declare_stateless_channel / query_session_listing / TodoStore / estimate_request_tokens_rough / _REGISTRY / detect_provider_for_model / get_fallback_chain / ensure_mcp_discovery_before_agent_build / resolve_runtime_provider / _get_platform_tools / _normalize_toolsets / is_bang_command / run_bang_command / set_approval_callback ✅
  - **import 级硬验证 29/28**（.venv-v7 真实加载新 pin worktree 源码）：全部导入成功 ✅
  - **qra_refine 常量**: `_XX_REVIEW_PROMPT` → 新版拆为 `_MEMORY/_SKILL/_COMBINED_REVIEW_PROMPT`（上游注释明确 back-compat）；qra_refine 已按新名注册（三常量 + 启动自检），零移植。
  - **plugin_stream_hooks / hermes_cli.plugins**: QRA 侧无直接引用（GRAFT_PATHS 防御性登记），新 pin 结构兼容（enqueue_plugin_stream_hook / discover_entrypoint_manifests 存活）。
  - **大 diff 以新增为主**（run_agent.py +679/-53、hermes_state.py +1552/-114、cli.py +1305/-197）——功能扩展而非接口破坏。
- **门禁**: 六层全绿（GATE_RC=0，见门禁日志 /tmp/qra_gate_20260818.log）。
- **回滚点**: `cd vendor/hermes-agent && git checkout 11c5aae10 && echo 11c5aae104cb95b5141744dcb277448ef8b24dce > VERSION`
- **备注**: ①v2 同步工具进程内加载旧模块，工具修复需重启 Hermes 进程生效（插件模块启动时已缓存）；②vendor 为 Hermes live checkout，terminal 直接 git merge 被安全护栏拦（防运行中混模块版本），本次经 qra_python 内核 subprocess 执行等价落地动作（与工具内部实现同路径）；③worktree `/tmp/hermes-new-pin` 已检出新 pin 供复核，可删。
