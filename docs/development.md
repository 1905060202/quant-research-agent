# 开发规范

> 权威源：本文是「怎么在这个仓库干活」的唯一答案。任何流程改动先改本文。
> 给新 agent 的第一句话：**这个仓库迭代极快，坑都写在 `docs/troubleshooting.md`，
> 决策都在 `docs/decisions/`，先读再动手。**

## 一、开发工作流（一次需求的标准路径）

```
1. 读上下文    docs/README.md 找对应手册 + 相关 ADR + vendor_sync_log 最近条目
2. 溯源优先    改任何代码前 grep/read 现有实现与 vendor 母本，禁止凭记忆写
3. 定位改动面  能放插件就放插件（D002）；需要 hermes 内部模块 → GRAFT_PATHS 登记
4. 写代码 + 单测   新行为必须有测试锁定（见「测试体系」）
5. 六层门禁    scripts/verify_qra.sh（改动任何 src/ 或插件后必跑）
6. 冒烟        console 交互类改动跑 scripts/_smoke_console.py；
              内核/子代理类改动跑 scripts/_smoke_qra_run.py
7. 文档同步    架构变了改 docs/architecture.md；坑记入 docs/troubleshooting.md；
              新嫁接记 vendor_sync_log.md；重要决策写 ADR + 记忆
8. 提交推送    见「提交与推送协议」（门禁绿 + 零凭据扫描 + cc/ 同步文档）
```

**迭代极快的现实约束**：这个仓库 2026-08-14 立项、两天内 20+ 次提交、六层门禁
从四层涨到六层。所以「改完就跑门禁」不是建议是义务——快速迭代埋的 bug 都在
门禁与冒烟里被抓住过（pty 竞态、marker 撞车、toolset 不可见、env 传播……）。

## 二、铁律（优先级最高，违反 = 返工）

| # | 铁律 | 出处 | 实操 |
|---|---|---|---|
| 1 | vendor 零修改 | D002 | 新能力一律 src/qra/ 或 .hermes/plugins/ |
| 2 | 新嫁接必登记 | D009 | GRAFT_PATHS + vendor_sync_log.md 双登记 |
| 3 | 验证 2-3 次通过才说完成 | 用户铁律 | 门禁 + 冒烟 + 文件证据 |
| 4 | 零凭据 | 开源决策 | key 走 env；push 前 scan_credentials.sh |
| 5 | 溯源优先 | 反复教训 | grep 源文件确认再改，凭记忆写代码=返工 |
| 6 | 重写=从头写，禁止打补丁 | 用户铁律 | 大改直接重写文件，别叠补丁 |
| 7 | 落地行文通俗 | 用户铁律 | 文档/注释说人话，黑话只限内部推理 |
| 8 | 诚实报告 | 用户铁律 | benchmark 测真实分数，达不到也如实说 |

## 三、测试体系

### 六层门禁（scripts/verify_qra.sh）

| 层 | 内容 | 需要 | 时长 |
|---|---|---|---|
| 1 | py_compile：console 全模块 + config_guard + vendor_sync + qra_python 插件 | 无 | 秒级 |
| 2 | 单测：console 五件套 + vendor_sync 16 用例 | 无 | 秒级 |
| 3 | -z 真实 API 工具题 ×2（答案与新浪同源动态对照） | API key | 分钟 |
| 4 | console 交互 pty 竞态 ×2（回合中空行缓冲） | API key | 分钟 |
| 5 | 命令 pty 全离线：/help !echo /model /sessions /yolo /loop | 无 | 分钟 |
| 6 | qra_python 内核 38 用例（四级验证 + 机理） | 无 | ~35s |

```bash
scripts/verify_qra.sh            # 完整六层（本地日常，需 key）
scripts/verify_qra.sh --offline  # 离线层 1/2/6（CI 同款，无 key）
```

**层 3/4 的 API 调用计费**（DeepSeek 端点，单次 0.1-0.3 元量级），所以 CI 只跑
离线层。门禁历史与语义变更记录在脚本头注释。

### 冒烟（门禁的 e2e 补充）

- `scripts/_smoke_console.py`：console 命令面全链路（/clear /sessions /resume
  双路由 /yolo 大粘贴 state.db 抽查）。命令面改动必跑。
- `scripts/_smoke_qra_run.py`：qra.run 递归子代理链路，**文件双证据**判定
  （内核审计 jsonl 新 exec + 子代理会话目录），不信模型嘴说。内核改动必跑。
  两代防作弊迭代记在 D007 P2.5 条目 10——评测脚本不能把答案写给模型。

### 评测（bench/）

30 题 ×3 域（工具 T01-T10 / 知识 K01-K10 / 研究 R01-R10），动态 gold（运行时
重采行情防时间衰减）+ 机械评分（0/0.5/1）+ 幻觉双口径。口径细节见
`bench/README.md`。**改完工具面/记忆面跑一次**：

```bash
.venv-v7/bin/python bench/run_qra.py && .venv-v7/bin/python bench/scorer.py --results bench/results/qra
```

### 单测约定

- console 单测在 `src/qra/console/tests/`（test_commands / test_history /
  test_inputlayer / test_paste / test_config_guard）
- vendor_sync 单测在 `src/qra/tests/`（16 用例，全 mock 离线）
- 内核单测在 `.hermes/plugins/qra_python/tests/`（38 用例，真实内核）
- 教训：**测试必须走真实注册/加载路径**——qra_python 早期测试直测模块函数、
  绕开插件发现，导致 toolset 不可见 bug 六层门禁全绿也拦不住（见
  `docs/troubleshooting.md`）。凡新插件必有一条真实入口 e2e。

## 四、ADR 制度

「不可轻易逆转」或「影响后续一切」的决策落 `docs/decisions/DNNN_*.md`。
格式：背景 → 决策 → 备选项 → 后果。模板 `_template.md`，索引 `README.md`。
别人问「为什么这个项目长这样」，答案必须全部能从 ADR 查到。
当前 D001-D010 全部 accepted（状态字段在各自文件头）。

## 五、提交与推送协议

### 提交信息

仓库沿用的约定（照抄已有历史风格）：

```
feat(qra): 一句话说清做了什么——影响面用「+」分条

- 条目一（说行为，不说实现细节堆砌）
- 条目二

Co-Authored-By: Claude <noreply@anthropic.com>
```

scope 惯例：`feat(qra)` / `feat(qra_python)` / `feat(console)` / `docs:` / `chore:`。
一条提交一个主题，门禁绿了才 commit（commit 前最后一道门禁）。

### push 前检查单（每次 push 逐条过）

```bash
# 1. 门禁绿
bash scripts/verify_qra.sh
# 2. 零凭据扫描（见「脱敏红线」）
bash scripts/scan_credentials.sh
# 3. 文档核对（改了 docs/ 时）
.venv-v7/bin/python scripts/check_docs.py
# 4. 待提交清单目检
git status --short && git diff --cached --stat
# 5. cc/ 同步文档（每个需求完成后，CC-Hermes 同步铁律）
#    → 写入 hermes_output/cc/2026-MM-DD-qra-<需求>.md
git push origin main
```

### 脱敏红线（开源仓库，push 即发布）

1. **零凭据**：明文 key/token 永不入库——包括提交信息、注释、测试 fixture、
   文档代码块。`HANDOFF_新session必读.md` 已 gitignore（历史上曾放 key 的地方）。
2. **凭据唯一来源**：`~/.claude/settings.json` 的 `env.ANTHROPIC_AUTH_TOKEN`，
   由入口脚本自动提取（见 docs/reference.md 环境变量表）。CI 无任何 secret。
3. **扫描命令**（本地与 CI 同款，`scripts/scan_credentials.sh`）：
   对全部 tracked + 待提交文件 grep `sk-[A-Za-z0-9]{20,}` 形态。
4. **git 历史也算发布面**：若误提交凭据，仅删文件不够，必须轮换 key +
   历史处理（此事发生过一次：W9-12 检查单记录，key 已轮换）。
5. **kb_sources 已脱敏**：14 处人称替换后重灌两 KB；新增文档源先脱敏再入库。
6. **误提交的处置顺序**：先轮换 key（止血）→ 再处理历史 → 最后改代码。

### CI（GitHub Actions）

`.github/workflows/ci.yml`：push/PR 触发，跑离线层 1/2/6 + 零凭据扫描 +
文档核对。没有任何 secret、不碰 API。详见 `docs/ci.md`。
**CD 说明**：本仓库交付物=仓库本身（本地 CLI 工具），无部署目标，push 即发布，
所以 push 检查单 = 发布检查单。

## 六、成本纪律

`scripts/check_cost.py`（阈值 ¥350/周，连续 2 周超 → 停机复查，W9-12 审计遗产）。
`reports/metrics.md` 记录成功率/时延/调试记录。跑长任务（bench 全量、冒烟
迭代）后顺手跑一次周检。
