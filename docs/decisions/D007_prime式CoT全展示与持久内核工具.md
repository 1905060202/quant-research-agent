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

## P2 落地记录（2026-08-16）

`.hermes/plugins/qra_python/` 已落地并通过门禁第 6 层（20 用例 ×2 连续绿）。范围变化与新增事实：

1. **prime 源码深挖后全量吸收 12 项 A 级机制**（逐变量快照/256MiB+原子替换/快照时过滤/marker-line 协议/防遮蔽 _b 别名/恢复顺序契约/恢复名单注入/JSON manifest/dispose 最终 flush 5s 上限/busy-interrupt 500ms×5s/allow_stdin=false/NO_COLOR），详见 `docs/机理研究_prime源码深挖与dsh接插件评估_2026-08-16.md` 1.2 表。
2. **快照 debounce 保留 QRA 参数（15s+30s）**：prime 是 1500ms 无间隔，量化内核常驻大 DataFrame，每笔执行序列化代价不可接受——这是刻意的分歧，已文档化。
3. **死内核自动重启+快照复活是 QRA 增强**（prime 无重启，restart() 是死代码）；执行中死亡 5s 探活检测。
4. **定位升级为全生命周期计算底座**（雅宁 2026-08-16 指令③）：工具描述去 quant 化——算指标/回测/数据处理/通用实验都行，「把复用逻辑写成函数留在内核里」是首要模式。prime 实证（26h/1229 调用全走单内核零重启）支撑该定位。
5. **安全边界诚实声明**：宿主用户权限 + workspace 目录隔离，非沙箱（与 prime 一致）；已有 execute_code/terminal 同权，风险记录不新增面。
6. **审计 jsonl**：每笔执行落 `kernel_history/{sid}.jsonl`（P3 JSONL 双轨的前置数据）。
7. **缺口：/loop**（雅宁指令④）：console 现有 /help /resume /sessions /clear /export /usage /status /model /memory /compact /yolo，**无 CC 的 /loop（自动继续模式）**。P1 立项：进程内调度器实现（空闲阈值后自动以 last prompt 继续），不依赖 cron。
8. **P1 滚动**：dsh 精华吸收两项立即项（fail-loud 启动自检、配置 schema 硬校验）+ 内核内 bootstrap 辅助函数（prime rlm 简化版）。
9. **P2.5 完全体移植（2026-08-16 雅宁拍板：「极简 bootstrap 不足以支撑工业实践，把 prime 完全体拿过来做些改造」）**：
   - 极简 bootstrap（仅 _qra_save）作废，qra_runtime 完全体落地：`host_request` comm 桥（control 通道回执、type-last 防劫持）、`harness` 文件店（12 CRUD + 快照恢复 + 全局/会话双店）、`agent_message` 收件箱、`qra.run` 递归子代理（admission 语义 + `subagent_result` 轮询——QRA 增强，hermes 子代理不自报）。
   - 宿主侧接线：iopub comm 路由（空 parent_header 先于 msg_id 过滤）、subagent 注册表、hermes subagent_lifecycle 接线、模型双路由（deepseek/opus proxy）。
   - 验证：qra_python 38 用例全绿（桥/文件店/消息/子代理四类）+ 六层门禁全绿。
   - 多上游同步机制（D009 §7）：vendor_sync.py UPSTREAMS 注册表——hermes=managed（自动合并+门禁）；prime/dsh=essence（钉针+diff 报告，嫁接面命中→人工 diff→重移植）。机制首秀 #10：prime 20 commits 命中 2 文件，审 diff 后判定零重移植（纯文档 + 宿主侧加固不动内核契约）。
10. **P2.5 收官（2026-08-16）——toolset 发现修复 + 真实链路 e2e 实证**：
    - 全链路易脏练习抓出老缺口：QRA 插件全系注册 toolset="qra"，hermes cli 平台默认工具集只有 18 个内置集、不含插件注册集 → qra_* 工具在 console 与 -z 均不可见（同根因）。qra_python 38 用例直测模块函数、绕开插件发现，六层门禁因此从未拦到。修复：console main.py 默认集 ∪ {"qra"}；run_qra.sh 动态解析内置集 ∪ {"qra"} 作默认 --toolsets（显式传参时尊重用户意图）。
    - 冒烟脚本 `scripts/_smoke_qra_run.py`（pty 驱动真实 console + 文件双证据）跑通 qra.run 递归链路：模型调 qra_python → 内核 await qra.run → comm 桥 → hermes 子代理 admission → subagent_result 轮询到 `status='completed', summary='2 + 2 = 4。'`（内核审计 jsonl + 会话目录双证据）。首轮模型内核代码报错后自愈（exec 2 修正 child_id 属性访问），符合「模型驱动恢复」预期。冒烟脚本自身收尾顺序 bug 同批修复（先 /quit 后停 drain）。
    - 门禁 #3 全绿（console main.py + run_qra.sh 修改后重验）。
    - 冒烟脚本两代防作弊迭代（对 bench 防作弊设计的直接输入）：run5 模型读了冒烟脚本、思考文本直接引用 docstring 字面串导致 regex 假阳性（84s 假 hit，被文件证据门正确拒绝）；run6 修复 = docstring 移除「终态字样+预期答案」可背诵组合、PROMPT 禁 qra_python 之外一切工具、判定升级为「真实 32 位十六进制 CHILD_ID + completed 同现」（思考文本凑不出真实 id）、error 判定贴 CHILD_ID 段 600 字符窗口。run6 干净通过（161s，SMOKE_RC=0，模型 6 次失败自愈后终态 completed）。教训：**评测脚本本身不能把答案写给模型**，判定不能依赖模型可复述的文本。
