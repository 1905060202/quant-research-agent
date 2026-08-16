# QRA 项目指令（每个 session 自动注入）

本仓库是 QRA（量化研究智能体）。你在本仓库干活时，以下规则优先于通用习惯。

## 内核优先路由铁律（最高优先级）

> **凡是「算」的（写 Python 处理数据、算指标、跑回测、做实验），走 `qra_python` 持久内核；
> 凡是「读/写盘」的，走 `read_file` / `search_files` / `write_file`。**

- 连续多步计算必须留在内核里复用状态：第一步定义的变量/函数，第二步直接引用。
- **禁止**把需要跨轮复用的连续计算拆成 `execute_code` 一次性脚本重算——`execute_code`
  每次调用是新解释器，变量不跨调用存活，这正是内核要补的短板。
- 反模式示例（不要这样）：第一轮 `execute_code` 算 DataFrame 存 CSV，第二轮
  `execute_code` 读 CSV 重算。正解：两轮都走 `qra_python`，第二轮直接用第一轮的内存变量。

## 内核使用范式

- 调用：`qra_python({"code": "..."})`——一段 Python，返回 JSON（ok/error/stdout/stderr/result）。
- 变量和函数在内核进程里跨调用存活（会话级 Jupyter 内核）。
- 把可复用逻辑写成函数留在内核里（这是首要模式，prime 实证 26h/1229 调用零重启）。
- 需要立刻落盘时调用 `_qra_save()`（平时 15s debounce + 30s 最小间隔自动 dill 快照，
  会话 /resume 后自动复活，复活名单会如实告知）。
- 内核里预装 `qra_runtime`：
  - `await qra_runtime("子任务提示")` 派生子代理（admission 即返回句柄）；
  - `qra_runtime.harness` 持久 CRUD 店（memory/skill/subagent/prompt_note/refinement，
    `global_=True` 跨会话持久）；
  - `qra_runtime.agent_message.send(message, receiver_role='parent')` 给父代理发消息；
  - `qra_runtime.find_models()` 查可用模型。
- 执行超时 60s（超时被中断并如实报错）；快照上限 256MiB；同时最多 2 个活内核
  （LRU 驱逐，被驱逐的有快照可复活）。

## 边界（别过度矫正）

- 内核**不是**沙箱：宿主用户权限 + `$HERMES_HOME/qra_python/workspace` 工作目录隔离。
- 读文件/搜代码/写文件不是内核的活——文件系统是「跨会话记忆」，内核是「活的数值状态」。
- 超长任务（>60s）或超大状态仍应拆分，或走 terminal 后台，不要为「走内核」而走内核。

## QRA 专职工具速查

| 工具 | 用途 |
|---|---|
| `qra_quote` | A 股实时行情（新浪源，现价/涨跌幅/量额） |
| `qra_signal` | 猎豹 v2.1 信号快照（市场温度/regime/HOT-COLD 榜/模型 IC/个股排名） |
| `qra_kb_fts` | 知识库全文检索（FTS5 trigram，中英文） |
| `qra_sync` | 同步 hermes 上游到 vendor（拉取→嫁接面核对→快进→门禁） |
| `qra_verify` | 验证闭环：声称登记账本 + 确定性检查（行情/文件/区间），不过会被回合末守卫拦下 |
| `qra_python` | 持久内核（见上） |
