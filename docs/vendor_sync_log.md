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
