# D009: hermes 上游同步流程（pin + ff-only + 嫁接面清单 + 回归门禁）

- **日期**: 2026-08-15
- **状态**: 已采纳（首次同步执行完成，门禁全绿）
- **背景**: QRA 基底 = NousResearch/hermes-agent（vendor/hermes-agent）。上游迭代活跃（17d6a7d 之后 44 commits/56 文件）。雅宁要求："同步 hermes 的 commit，合并 diff，同时保证我们系统的稳定迭代运行"。本 ADR 定义同步的制度化流程。

## 1. 可同步性的结构性前提（已验证）

- 上游 main 是纯线性推进，**没有改写历史**（钉针 17d6a7d 是 main 的直系祖先，compare API: behind_by=0, ahead_by=44）。
- 因此同步 = **fast-forward-only**，永远不会产生 merge commit 与冲突。
- vendor/hermes-agent 从 2026-08-15 起持有真实上游历史（浅克隆 depth 会随同步滚动），`git merge --ff-only upstream/main` 随时可用。
- **禁止**对 vendor 内任何文件做本地修改；QRA 的一切改动必须在 src/qra/ 或 .hermes/plugins/ 中（D002 铁律）。这是 ff-only 永不被破坏的前提。

## 2. 同步流程（QRA 原生命令 + agent 工具）

同步能力是 QRA 自己的，不是外部脚本（2026-08-15 起）：

```
qra sync                # 完整同步：拉取→嫁接面核对→快进→VERSION→四层门禁（默认，直接拉）
qra sync report         # 只拉取+核对，不落地（预检）
qra sync apply          # 拉取+核对+快进+VERSION，跳过门禁（急用）
```

- **核心实现**：`src/qra/vendor_sync.py`（纯 stdlib + git CLI，单一事实来源）。
- **agent 工具**：插件 `qra_sync`（.hermes/plugins/qra/），对话里说"同步 hermes"即调用；`full` 模式门禁失败**自动回滚旧钉针**。两个入口复用同一核心。
- **兼容**：`scripts/vendor_sync.sh` 降级为薄壳透传（旧参数 --apply/--full 自动映射）。

五步：

1. **fetch upstream/main**（代理 127.0.0.1:7890 自动探测，大陆直连 GitHub 不稳；瞬时 SSL 抖动自动重试一次）。
2. **嫁接面核对**：diff 出 `旧钉针..新钉针` 的变更文件，与 `GRAFT_PATHS` 清单（21 个 QRA 外部依赖面文件，见 vendor_sync.sh）比对。**命中任何一项 → 拒绝自动落地（exit 2）**，必须人工逐文件看 diff、适配 QRA 侧代码、门禁跑通后再 `--apply`。
3. **ff-only 快进** `git merge --ff-only upstream/main`。
4. **VERSION 钉针回写**（上游无 VERSION 文件，这是 QRA 自己的 pin 机制）。
5. **回归门禁**（scripts/verify_qra.sh，四层）：
   - py_compile qra_console
   - 单测 9 项（TurnState 折叠状态机 / InputLayer 行编辑）
   - `-z` 真实 API 工具题 ×2（答案与新浪同源价格动态对照，不写死价格）
   - console 交互 pty 竞态用例 ×2（回合中空行 → 缓冲 → 回合后退出）
   - **全绿才算同步成功**；失败即回滚。

## 3. 回滚路径

门禁失败或发现上游引入回归时：

```
cd vendor/hermes-agent
git checkout <旧钉针>          # vendor 自带真实历史，可回到任意旧钉针
echo <旧钉针> > VERSION
# 重新跑 scripts/verify_qra.sh 确认回到已知良好状态
```

## 4. 维护义务（铁律延伸）

- **"新嫁接必须入清单"**：QRA 侧任何新增的 hermes 内部依赖（import hermes 内部模块、依赖其函数签名/常量/CLI 行为），必须同步追加到 `scripts/vendor_sync.sh` 的 `GRAFT_PATHS`，并在 docs/vendor_sync_log.md 登记。这是 D002"不改核心循环"的配套：不改，但把"我们依赖了哪些上游内部面"显式记账，上游动了它我们第一时间知道。
- **每次同步后写 docs/vendor_sync_log.md 一条记录**：旧钉针→新钉针、commit 数、嫁接面核对结果、门禁结果、回滚点。
- **建议节奏**：每周一次（上游迭代活跃期），或重大功能上线前必跑一次 `--full`。
- **上游大版本/破坏性变更**（如 hermes 架构重构）：嫁接面清单必然命中 → 走人工适配通道，绝不自动落地。

## 5. 首次同步结果（2026-08-15）

- 17d6a7d → de0abc06，44 commits / 56 文件（多为 gateway/desktop/ci 修复）。
- 嫁接面核对：**21 项零命中**——但注意 4 个文件实质有改动，只是清单外（评估见下）。
- 门禁：四层全绿（py_compile ✓ / 单测 9 ✓ / -z ×2 ✓ / 交互 pty ×2 ✓）。
- 同日（上游数小时内连推新 commit）又完成三轮同步：de0abc06→0a8765a（脚本化）、0a8765a→cc1c125（`qra sync` CLI 真同步+门禁全绿）、cc1c125→11c5aae（agent 工具 qra_sync full 模式真同步+门禁全绿）——命令与工具两条入口当天全部得到真实同步验证。
- 细节记录：docs/vendor_sync_log.md。

### 5.1 本次 diff 中与 QRA 相关的评估（人工过目结论）

| 文件 | 改动性质 | QRA 影响 |
|---|---|---|
| hermes_state.py | 增加 `update_session_model(session_id, model, provider=None)` 可选参数 | 纯增量，旧调用不受影响 ✓ |
| cli.py | 仅 /model 交互路径 | console 不用 /model 前缀，无影响 ✓ |
| hermes_cli/main.py | -z 启动守卫（昂贵模型确认） | 已验证 deepseek-v4-pro 不触发（$0.27/$1.10 vs 阈值 $20/$100；数据政策守卫仅 Meta 贡献者规则）✓ |
| tools/approval.py | EXEC_ASK 泄漏修复 | 修复上游 bug，利 QRA ✓ |
| agent/context_compressor.py (+5/-1)、agent/error_classifier.py (+2) | 微调 | 不触碰 qra_refine 的 prompt 常量 ✓ |

## 6. 为什么这样做是"稳定迭代"而非"跟着上游漂"

1. **钉针（pin）**：QRA 永远跑在显式 commit 上，不是跑在"上游最新"。同步是主动决策，不是被动跟随。
2. **ff-only**：无冲突可能，vendor 永远干净。
3. **嫁接面清单**：上游动我们依赖的内部面 → 自动拦截，人工评估后才落地。
4. **回归门禁**：每次同步都用真实 API E2E 验证 QRA 核心路径没被打断。
5. **可回滚**：任何一次同步都可一键回到上一个已知良好钉针。

## 7. 多上游同步（2026-08-16，essence 源机制）

雅宁 2026-08-16 拍板：「极简 bootstrap 不足以支撑工业实践……像 hermes 同步那样，
prime、deepseek-harness 也是极具价值的项目，未来它们更新、升级，也要把 diff 合并
到我们的 agent 中去」。由此 vendor_sync 从单上游扩展为注册表形态：
`src/qra/vendor_sync.py` 的 `UPSTREAMS`（hermes / prime / dsh 三条配置）。

### 7.1 两个同步形态

| 形态 | 上游 | 语义 |
|---|---|---|
| **managed** | hermes（基底） | §1-§5 现状不变：ff-only + 嫁接面硬拦截 + 门禁 + 失败自动回滚 |
| **essence** | prime / dsh（本质源） | 推进 vendor 钉针 + diff 报告；**不自动合并到 QRA 代码** |

essence 的 vendor 克隆只是「源头材料」：QRA 侧移植活在 src/qra/ 与
.hermes/plugins/ 里（qra_runtime 完全体 = prime rlm 的移植）。上游动了嫁接面
文件 → 结果 `needs_regraft=True`（CLI 打印 ⚠️ 待重移植清单；agent 工具回传
needs_regraft 字段），人工 diff → 重移植到 QRA 侧 → 门禁跑通后闭环。
essence 推进不打门禁：新代码未进 QRA 运行面（直到重移植完成）。
essence 的 full 与 apply 等价（无门禁步骤），report=只拉取核对不落地。

### 7.2 命令与入口

```
qra sync <upstream> [full|apply|report]   # upstream: hermes|prime|dsh；默认 hermes full
qra sync                                   # 与旧版完全兼容（hermes 完整同步）
```

- agent 工具 qra_sync 增加 `upstream` 参数——对话里说「同步 prime」即触发。
- 兼容承诺：`scripts/vendor_sync.sh` 薄壳（--apply/--full 映射）与旧参数
  位不变，`qra sync report` 等旧用法语义不变（= hermes report）。

### 7.3 嫁接面清单

- `GRAFT_PATHS`（hermes，managed）33 项，§2 语义不变。
- `PRIME_GRAFT_PATHS`（essence）7 项：qra_runtime 三文件的直接母本
  （prime-agent-runtime/src/rlm/__init__.py、rlm/harness.py、agent-message
  skill 的 agent_message/__init__.py）+ comm 桥协议宿主侧 4 文件
  （coding-agent/src/core/kernel/index.ts、kernel/bootstrap.ts、
  tools/ipython.ts、agent-session.ts——target "qra.host.request"、control
  通道回执、comm open type-last 的语义源头，协议变更必须人工复核）。
- `DSH_GRAFT_PATHS`（essence）4 项：P1 精华「fail-loud 启动自检 + 配置
  schema 硬校验」的 canonical 源（boot/app-boot 的 index.ts、invariant.ts；
  settings-file/index.ts、settings/types.ts）。借底座形态——QRA 侧实现是
  config_guard.py + qra_python 启动自检，这些文件是 diff 复核的溯源点。

### 7.4 钉针现状（2026-08-16）

- hermes 11c5aae（managed，门禁全绿钉针）；prime 83a0f9f9（v0.7.2 release
  commit，essence，已验证远端默认分支 = main）；dsh 47f94385（master，
  essence）。vendor 整体 gitignore；VERSION 是 QRA 自己的 pin 文件。
- 上游若改默认分支，改 UPSTREAMS 里对应 branch 字段即可。
