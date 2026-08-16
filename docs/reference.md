# 参考手册

> 权威源：命令、工具、API、环境变量的速查表。**与代码同步义务**：改工具参数、
> 命令面、env 后必须改本表（check_docs.py 只查链接不查内容，内容一致性靠自觉，
> 所以本文件尽量「少写字、多给指针」，细节以代码注释为准）。

## 一、qra 入口

安装（一次性）：`ln -s "$(pwd)/bin/qra" ~/.local/bin/qra`（或 /usr/local/bin）。

| 用法 | 行为 | 真实执行 |
|---|---|---|
| `qra` | 直接进系统（CoT 全展示多轮交互） | scripts/qra_console.sh |
| `qra console` | 同上（显式） | scripts/qra_console.sh |
| `qra console -z "问题"` | 单发（CoT 全展示） | scripts/qra_console.sh |
| `qra -z "问题"` | 传统单发（只回显最终答复） | scripts/run_qra.sh |
| `qra sync` | 同步 hermes 上游（默认 full） | python -m src.qra.vendor_sync |
| `qra sync prime report` | prime 本质源预检（diff 报告） | 同上 |
| `qra sync dsh` | dsh 本质源同步 | 同上 |

`qra sync` 完整参数：`qra sync [upstream] [mode]`，upstream ∈ {hermes, prime, dsh}
（缺省 hermes），mode ∈ {full（默认）, apply, report}。prime/dsh 是 essence 源：
只钉针 + 报告，嫁接面命中 → 人工审 diff 判重移植（D009 §7，`docs/vendor_sync_log.md`）。

## 二、console 命令面（15 个 /命令 + ! 直达）

交互中输入 `/help` 看全表。分类与要点：

| 命令 | 类别 | 行为 |
|---|---|---|
| /help（/h,/?） | 会话 | 打印全部命令 |
| /resume（/r） | 会话 | 无参列表选号；有参=数字/id/标题恢复会话（内核快照自动复活） |
| /sessions（/ls） | 会话 | 列最近会话 |
| /clear（/new） | 会话 | 新会话（保留当前路由与 yolo，不 reset 审计行） |
| /compact（/compress） | 会话 | 强制上下文压缩（defer 模式） |
| /export（/e） | 会话 | 导出当前会话 md/jsonl → HERMES_HOME/exports/ |
| /model（/m） | 系统 | 双路由切换 deepseek-v4-pro ↔ opus@127.0.0.1:8789（原地换客户端+失败快照回滚） |
| /yolo | 系统 | session 级审批开关（默认开；off 时 ! 可交互审批，agent 工具 fail-closed 拒） |
| /usage（/cost） | 系统 | token/花费/credits |
| /status（/st） | 系统 | 会话摘要（id/路由/yolo/时长） |
| /memory（/mem） | 系统 | $EDITOR 打开记忆文件（编辑前还原 termios） |
| /loop | 系统 | 自动继续：每轮以 last prompt 重跑（CC 对齐，Ctrl+C 退出） |
| /fold（/f） | 显示 | 折叠块管理：无参列块表，带序号切换折叠（鼠标点击折叠行等效） |
| /mouse | 显示 | 鼠标捕获开关：on=点击折叠行展开（原生拖选复制/滚轮失效）；**默认关** |
| /agents | 显示 | 本进程子代理快照：状态/角色/模型/耗时（delegate_task 类工具） |

**! 直达 shell**：`! git status`——与 terminal 工具同门同黑名单（yolo on 自动放行），
输出**不进模型上下文**（零 token、零角色污染，vendor 裁决）；120s 超时；交互命令
（vim 等）另开终端。

**输入层（D011）**：输入 `/` 即弹候选面板（↑↓ 选择、过滤、Esc 关闭、Tab 补全，
**Enter=选中即执行**）；←→/Home/End 光标移动、行中插入删除；↑↓ 历史 /
大块粘贴确认（4096 字节且 <200ms 触发）；回合中打字回显静音、回合结束一次补画
（流式期间打字不再崩终端）。回合异常落 `HERMES_HOME/logs/console_errors.log`。

## 三、工具面（模型可见的 6 个工具，toolset="qra"）

| 工具 | 参数 | 行为 | 诚实性 |
|---|---|---|---|
| qra_quote | symbol（600519 或 sh600519） | 新浪实时行情（价/涨跌/量） | 标注数据新鲜度 |
| qra_signal | code 或 top_n（默认10），path 可选 | 猎豹 v2.1 信号摘要 | 池外代码如实报错 |
| qra_kb_fts | query，limit（默认5），path 可选 | 方法论文档 FTS5 trigram 检索 | 命中片段+出处 |
| qra_sync | upstream（hermes/prime/dsh），mode（full/apply/report） | 上游同步（D009） | 输出 diff/门禁结果 |
| qra_verify | action（add/check/list 等），claim_text/task/… | 声称账本 + 4 类确定性检查 | 不过会被回合末守卫拦 |
| qra_python | code（Python 代码） | 会话级持久内核执行，60s 超时 | 返回 ok/error/stdout/stderr |

## 四、qra_runtime（内核内预装运行时，prime 完全体）

内核代码 `import qra_runtime` 即可用。**这是模型在内核里写代码时的契约面。**

```python
import qra_runtime as qra

# 递归子代理：admission 即返回句柄（.qra_child_id），轮询取结果
handle = await qra.run("子任务提示")
final = await qra.subagent_result(handle)      # QraSubagentResult(status/summary/error)
await qra.list_subagents()                     # 找回句柄（丢失时）

# harness 文件店：12 类 CRUD，global_=True 跨会话持久
qra.harness.create_memory("id", "内容", global_=True)
qra.harness.update_skill("id", "内容")         # create/update/delete ×
                                               # memory/skill/subagent/prompt_note
qra.harness.record_refinement(...)             # 精炼事件
qra.harness.overview()                         # 全景

# 消息：发给宿主（父代理）
qra.agent_message.send("已完成，结果…", receiver_role="parent")
# 收件箱在 $QRA_INBOX_DIR，宿主侧可用 glob 读

await qra.find_models()                        # 可用模型
_qra_save()                                    # 请求立即快照落盘
```

宿主侧对应机制：iopub comm 路由（空 parent_header 先于 msg_id 过滤）→
subagent 注册表 → hermes subagent_lifecycle 接纳 → 轮询完成。快照 debounce
15s+30s；死内核自动重启+快照复活；审计 jsonl：`kernel_history/{sid}.jsonl`。

## 五、技能（SKILL.md）

| 技能 | 入口 | 流水线 |
|---|---|---|
| qra_daily | `scripts/run_qra_daily.sh`（--resume 续跑） | 信号→行情→方法论→记忆→撰写→验证卡（6 步，3 条可验证预测） |

## 六、环境变量

| 变量 | 说明 | 谁设置 |
|---|---|---|
| ANTHROPIC_TOKEN | API key（第一优先） | 用户或入口脚本（从 ~/.claude/settings.json 提取） |
| ANTHROPIC_BASE_URL | 默认 https://api.deepseek.com/anthropic | 入口脚本 |
| HERMES_HOME | 默认 $PWD/.hermes（QRA 运行时全部状态在此） | 入口脚本 |
| HERMES_ENABLE_PROJECT_PLUGINS | =1 启用 .hermes/plugins | 入口脚本 |
| HERMES_INTERACTIVE | =1 终端交互标志（sudo 提示等路径） | console 构建时设置 |
| HERMES_ACCEPT_HOOKS | =1 放行 hooks（同 vendor REPL 行为） | console 构建时设置 |
| HERMES_SESSION_KEY | 当前会话键（approval 经此认会话） | /resume /clear /yolo 时同步 |
| ~~HERMES_YOLO_MODE~~ | **已废弃**：import 时冻结，运行期无效——用 /yolo（session 级） | 勿用 |
| QRA_MEMORY_DB / QRA_VERIFY_DB | bench 会话隔离用（指向 bench/isolation/） | bench 运行器 |
| QRA_KERNEL_SID / QRA_SESSION_DIR / QRA_HARNESS_STATE_DIR / QRA_INBOX_DIR / QRA_RUNTIME_PATH / QRA_AGENT_DIR | 内核注入（qra_python _spawn 显式设置，勿改） | 插件 |

## 七、配置文件（.hermes/config.yaml，入库）

| 段 | 内容 | 注意 |
|---|---|---|
| model | default=deepseek-v4-pro，provider=anthropic | 裸名写法（厂商前缀会 400） |
| plugins.enabled | qra / qra_verify / qra_refine / qra_python | qra_memory 是 memory provider 不在此 |
| memory.provider | qra_memory | |
| approvals.timeout | 60 | /yolo off 时 fail-closed 兜底 |
| model_overrides.anthropic.opus.context_window | 1000000 | /model opus 的 30s→2s 短路（跳过 models.dev 网络探测） |
