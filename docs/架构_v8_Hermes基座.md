# QRA v8 架构 · 以 Hermes 为基座的完整通用 Agent

> 2026-08-14 · 推倒重来第三版（用户三次纠正：不要简化版，要完整体）
> 核心决策：**不再自写 agent 骨架**——以 hermes-agent(230k★) 为完整基座，
> 它本身就是通用 agent（会话/记忆/技能/工具/子代理/自我进化全有）。
> QRA 只做：①嫁接 prime/DSH/Claude 的设计 ②配置 DeepSeek ③加量化研究领域层。

---

## 一、为什么以 Hermes 为基座

Hermes 是完整的通用 agent（已验证可导入）：
- `hermes_state.py` (11605行)：SQLite+FTS5 会话层（WAL+trigram+CJK）
- `agent/memory_manager.py` (1291行)：记忆管理（provider 插件+prefetch+后台写）
- `agent/memory_provider.py` (404行)：记忆 provider 抽象（8 个外部实现）
- `tools/memory_tool.py` (1248行)：记忆工具（字符预算+原子写+冻结快照）
- `agent/background_review.py` (1144行)：后台审查 fork（自我进化）
- `agent/curator.py` (2019行)：记忆/技能整理（30天stale/90天归档）
- `agent/skill_utils.py` (934行)：技能系统（SKILL.md 发现）
- `agent/learning_graph.py` (328行)：学习图谱（记忆+技能节点）
- `tools/session_search_tool.py` (1161行)：FTS5 会话搜索
- `agent/conversation_loop.py`：对话主循环（完整 agent 运行时）
- `tools/async_delegation.py`：子代理委托
- 原生支持 DeepSeek（`_is_deepseek_anthropic_endpoint`）

## 二、v8 架构分层

```
┌──────────────────────────────────────────────────────┐
│ QRA 领域层（我们写的胶水+领域技能）                     │
│  · skills/quant_*：行情/知识库/验证卡/日报 技能        │
│  · domain/：量化研究领域逻辑（信号→日报→验证）          │
├──────────────────────────────────────────────────────┤
│ 嫁接层（prime/DSH/Claude 设计）                       │
│  · bridges/prime_refine.py：双评审门+回滚（prime）     │
│  · bridges/dsh_events.py：事件三分域（DSH）            │
│  · bridges/claude_contracts.py：frontmatter 契约      │
│  · bridges/kernel_state.py：dill 内核记忆（prime）     │
├──────────────────────────────────────────────────────┤
│ Hermes 基座（完整通用 agent·230k★）                    │
│  · 会话层 SQLite+FTS5 · 记忆系统 · 技能系统            │
│  · 工具注册表 · 子代理 · curator · learning_graph      │
│  · conversation_loop 主循环                           │
├──────────────────────────────────────────────────────┤
│ 运行时：DeepSeek (anthropic 端点) · uv venv           │
└──────────────────────────────────────────────────────┘
```

## 三、嫁接设计（prime/DSH/Claude → Hermes）

### 3.1 prime 嫁接（自我迭代）
- Hermes 有 learning_graph + background_review，但缺 prime 的"双评审门+回滚"
- 嫁接：`bridges/prime_refine.py` 把 prime refinement 的评审门逻辑挂到
  Hermes 的 background_review 之上（先评审再写入+before/after 快照可回滚）

### 3.2 DSH 嫁接（事件架构）
- Hermes 有自己的事件系统，嫁接 DSH 的三分域语义：
  - 会话事件=事实日志（Hermes SQLite 已是）
  - agent 事件=实时通道（Hermes 已有 hooks）
  - tools 事件=工具管道（Hermes tools/registry 已有 pre/post 概念）
- 嫁接：`bridges/dsh_events.py` 提供 EventBus 兼容层，让 DSH 式插件能挂进 Hermes

### 3.3 Claude 嫁接（文件契约）
- Hermes 技能已是 SKILL.md 格式（与 Claude 兼容）
- 嫁接：`bridges/claude_contracts.py` 支持 frontmatter 契约（name/description/触发条件），
  让技能可以"描述即路由"

### 3.4 prime kernel-state 嫁接（内核记忆）
- `bridges/kernel_state.py`：dill 序列化 IPython 命名空间（已实现）

## 四、QRA 领域层（量化研究）

skills/ 目录（Hermes SKILL.md 格式）：
- quant_quote：行情查询技能
- quant_kb：知识库检索技能（FTS5）
- quant_verify：验证卡/事实核查技能
- quant_report：日报生成技能

domain/ 目录：
- signals.py：信号读取
- report.py：日报组装
- verify.py：验证卡

## 五、实施顺序

1. ✅ Hermes 基座安装验证（.venv-v7）
2. ⬜ 配置 DeepSeek + 启动 Hermes 会话
3. ⬜ 嫁接 prime_refine（双评审门）
4. ⬜ 嫁接 dsh_events（三分域）
5. ⬜ QRA 领域技能（quant_*）
6. ⬜ 全量测试（QRA-Bench + 通用能力）
