# Console 批 3 任务单：命令 P0 移植（flash 可执行版）

> 依据：`docs/console_全面审计_2026-08-17.md` 批 3 定义（§5）。
> 本任务单所有 vendor 行号由 2026-08-17 五路溯源 agent 逐行 grep/read 验证，
> 设计层已抽查 6 处关键行号复验。执行层按本节节奏走，遇「升级信号」停手回报。

## 铁律（违反任一条 = 返工）

1. **D002**：`vendor/` 零修改。只 import，不改一个字。
2. **D009**：任何新 import 的 vendor 模块 → 追加进 `src/qra/vendor_sync.py:59` 的
   `GRAFT_PATHS`（按字母序插在 agent/ 段内合适位置）+ `docs/vendor_sync_log.md`
   加一条登记（格式照 #10/#11 先例：范围/嫁接面核对/门禁/回滚点/备注）。
3. **troubleshooting 先加条目**：每任务落地前先在 `docs/troubleshooting.md` 加该命令的
   故障条目（维护义务，见 blueprint）。
4. **验证**：每任务 = 单测全绿 + `scripts/verify_qra.sh` 七层门禁全过 + 该任务的
   pty 断言 ✓。没全过不算完成。
5. **溯源**：写任何 handler 前先 `grep -n` 本任务单给的 vendor 行号原文，照语义写，
   不凭记忆。

## 全局前置（只做一次）

`src/qra/vendor_sync.py:59` GRAFT_PATHS 追加以下 7 项（批 3 全体）：

```python
    "agent/context_breakdown.py",       # qra_console: /context 占用统计
    "agent/context_compressor.py",      # qra_console: /retry /undo is_user_originated_turn（补登欠账）
    "agent/context_references.py",      # qra_console: @file @folder 引用注入
    "agent/prompt_builder.py",          # qra_console: /steer marker 常量（agent_runtime_helpers 内部依赖）
    "agent/skill_commands.py",          # qra_console: 技能斜杠命令（scan/叠加/invocation）
    "agent/skill_preprocessing.py",     # qra_console: 技能模板变量/shell 展开
    "agent/skill_utils.py",             # qra_console: 技能 frontmatter/目录扫描
    "tools/skills_tool.py",             # qra_console: 技能 payload 权威出口
```

`docs/vendor_sync_log.md` 登记一条（2026-08-17，批 3 命令 P0 移植，范围=上述 8 项）。

## 设计裁决（已定，执行层不重开）

| 裁决 | 内容 |
|---|---|
| /skills 全套 hub 砍掉 | 不移植 search/install/audit（skills_hub.py 2036 行 Rich/网络栈）。只做 list+help：用 `scan_skill_commands()` 返回 dict 自排版 |
| bundle 分支砍掉 | 不 GRAFT `agent/skill_bundles.py`；叠加语法保留（`split_stacked_skill_commands` 纯函数） |
| 确认门砍掉 | undo 不弹模态（QRA /clear /compact 无确认先例）；打印被撤文本 + 提示 /retry 可恢复。retry 本来无确认 |
| 输入预填砍掉 | `_prefill_input_buffer` 依赖 prompt_toolkit，不移植；retry 用 ctx 字段自动重发 |
| @ Tab 补全二期 | 批 3 不做（静态列表+路径补全是 P1 加项） |
| /busy 命令 | 不移植（超批 3 范围） |

---

## T1 技能斜杠命令（评级：中，~150-250 行）

**语义**：`HERMES_HOME/skills/` 下每个含 SKILL.md（frontmatter 有 name/description）的
技能自动成为一条斜杠命令；`/技能名 [args]` 把技能正文注入模型；
`/skill-a /skill-b do XYZ` 叠加（上限 5）；`/skills` 列出；`/reload-skills` 重扫。

**vendor 照抄源**（全部 `vendor/hermes-agent/`）：

| 函数 | 位置 | 用途 |
|---|---|---|
| `scan_skill_commands()` | agent/skill_commands.py:402 | 扫 SKILL.md → `{"/slug": {name, description, skill_md_path, skill_dir}}` |
| `get_skill_commands()` | 同 :498 | 懒加载入口（模块级缓存） |
| `resolve_skill_command_key()` | 同 :578 | 下划线↔连字符归一 |
| `split_stacked_skill_commands(rest)` | 同 :661 | 叠加语法解析（上限 `_MAX_STACKED_SKILLS=5`） |
| `build_skill_invocation_message()` | 同 :597 | 单技能 → 注入消息文本 |
| `build_stacked_skill_invocation_message()` | 同 :693 | 叠加 → (msg, loaded_names, missing) |
| 分发接线段（对照） | cli.py:11024-11161 | ⚡ Loading 文案与 _pending_input 语义 |
| /help 技能区（对照） | cli.py:8315-8317 | 标题文案 `⚡ Skill Commands (N installed):` |

**QRA 改动**：

1. `src/qra/console/commands.py`：`_register_p0()` 之后加 `_register_skills()`（模块级缓存 + 与 `all_commands()` 冲突检查 + 对每 slug `register(CommandDef(slug, f"/{slug} [args]", "技能", desc, _skill_invoke))`）；`_skill_invoke` 在 `dispatch` 的 `d is None` 分支之前查技能映射。注意 QRA 注册表键是**裸名**（无 `/`），vendor 键是 `"/slug"`——统一剥 `/`。
2. `src/qra/console/handlers.py`：`cmd_skills(ctx, args)`（list+help：scan 结果纯文本排版，**不移植 skills_hub Rich 渲染**）、`cmd_reload_skills(ctx, args)`（重扫+重注册+打印 added/removed diff）、`_skill_invoke(ctx, name, args)`（`build_skill_invocation_message("/" + name, args, task_id=ctx.sess.session_id)` → 打印 `⚡ Loading skill: {name}`（保留 vendor 文案，pty 断言用）→ 注入 `ctx.loop_prompt`）。
3. `src/qra/console/main.py`：`ctx.loop_prompt` 消费块（:781-783）已存在，技能消息进 loop_prompt 后自动走 one_turn——零新增。确认 `session_state.py:52` CommandContext 有 `loop_prompt` 字段（已有，批 1 建）。
4. 菜单/补全：技能注册进 `_COMMANDS` 后 `menu_items`/`complete` 自动生效，零代码。

**测试**：
- 单测：fixture 技能目录（临时 HERMES_HOME/skills 放一个 SKILL.md）→ `_register_skills()` 后 `/fixture-skill` 可 dispatch；叠加语法 2+1 拆分正确；冲突名跳过；`parse_input("/skill-a /skill-b do X")` 三分流正确。
- pty 断言（命令 pty 层）：`/skills` → 含技能名与描述；输入技能命令 → 含 `⚡ Loading skill:`。

**验收**：单测全绿 + 七层门禁 + pty 断言 ✓ + GRAFT 8 项登记齐全。

---

## T2 /queue + /steer（评级：小，~60-80 行）

**语义**：/queue 排队一条任务（当前回合结束后作为独立回合执行，FIFO 不合并）；
/steer 向运行中回合插话（下个工具调用结果后送达）；无运行回合时 steer 等价 queue。

**vendor 照抄源**：

| 位置 | 内容 |
|---|---|
| run_agent.py:3294-3328 | `AIAgent.steer(text)`——线程安全（`_pending_steer_lock`，agent_init.py:845 初始化），**QRA 的 vendor AIAgent 已原生具备，零移植** |
| run_agent.py:3442-3456 | `_drain_pending_steer()` 工具批次末尾取走 |
| agent/tool_executor.py:1575-1580 | drain 调用点（顺序执行路径） |
| agent/prompt_builder.py:677-713 | `STEER_MARKER_OPEN/CLOSE` + `format_steer_marker` |
| agent/turn_finalizer.py:714-719 | leftover：回合收尾把残留 steer 交还 `result["pending_steer"]` |
| cli.py:10933-10969 | 两个 handler 的语义（对照） |
| cli.py:10292-10314 | busy 中 /steer 必须内联处理（否则回合结束后 `_agent_running` 已翻 False，steer 退化成 queue） |

**QRA 改动**：

1. `commands.py` `_register_p0()`：注册 `queue`（别名 `q`）与 `steer` 两条，handler 指 `handlers.cmd_queue/cmd_steer`。
2. `handlers.py`：`cmd_queue(ctx, args)` = payload 非空 → `ctx.inp.inject(payload)` + `_say` 确认；`cmd_steer` 同款（主循环只会空闲时执行到它 → 天然走「无运行回合=队列回退」分支，对齐 vendor cli.py:10967-10969）。
3. `input_layer.py`（唯一低层改动，~6 行）：`__init__` 加 `self._steer_handler = None`；加 `set_steer_handler(fn)`（与 `set_event_sink`:186 同款）；`_handle_plain` 的 `\r` Enter 分支（:644-649 附近）前插：busy 且草稿以 `/steer ` 开头 → 提取 payload 调 `self._steer_handler(payload)`，清草稿 return（**不进 `_q`**，防回合结束后被当普通 prompt 再跑一轮——这正是 vendor cli.py:10292 解决的问题）。
4. `main.py`：装配处（:695 附近）加 `inp.set_steer_handler(_on_steer)`；`_on_steer(text)` = `agent.steer(text)`（vendor 线程安全，输入线程直调）+ `events.put(("status", "steer", ...))` 走既有 status 渲染确认（零新增渲染代码）。`one_turn` 返回后（:619 return result 前）：`result.get("pending_steer")` 非空 → 打印 vendor 同款 `⏩ Delivering leftover /steer as next turn: '…'`（cli.py:15180-15184 文案）→ `ctx.inp.inject(leftover)`。input_layer 的 busy 标志是 `self._busy`（input_layer.py:110，set_busy:173）。

**测试**：
- 单测：InputLayer 假 stdin——busy 中喂 `/steer 查回测\r` → fn 收到 payload、`_q` 空、草稿清空；busy 中喂 `/queue xxx\r` → `pop()` 返回原行（自然排队）；空闲 `cmd_queue` 后 `pop()` 得 payload。agent 语义（`steer` 拼接/`drain` 清空/marker 包裹）vendor 自带测试已覆盖，不重复。
- pty 断言（命令 pty 层）：/queue 与 /steer 各 1 条——无参时打 usage 提示文本即可（离线断言，不起真实回合）。

**验收**：单测全绿 + 七层门禁 + pty 断言 ✓ + `agent/prompt_builder.py` 已入 GRAFT。

---

## T3 @file / @folder（评级：小，~40 行包装 + 2 行接线）

**语义**：输入里 `@file:路径[:L[-L2]]`（含行范围、引号路径）、`@folder:目录`（rg 递归树清单、
尊重 .gitignore）、`@diff`/`@staged`/`@git:N`、`@url:` 自动展开注入上下文；
原 @token 保留；安全三连（allowed_root 锁 cwd / 凭证 deny-list / fail-closed）与
token 预算（25% 警告、50% 整体拒绝）vendor 已全做。

**vendor 照抄源**：`agent/context_references.py` 全模块（720 行，纯函数自足）：

| 函数 | 位置 |
|---|---|
| `parse_context_references(message)` | :148 |
| `preprocess_context_references(message, cwd, context_length, ...)` | :212（同步包装，CLI 同款） |
| `_expand_file_reference` / `_expand_folder_reference` | :368 / :402 |
| `_ensure_reference_path_allowed`（deny-list） | :484 |
| 调用点参照 | cli.py:14503-14526（`[@ context: N ref(s), M tokens]` 回显格式） |

**QRA 改动**：

1. 新建 `src/qra/console/context_refs.py`（~40 行薄包装）：`expand_prompt(text, cwd) -> tuple[new_text, warnings]`，内部调 `preprocess_context_references`；`context_length` 从 config 读（`hermes_cli.config.load_config` 用法见 models_router.py:35 先例，读 `model.context_length`，读不到给保守默认值）；blocked 时返回原 text + warnings。
2. `main.py` `one_turn`（:596）函数体开头两行：`prompt, warns = expand_prompt(prompt, os.getcwd())` + warns 打印（`renderer.append_line`）。插入点选 one_turn 开头 = 交互/-z//loop 三条通道全覆盖。
   - 注意：:601 自动标题用 `prompt[:48]`——扩展后的 prompt 会把文件内容灌进标题，标题行挪到扩展**之前**取原文。
3. 三分流零改动：`@` 开头不撞 `!`/`/` 分流（commands.py:46-56/66），加回归测试锁定即可。

**测试**：
- 单测（新 `tests/test_context_refs.py`，tmp_path 夹具）：注入含 `📄` 与文件内容且原 token 保留 / 行范围只含指定行 / 目录树清单 / 缺失文件 warning / 越界 warning（allowed_root）/ 极小 context_length → blocked。三分流回归：`parse_input("@file:x") == ("prompt", "@file:x")`。
- pty 断言：`-z` 单发 `分析 @file:tmp/demo.md` → 输出含 `[@ context: 1 ref(s),` 与文件正文片段；敏感路径 `.env` → 输出含拒绝警告不含内容。

**验收**：单测全绿 + 七层门禁 + pty 断言 ✓ + `agent/context_references.py` 已入 GRAFT。

---

## T4 /retry + /undo（评级：小，~60-80 行）

**语义**：/retry 撤掉最后一组 exchange 重发最后一条用户消息；/undo [N]（默认 1）
撤掉最近 N 条用户轮（内存截断 + DB 软删 `active=0` 留审计 + `sessions.rewind_count` 递增）。

**vendor 照抄源**：

| 位置 | 内容 |
|---|---|
| cli.py:9004-9038 | `retry_last()`：`is_user_originated_turn` 倒扫 → 内存截断 → 打印 `(^_^)b Retrying: "..."` → 返回消息文本。**不碰 DB** |
| cli.py:9040-9167 | `undo_last(n=1)`：倒扫 N 条用户消息索引 → 截断 → `list_recent_user_messages` → `rewind_to_message` 软删 → agent 手术三段 → 打印 `(^_^)b Undid N turns (M messages)` |
| cli.py:9130-9153 | agent 手术三段：`_invalidate_system_prompt()` / `_last_flushed_db_idx = len(history)` / `_memory_manager.on_session_switch(..., rewound=True)`（hasattr 防护） |
| agent/context_compressor.py:7372 | `is_user_originated_turn(message)`——「真实用户轮」唯一判据 |
| hermes_state.py:9552-9637 | `SessionDB.rewind_to_message`（QRA `ctx.db` 即此实例，直调） |
| hermes_state_search.py:1097-1176 | `list_recent_user_messages`（mixin，已挂 SessionDB） |
| cli.py:10728-10751 | N 解析（ValueError 打 "Invalid count"、N<1 clamp 1） |

**QRA 改动**：

1. `commands.py`：注册 `retry` 与 `undo [N]` 两条。
2. `handlers.py`：`cmd_retry(ctx, args)`——空历史打 `(._.) No messages to retry.`（vendor 文案，cli.py:9004 同款）；倒扫+截断；`agent._last_flushed_db_idx = len(sess.history)`（hasattr 防护）；待重发文本写 `ctx.retry_prompt`。
   `cmd_undo(ctx, args)`——N 解析（vendor 同款）；倒扫截断；DB 软删（`list_recent_user_messages(session_id, limit=max(n,10))` → 第 n 条 id → `rewind_to_message`，ValueError 时保留内存撤销、debug 级跳过）；agent 手术三段；打印被撤文本 + `（/retry 可恢复重发）`。
3. `session_state.py:52` CommandContext：加 `retry_prompt: str | None = None`。
4. `main.py` 主循环：`ctx.loop_prompt` 消费块（:781-783）旁加同构块——`ctx.retry_prompt` 非空 → `one_turn(pending)`（one_turn 自动把 user/assistant 追加回 history，闭环「重发」）。

**测试**：
- 单测：假 CommandContext（test_commands.py:144 先例）——截断后 history 长度/内容正确；`/undo 0` clamp 1、`/undo abc` 不炸、空历史 `(._.)` 分支；`ctx.retry_prompt` 置位。
- pty 断言：/retry 与 /context 各 1 条离线断言（空历史 → `No messages to retry.` / `No messages to undo.`，不依赖真实回合）。
- 冒烟加步骤（可选增强）：真实两问后 /undo 1 → DB `active=0` 计数 +1。若本批时间紧可移批 4，不阻塞批 3 验收。

**验收**：单测全绿 + 七层门禁 + pty 断言 ✓ + `agent/context_compressor.py` 已入 GRAFT。

---

## T5 /context /ctx（评级：小，~30 行 handler）

**语义**：当前会话上下文窗口占用——8 类目估算（system prompt/工具 schema/规则/技能/
MCP/子代理/记忆/对话）+ 5×20 字形网格 + `Context window: used / max tokens (pct%)`；
`/context all` 展开 skills/toolsets 明细。与 /usage 正交互补（累计消耗 vs 当前占用）。

**vendor 照抄源**：`agent/context_breakdown.py` 全模块（361 行，纯函数）：

| 函数 | 位置 |
|---|---|
| `compute_session_context_breakdown(agent, messages)` | :89（主入口；total 优先 `agent.context_compressor.last_prompt_tokens` 实测值） |
| `compute_context_details(agent)` | :190（`/context all`） |
| `render_context_breakdown_lines(payload, details, grid=True)` | :325（总装输出行） |
| CLI handler（对照） | cli.py:11988-12035（`🧠 Context Usage — {model}` 头；无 agent 打 `(._.) No active agent`） |

**QRA 改动**：

1. `commands.py`：注册 `context [all]`（别名 `ctx`）。
2. `handlers.py`：`cmd_context(ctx, args)`（~30 行）——无 agent 守卫 → `expanded = args.strip().lower() in {"all", "full", "details"}` → 调 compute → 行循环 `_say`；try/except 兜底（系统提示渲染异常不炸 console，vendor cli.py:12018-12020 同款）。`ctx.sess.history` 即 messages 入参。
3. 零新状态、零 vendor CLI import（保持 handlers.py「不 import hermes_cli.main」既有约束）。

**测试**：
- 单测：MagicMock agent（vendor tests/agent/test_context_breakdown.py 的 `_make_agent` 模式：`context_compressor=MagicMock(context_length=200_000, last_prompt_tokens=50_000)`）+ patch `build_system_prompt_parts` → 断言输出含 `Context window: 50,000 / 200,000 tokens (25%)` 与类目行；`"all"` 分支不炸；plain 路径覆盖。
- pty 断言：/context（未发消息）→ 守卫行 `(._.)`；真实回合后 /ctx → `Context window:` 行（进冒烟，可选）。

**验收**：单测全绿 + 七层门禁 + pty 断言 ✓ + `agent/context_breakdown.py` 已入 GRAFT。

---

## 执行顺序

T3（最简、独立）→ T5（最简 handler）→ T4（DB 链路）→ T2（输入层拦截）→
T1（最大，放最后）。每任务完成即 commit（单独提交，标题 `console 批3-T<n>：<名>`），
全部完成后一条总结 commit 收尾文档。

## 升级信号（以下任一出现 → 停手回报设计层，不硬扛）

1. 同一处 vendor 行号与任务单描述对不上（上游漂移）→ 停。
2. 单测或门禁失败 ≥3 次尝试（blueprint 升级信号）。
3. 需要动 vendor/ 下任何文件才能继续 → 停（D002）。
4. 发现任务单未覆盖的新依赖模块 → 停（D009 面扩大）。
5. 任一任务实际改动量超过评级上界 1.5 倍 → 停（任务单可能写错了）。

## 验收总表（批 3 完成后勾选）

| 项 | 状态 |
|---|---|
| 8 个 GRAFT_PATHS 登记 + vendor_sync_log 一条 | ☐ |
| troubleshooting.md 5 个新条目 | ☐ |
| 单测全绿（预计新增 ~30 条） | ☐ |
| verify_qra.sh 七层门禁全过（命令 pty 层含 5 条新断言） | ☐ |
| T1-T5 各单独 commit + push | ☐ |
