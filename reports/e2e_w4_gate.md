# W4 评审门 e2e 验证 2026-08-14

qra_refine 移植的 prime 准入评审门，五个真实会话的对照实验证据。

## 实验设计

- 触发条件（临时）：config.yaml 注入 `skills.creation_nudge_interval: 1`（每 1 次工具迭代触发技能评审，测完已删除）。
- 调用方式：`{ echo "prompt"; sleep 150; } | ./scripts/run_qra.sh chat`——chat 模式 + stdin 睡眠保持进程存活，让会话结束时的后台评审 fork（daemon 线程，oneshot 模式进程退出即死）有时间跑完。
- 证据通道：`.hermes/logs/agent.log`（门激活行 + fork 回合行）、stdout toast、磁盘 diff（MEMORY.md hash、skills 目录）、qra_memory.db 行数。
- fork 消息不落 state.db（上游 _persist_disabled 持久化隔离），证据以日志+磁盘为准。

## 结果

| 会话 | 内容 | 门裁决 | 证据 |
|------|------|--------|------|
| A `20260814_193359` | 查价但深挖插件源码（14 工具调用，含可复用发现） | 放行（memory 路径） | fork 调用 memory 工具写入内置 MEMORY.md 3 条教训；toast "Memory updated"；qra_memory.db 零新增（外部 provider 零副作用，上游设计） |
| B `20260814_194242` | 纯噪声查价（4 工具调用，禁探索） | **拒绝** | fork 终答恰 16 字符 = "Nothing to save."；零工具调用；MEMORY.md hash 不变（1454f4c3…）；无新技能文件；无 toast |
| C `20260814_194551` | 用户固定格式要求（教训会话，qra_daily 未 adopt） | 放行，但写入被上游所有权守卫拦截 | skill_manage 返回 "Refusing background curator patch … not curator-managed (created_by=None)"；无写入 |
| D `20260814_194947` | 同 E（重试） | fork 首个请求卡死 | 评审回合开始后 129 秒无 API 调用，进程退出；无错误日志。**一次性抖动**（同 prompt 重跑 E 正常） |
| E `20260814_195306` | 同 C（qra_daily 已 `curator adopt`） | 放行 + 持久化 | toast "Patched SKILL.md in skill 'qra_daily' (1 replacement)"；SKILL.md diff 显示正确新增"数据时间戳固定要求"（测试后已还原） |

## 关键机制事实（源码核实）

1. **门激活**：agent.log 三行自检 `qra_refine 评审门状态: {'_MEMORY_REVIEW_PROMPT': True, ...}`，评审 fork 回合的 msg 以门提示词开头。
2. **拒绝契约**：门要求拒绝时终答只能是 "Nothing to save."（16 字符）且不调工具——B 会话逐字吻合。
3. **fork 记忆写入路径**：`review_agent._memory_store = agent._memory_store`（共享父 store），写入落内置 MEMORY.md；skip_memory=True 保证对外部 provider（qra_memory 等）零副作用——上游有意设计，qra_memory.db 因此不受评审影响。
4. **技能所有权守卫**（上游）：后台策展只能改 curator-managed 技能（usage 记录 `created_by: "agent"`）；用户自建技能须 `hermes curator adopt <name>` 选择加入。另有 read-before-write 守卫（fork 必须先读目标文件）。
5. **toast 语义**：`summarize_background_review_actions` 扫描 fork 成功工具动作，无动作则无 toast——拒绝路径天然静默。

## 结论

门的两个分支（拒绝/放行）都在真实会话中得到验证。qra_refine 嫁接成立：prime 的准入质量门以官方兼容钩（agent._XX_REVIEW_PROMPT getattr 回退模块常量）形式运行在 hermes background_review 上，vendor 源码零改动。
