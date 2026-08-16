# 已知坑与解法

> 权威源：这个仓库迭代极快，坑都沉淀在这里。**遇到新坑先加条目再修**——
> 症状、根因、解法三件套，每个条目标注出处（vendor_sync_log #N / ADR / 记忆）。

## 运行时可见问题

| 症状 | 根因 | 解法 | 出处 |
|---|---|---|---|
| 插件加载成功但 qra_* 工具在 console/-z 全部不可见 | hermes cli 平台默认工具集 = `_get_platform_tools({}, "cli")` 的 18 个内置集，**不含插件注册的 toolset**；QRA 插件全系注册在 "qra" 集 | console main.py / run_qra.sh 默认集 ∪ {"qra"}；新插件必须注册 toolset="qra" | vendor_sync_log #11 |
| 内核 harness 全局店写进真实 ~/.hermes（污染宿主目录） | jupyter_client `env` 参数**不合并** os.environ，HERMES_HOME 没进内核 | `_spawn` 显式 `**os.environ` + 钉死 QRA_AGENT_DIR；测试断言守护 | vendor_sync_log #9 |
| /model opus 切换挂死 ~28s | get_model_context_length 走 models.dev 网络探测，opus 代理不可达时阻塞 | config.yaml model_overrides.anthropic.opus.context_window=1000000 短路（与 models.dev catalog 一致） | config.yaml 注释 |
| 内核代码首轮报 AttributeError/TypeError 后第二轮成功 | 模型对 qra_runtime 的 handle 用 `.get()` 当 dict 用（实为 dataclass）；模型自愈是预期行为 | 工具描述已写明 `handle.qra_child_id`；判定链只看终态 | D007 P2.5 |
| `qra` 报「找不到 ANTHROPIC_TOKEN」 | ~/.claude/settings.json 无 env.ANTHROPIC_AUTH_TOKEN 或 jq 缺 | `export ANTHROPIC_TOKEN=...` 或用 run_qra.sh 的提取逻辑排查 | 脚本头注释 |
| hermes 启动告警 state.db WAL-reset / linked SQLite 旧 | 本机 SQLite 3.50.4 有 WAL 损坏 bug，hermes 自动降级 journal_mode=DELETE | 无害告警；按提示 `hermes doctor` / 升级 SQLite 3.51.3+ | hermes 上游 |
| `qra sync` 报 vendor 目录缺失/无 .git | vendor/ gitignored，新环境没克隆 | 按 `docs/vendor_sync_log.md` 钉针重建（hermes-agent 11c5aae；prime/dsh 见各 VERSION） | D009 |
| console 输出大量重复、不自动滚动、流式期间打字终端崩 | rich Live 全帧重绘 + 两个不同步 tty 写入者字节插进转义序列中间（CSI 状态机卡死） | D011 追加式渲染（已定型内容只印一次）+ TermIO 单写入者串行化；回合中输入回显静音 | D011 |
| 终端里无法拖选复制输出、无法滚轮翻页 | 鼠标捕获（`?1000h`）常开吞掉原生选择与滚轮 | 默认关；`/mouse on` 显式开（iTerm2 按住 Option 可临时拖选） | D011 |
| 输入 /help 回车不执行（只补全不跑） | 旧版斜杠菜单 Enter 只应用草稿不提交 | D011：菜单 Enter=选中即执行；Tab 只补全 | D011 |
| 回合报错后 console 假死、错误现场拿不到 | 渲染线程无异常兜底 + 终端 raw 模式残留 | 渲染/主循环三层兜底 + `HERMES_HOME/logs/console_errors.log` 落盘（traceback 全文） | D011 |

## 测试与冒烟框架的坑（都是框架 bug，产品代码一直正确）

| 症状 | 根因 | 解法 | 出处 |
|---|---|---|---|
| 冒烟脚本挂死在收尾（/quit 后不退出） | 先 stop drain 线程再写 pty：console 早死时 pty 缓冲写满**永久阻塞** | 顺序：先写 /quit → 停 drain → kill → waitpid（容错 ChildProcessError） | vendor_sync_log #11 |
| e2e 冒烟假阳性：模型思考文本让「终态字样」regex 命中，84s 假 hit | 模型读了冒烟脚本，思考里直接引用 **docstring 字面串**——脚本把「答案」写给了模型 | docstring 去掉可背诵组合；判定升级「真实 32 位 hex CHILD_ID + 终态同现」；PROMPT 禁 qra_python 以外工具 | D007 P2.5 条目 10 |
| 六层门禁全绿但真实 console 工具不可见 | qra_python 38 用例直测模块函数、**绕开 register()/插件发现** | 凡新插件必有一条真实入口 e2e（冒烟）兜底 | vendor_sync_log #11 |
| pty 双向互锁（写不进去也读不出来） | 测试父子进程同时读写 pty 互等 | 常驻 drainer 线程先起后写 | vendor_sync_log #5 |
| 冒烟 marker 撞车（"最近会话"出现在 /help 里） | 断言关键词不是唯一 | 改重边框表头 ┃ + 按次数等 | vendor_sync_log #5 |
| 表格提取循环不 break 被最后一行覆盖 | 提取循环缺 break | 命中即 break | vendor_sync_log #5 |
| check_db 无序 next() 命中错误表 | 真表 PK 名与预期不符（session_model_usage vs sessions） | 按表名过滤再取 | vendor_sync_log #5 |
| 快照模板 KeyError：'{}' 字段名 | set 字面量 `{}` 未转义被 .format() 当占位符 | 转义 `{{}}` | 记忆 P2 踩坑 |
| ipykernel 执行结果断言带引号不符 | str 型 execute_result 的 text/plain 是 repr（加引号） | 断言对齐 repr 口径 | 记忆 P2 踩坑 |
| zmq 收线程自毁 | RCVTIMEO 设了接收侧，超时即自杀 | SNDTIMEO 只设发送侧 | 记忆 P2 踩坑 |
| 执行报 raise 被误报为 ok | shell reply 队列错位 | iopub 权威 + shell reply 仅兜底 | 记忆 P2 踩坑 |

## 开发过程中的纪律性教训（不修代码，改习惯）

1. **溯源优先**：凭记忆写代码=返工（本项目与 MES 项目反复验证）。改任何文件前
   grep 现有实现与 vendor 母本。
2. **接口识别看签名不看描述**：描述相同 ≠ 同一接口（MES 生产事故教训）。
3. **校验在写之前**：任何拦截/校验必须放在数据变更之前，副作用不可逆。
4. **验证 2-3 次通过才说完成**：没有证据的「应该没问题」不算数——门禁、冒烟、
   文件证据至少两样。
