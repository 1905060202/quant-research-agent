# D007 · 采纳 prime 式 CoT 全展示 + 持久内核工具（2026-08-14）

## 背景

雅宁提出两点改造诉求：①prime 那种"ipython 天才般设计"与对话 CoT 全展示，机理是什么、要落地；②Claude Code 源码泄露后社区逆向出的核心设计，要深度学习改造。三路研究完成（`docs/机理研究_prime与CC逆向_2026-08-14.md`）。

关键事实：
- hermes 的 thinking 数据链**已完备**（DeepSeek anthropic 兼容端点的 thinking_delta 原样到达 `_fire_reasoning_delta`，落盘 state.db 三列），缺的只是显示层——`-z` 模式全部丢弃。
- `AIAgent` 是根模块公开类，被三个官方前端程序化驱动（oneshot.py 即先例）→ **零 vendor 改动可接管显示层**，D002 保得住。
- prime 的机理：单 ipython 工具 + 持久内核（dill 快照）+ CoT 一等公民（归一化 thinking_delta、默认全展开、折叠 recap=最后加粗标题、全量落盘回放）。

## 决定

### Phase 1 · qra_console（prime 式 CoT 全展示终端）

新命令 `qra console`（Rich Live 实现）：
- import AIAgent 自建循环（照抄 oneshot 最小参数集），注入 `stream_delta_callback` + `reasoning_callback` + 工具回调，`quiet_mode=True`
- CoT **默认全展开**实时渲染，`T` 键折叠（recap=推理最后 `**加粗标题**`）；thinking 用暗色独立主题
- 逐 token 流式正文 + 工具调用/结果块（时长、stdout/stderr）+ footer 成本统计（**显示**，量化场景反 prime 品牌选择）
- 处理 `stream_delta_callback(None)` 回合结束信号；回调线程安全入队
- 不复制 oneshot 的 `os._exit`；自建循环正常清理

### Phase 2 · qra_python 持久内核工具

prime ipython 设计落地为**插件工具**（非替换现有工具）：
- jupyter_client + ipykernel 会话级内核（Python 侧不必自实现 ZeroMQ/HMAC 协议）
- 工具 schema 仅 `{code}`；变量跨轮存活；dill 快照 + resume 复活
- 用途：模型可写代码计算指标/跑回测——量化场景的真实增益

### Phase 3 · CC 设计嫁接（低-中成本项）

1. 会话双轨：state.db 之外落 append-only JSONL transcript（消息+工具调用），支撑审计复盘（QRA 验证文化）
2. Hooks 事件化：基于 hermes plugin stream hooks 扩展 PreToolUse/PostToolUse 事件 + 正则 matcher + exit 2 阻断（CC 设计清单第 5 项，最值得抄）
3. 工具 schema token 预算复审

### 不采纳（详见机理文档 4.2）

单工具哲学替换（结构化工具是 bench 可验证性根基）；提示词分段缓存（动 vendor，违 D002）；Bash AST 安全（无 bash 工具）；权限引擎重写（hermes 已有审批体系）；daemon 拓扑（无后台常驻需求）；autoCompact（依赖 CC 专有 API 特性）。

## 影响

- vendor 零改动，D002 续保
- 新增：`src/qra/console/`（TUI）、`.hermes/plugins/qra_python/`、`qra_hooks` 雏形
- bench 可扩展：内核工具题（如"写代码算 RSI 并回答"）
- 面试叙事素材：机理文档可对外公开（无敏感信息）

## 验证

qra_console 端到端 ×3（CoT 全展示/折叠 recap/工具块）；qra_python 四级（执行→跨轮变量→dill 恢复→bench 题）；JSONL 双轨抽查。
