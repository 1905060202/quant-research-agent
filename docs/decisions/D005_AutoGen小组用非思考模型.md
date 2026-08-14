# D005：AutoGen 小组用 deepseek-chat（非思考模式）

- 状态：accepted
- 日期：2026-08-14

## 背景

JD 轨道 W3-4 三人小组首跑时，researcher 的"内心独白"整段进入 team_daily.md 落盘。
根因：deepseek-v4-flash 是 thinking 模型，OpenAI 协议下推理流混进 content 字段，
被当成结论文本。多智能体小组的每轮消息都会进入下一轮上下文，独白污染会逐轮放大。

## 决策

小组 agent 统一用 deepseek-chat（非思考模式）。LangGraph 单 agent 版保留 v4-flash
（Anthropic 协议正确处理 thinking，且单 agent 深度思考有价值）。

## 备选项

- 解析 reasoning_content 剥离：DeepSeek OpenAI 端点的 reasoning 字段能否稳定分离取决于
  客户端处理，autogen 0.7 未暴露该字段；修框架不可控
- 提示词禁止思考：模型行为不可靠约束

## 后果

- 小组结论干净落盘（实测对比实验通过）
- 时延对比有混杂因素：小组 deepseek-chat vs LangGraph v4-flash，"快 5 倍"不能归因于编排
- 小组场景牺牲单步推理深度换对话稳定性——多智能体分工本身承担了部分推理负担
