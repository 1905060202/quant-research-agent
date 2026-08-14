# QRA 指标追踪（reports/metrics.md）

> 铁律：每个 mini 项目/阶段必须记录指标——不跑 demo 当学会。
> 指标：任务成功率 / 响应延迟 / 幻觉率（幻觉率在接入 LLM 后记录）

## W1 mini① 行情工具调用 Agent（2026-08-14）

| 日期 | 用例 | 预期 | 实际 | 成功 | 延迟 |
|---|---|---|---|---|---|
| 08-14 | 查 sz159558 现价涨跌 | 返回价格+涨跌幅 | 半导体E 1.164 +0.78% | ✅ | 链路 <1s |
| 08-14 | 查上证指数 | 返回指数+涨跌幅 | 上证 3927.18 +0.01% | ✅ | 链路 <1s |
| 08-14 | 天气查询（无工具） | 优雅降级 | "后续接 LLM 节点" | ✅ | 链路 <1s |

**成功率：3/3 = 100%** · 平均延迟 <1s（本地无 LLM，仅工具+图编排）

## 记录规则
- 每次运行新用例 → 追加一行
- 每周五汇总：成功率/平均延迟/趋势
- 接入 LLM 后增加：幻觉率（答案被事实核查拒绝的比例）

## W1 mini② 带记忆对话 Agent（2026-08-14）

| 日期 | 用例 | 预期 | 实际 | 成功 | 延迟 |
|---|---|---|---|---|---|
| 08-14 | 查行情 | 返回价格+涨跌幅 | 半导体E 1.164 +0.78% | ✅ | <1s |
| 08-14 | 问方法论（多假设竞争） | 知识库检索命中 | 命中 research_methodology_v2.1 + thinking_architecture_v3 | ✅ | <1s |
| 08-14 | 记忆写入 | 记住偏好 | 已写入 memory.json | ✅ | <1s |
| 08-14 | 记忆取回 | 返回历史 | 正确返回且去重 | ✅ | <1s |

**本轮成功率：4/4 = 100%** · 知识库 503 块 · 记忆持久化 JSON

## 调试记录（真实学习证据）
1. 知识库触发词过窄（"多假设"未命中）→ 扩充意图词表
2. 检索整词匹配失败 → bigram 子串切分修复（召回提升）
3. 记忆取回被"记住"分支抢先 → 调整意图判断顺序
4. 记忆重复写入 → 加去重

## W2 · LLM Agent + 源码吸收升级（2026-08-14）

| 用例 | 结果 | 说明 |
|---|---|---|
| LLM function calling（行情） | ✅ | LLM 自主决定调 market_query |
| LLM function calling（知识库） | ✅ | 命中方法论 v2.1，回答质量显著提升 |
| 工具契约 JSON Schema | ✅ | 缺参/未知工具正确报错 |
| 四层记忆 + slug 去重 | ✅ | 同标题自动更新不重复 |
| 精炼回路 record_refinement | ✅ | refine_0001 已记录 |
| 长回答防误判 | ✅ | 修复"markdown 正文被当工具调用" |

**调试记录（本次 4 个真实 bug）**：
1. LangGraph 消息是 HumanMessage 对象非 dict → 用 getattr 兼容
2. LLM 返回 OpenAI 风格 {name,arguments} 而非自定义 → 双格式解析
3. 正则 [^{}] 不支持嵌套 JSON → 改首尾大括号+json 解析
4. 长回答被误判为工具调用 → 仅开头是 { 才提取

## 源码吸收清单（prime-agent + Claude Code）
- D2 工具契约 JSON Schema（claude-code sdk-tools.d.ts 规范）
- D5 slug 归一化去重（harness._slug 移植）
- D6 缓存+原子写（harness _state_cache/os.replace）
- D3 四层状态 prompt/memory/skill/subagent（harness.py）
- D4 精炼回路 plan/record（harness.plan_refinement 移植）
- D1 方法论 skills/ 化（skills.js 发现协议）→ P1 待做

## W2 补 · Chroma 向量检索（2026-08-14）

| 用例 | 结果 | 说明 |
|---|---|---|
| 查行情 | ✅ | LLM 调 market_query，1.164 +0.78%（数据时间 15:45） |
| 多假设竞争怎么做 | ✅ | kb_search 命中 v2.1，Phase 4.5/前检表/判断日志进回答 |
| 日报自动化起步 | ✅ | LLM 自主检索 KB，引用 3 篇文档给落地路线 |

**升级内容**：bigram 字面检索 → **Chroma 向量检索**（687 块 / 7799 维 n-gram TF 向量 / 余弦相似度 TopK）。embedding 用零下载离线方案（hf-mirror 64KB/s 不可行），机制与真实向量检索一致，语义弱于 transformer——生产换 bge 类模型（注入点已留）。

**探针**：『多假设竞争』→ 精准命中 v2.1（距离 0.400）；『叙事维度的因子溢价』→ narrative_awareness 两段召回。

**调试记录（本轮 2 个真实 bug）**：
1. max_tokens=600 截断工具调用 JSON → `{"tool": "kb_search", "` 残片被误当最终回答 → 提到 1000 + 截断残片请求续写
2. DeepSeek 偶发空响应 → 重试逻辑已在，本轮再次触发验证有效（3 次重试后成功）

## W3-4 · AutoGen 量化研究小组（2026-08-14）

| 用例 | 结果 | 说明 |
|---|---|---|
| 茅台行情+方法论理由（小组） | ✅ | analyst 真实行情 + librarian 文档检索 + researcher 汇总带来源标注 |
| 检查点落盘 | ✅ | 结论过门槛才写 team_daily.md，拒绝路径可用 |
| 对比：单域任务 | 打平 | LangGraph 5.1s vs AutoGen 4.7s，小组有冗余发言 |
| 对比：跨域任务 | 小组优 | 27.9s vs 5.3s，分工并行+角色隔离（⚠️模型不同有混杂） |

**调试记录（4 个真实 bug）**：model_info 必填 / Anthropic 端点拼 OpenAI SDK 404 / thinking 模型独白混入 content / FunctionTool 单 dict 签名接不住 kwargs
**框架契约差异**：同一批工具 LangGraph 手写 JSON 传 dict、AutoGen 按签名拆 kwargs——跨框架复用要薄包装（面试弹药）

## 源码精读记录（三源码交叉）
- prime-agent（skills.js/frontmatter.js/harness.py）→ D1-D8
- Hermes（hermes_state.py 8767行/memory_manager.py 1241行）→ D9/D10/D16/D17
- DeepSeek Harness（dsh-agent-loop/dsh-skill/dsh-subagent/dsh-spill/dsh-compaction）→ D11-D15/D18
- 详见 docs/架构蓝图_v4_三源码精读.md
