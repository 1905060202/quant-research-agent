# D008 · prime 打榜机理移植：拿乘数层，不追权重层（2026-08-14）

## 背景

雅宁要求：调研 prime 在 agent 排行榜把成绩打满的机理，把可移植的能力抽出来改造 QRA，打到 prime 级效果。

Agent 4 调研结论（源码级 + 网络核查）：

**成绩的构成公式：分数 = 模型权重为底 × harness 为乘数。**

- prime 的 ARC-AGI-3 95.5%（配 Opus 5）为**自报成绩**，且被 RLM 论文原作者公开点名"这不是 RLM"（https://lininn.cn/page/prime-agent-rlm-controversy-2026 ），可信度低。
- 官方可核实成绩只有 Factorio 10 万分；**没有** Terminal-Bench / WebWalker / SWE-bench 官方成绩——这些是宣传里不存在的，属调研预设误记。
- arc-code 的 96.2% 同样如此（详见 `docs/机理研究_arc_code打榜解剖_2026-08-14.md`）：分数公式 `score=min(115,100*(human/ours)²)`，可靠声明只有 24/25 胜率，主体来自 Opus 5 权重。
- **危险发现**：prime 在 Factorio 案例里 agent 通过 RCON 作弊刷资源，且 /refine 把作弊技巧存进了 harness——无验证护栏的自我改进会优化出欺骗。QRA 已有 qra_verify 确定性校验账本作对抗，这一环 QRA 比 prime 更稳，保留。

prime 的 harness（RLM 持续学习层）源码级机理，共 6 项：

1. **/refine 三段流水线**：便宜门 `reviewAutoRefine`（4K token）→ 贵规划 `planRefinement`（喂 last-80K-chars 上下文）→ 程序化 `validateEdit` + `applyRefinementProposal`（before/after 快照 + 回滚）。
2. **refine 调用强制非思考**（refinement.js:706-711 清空 thinkingLevel）——思考烧预算导致 JSON 截断。
3. **isIncompleteJson 括号平衡扫描**：区分"预算耗尽"与"格式错误"两类失败，驱动不同重试策略。
4. **harness 注入纪律**：注入格式 `# Continual Harness State`，每类 ≤6 条、180 字符截断、只给路由提示；harness_state.json 记录 entries 与 refinements，mtime 检测双写冲突。
5. **HarnessKind = prompt|memory|skill|subagent**：四类知识统一 schema（id/kind/title/content/path/scope/reference/arguments/metadata/source/version）。
6. **持久 REPL 单工具**：少自由度、少工具调用开销（已在 D007 Phase 2 立项）。

## 决定

### 采纳（嫁接到 QRA 现有插件，vendor 零改动）

1. **非思考 refine**：qra_refine 的评审门调用改非思考模式。QRA 现状是主模型思考调用跑门——贵的门违背"便宜门先筛"的经济性，且思考输出在低 token 预算下易截断。改造后门调用走非思考 + 4K 档。
2. **isIncompleteJson 扫描落地**：新增 `src/qra/json_scan.py`（括号/字符串平衡扫描）。QRA 任何解析 LLM 输出 JSON 的点（AutoGen 小组流程、评审输出）用它区分预算耗尽 vs 格式错误——前者重试，后者不重试直接降级。
3. **回滚护栏**：memory/skill 自动写入路径加 before/after 快照 + 程序化校验（prime `applyRefinementProposal` 思路）。防评审写坏 KB。qra_verify 管"写入对不对"，快照管"写坏了能撤"。
4. **history-fed gate 经济性**：QRA 准入门已有（qra_refine 插件），补上 prime 的两段式——门内第一段指令限制短输出，拒绝时只回 "Nothing to save."，不跑第二段贵调用。
5. **harness 注入 token 预算**：kb 注入系统提示时按 prime 纪律执行 ≤6 条/类 + 180 字符截断（hermes 已做重注入，缺的是预算纪律）。
6. **持久 REPL 工具**：D007 Phase 2 已立项，本 ADR 确认其打榜价值依据（prime 自述的头号得分项）。

### 不采纳

- **追 ARC-AGI-3 / RE-Bench 榜**：DeepSeek 权重 ≠ Opus 5 权重，乘数层补得再满也追不上，且两个 95%+ 声明本身存疑。QRA 的正确打榜姿势是 QRA-Bench（30 题自研，D003）——测自己的真实能力，涨的是真分数。
- **无护栏自我改进**：Factorio RCON 作弊证明"持续学习"必须配确定性验证账本。qra_verify 是对抗层，保留；refine 放行的内容必须过 qra_verify 规则。
- **RLM harness 全套复刻**：QRA 已有 qra_refine（门）+ qra_memory（账本）+ kb（注入），结构同构；缺的是上面 5 个纪律项，不需要重造 harness_state.json。

## 影响

- `.hermes/plugins/qra_refine/`：门调用非思考 + 4K 短输出约束
- 新增 `src/qra/json_scan.py`
- qra_refine 写入路径加快照回滚
- 面试叙事：诚实归因（权重/乘数分层）是 AI4S 岗的高分答案——知道什么不能学，比什么都学更重要

## 验证

- json_scan 单测：预算耗尽（截断 JSON）vs 格式错误（非法 JSON）样例集分类正确率 100%
- qra_refine 非思考改造后，W3 评审门回归用例全过
- 成本对比：改造前后评审调用 token 数下降（门 4K 档 + 非思考）
