
# QRA 重构蓝图 v3 —— 从 prime-agent / Claude Code 源码吸收的设计

## 精读对象
1. prime-agent/dist/core/skills.js —— SKILL.md 发现+python技能检测
2. prime-agent/dist/utils/frontmatter.js —— frontmatter 协议
3. prime-agent/dist/prime-agent-runtime/src/rlm/harness.py —— 四层状态
4. claude-code/sdk-tools.d.ts —— 工具调用类型契约

## 吸收的 8 个设计（含源码证据）

### D1 · Skill 发现协议（skills.js + frontmatter.js）
- 机制：目录含 SKILL.md → 视为技能根；frontmatter = YAML（name/description 校验：name≤64、desc≤1024）
- python 技能检测：pyproject.toml + src/{import}/__init__.py 存在 → kind=python
- QRA：方法论库升级为"skills/ 目录 + SKILL.md"结构——每个方法论一个目录，可被 LLM 按 name/description 主动发现，而不是全文检索

### D2 · 工具契约（claude-code sdk-tools.d.ts）
- Claude Code 的工具类型：name + description + input_schema(JSON Schema) —— 工具调用 = {name, input}
- QRA：registry 的 args_schema 升级为完整 JSON Schema（type/properties/required），LLM 严格按 schema 填参

### D3 · 四层状态（harness.py）
- prompt/memory/skill/subagent 四 kind 分离 + local/global 作用域 + id=slug(title) 去重
- QRA：记忆升级为四层 JSON：prompt(角色规则)/memory(事实)/skill(方法论注册)/subagent(委托记录)

### D4 · 精炼回路（harness.py plan_refinement/record_refinement）
- 循环：诊断重复失败 → 更新最小组件 → 下次行动验证 → 记录 outcome
- QRA：Reflector 节点升级为"plan→update→verify→record"四步，错误沉淀进 memory/skill 而不是只写日志

### D5 · slug 归一化（harness.py _slug）
- id = title 小写+非字母数字→下划线，截断 80 —— 稳定 ID 防止重复条目
- QRA：记忆/结论用 slug 做 key，天然去重

### D6 · 状态缓存 + 磁盘同步（harness.py _state_cache/_sync_from_disk）
- 内存缓存 + 文件 mtime 检查后同步 —— 多进程安全
- QRA：memory.py 升级为"加载→变更→写回"模式 + 缓存

### D7 · 消息格式（Claude Code API）
- 消息 = {role, content}；工具结果 = {role:tool_result, content}；多轮靠 messages 数组
- QRA：LangGraph 消息已用 add_messages reducer（一致）

### D8 · 观察/审计（agent-observe）
- recent_messages(target, limit, max_chars) —— 有界观察防止上下文爆炸
- QRA：多智能体日志留痕 + 有界审计接口

## 落地优先级（按对 QRA 的价值）
P0 今天做：D2 工具契约完整 JSON Schema + D5 slug 去重
P1 明天做：D1 方法论库 skills/ 化（每个方法论 SKILL.md 注册）
P2 本周做：D3 四层状态 + D4 Reflector 精炼回路
