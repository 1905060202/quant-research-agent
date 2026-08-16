# QRA · Quant Research Agent

**量化研究智能体**：hermes-agent 骨架 + QRA 嫁接层的融合架构——把「行情数据 →
信号 → 方法论文档 → 日报 + 验证卡」做成一条可评测、可复算的自动流水线，
并带一个与 Claude Code 对齐的交互终端（CoT 全展示 + /命令体系 + 持久 Python 内核——
凡是「算」的走内核，路由铁律见 AGENTS.md）。

[![CI](https://github.com/1905060202/quant-research-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/1905060202/quant-research-agent/actions/workflows/ci.yml)

## 能力总表

| 层 | 实现 | 说明 |
|---|---|---|
| 终端 | `qra` 入口 + `src/qra/console/` | prime 式 CoT 全展示（无框流式+追加式渲染不重复）；15 个 /命令（resume/clear/export/model/yolo/loop/fold/mouse/agents…，输入 / 即弹菜单，Enter 即执行）；! 直达 shell；←→ 光标编辑/↑↓历史/Tab 补全/大粘贴确认；deepseek↔opus 双路由 |
| 工具 | `.hermes/plugins/qra/` | qra_quote 新浪实时行情 / qra_signal 猎豹信号摘要（诚实标注数据新鲜度）/ qra_kb_fts 方法论 FTS 检索 / qra_sync 上游同步；全工具 schema 信封化——deferred 面 tool_describe 描述可信（回归锁 `src/qra/tests/test_plugin_envelope.py`） |
| 内核 | `.hermes/plugins/qra_python/` | D007 指令③「全生命周期计算底座」：会话级 Jupyter 持久内核，变量跨调用存活、dill 快照跨重启复活、死内核自愈；凡是「算」的走内核（AGENTS.md/SOUL.md/skill 三层注入，禁止退回 execute_code 一次性脚本）；生命周期可调——QRA_PY_IDLE≤0=永不关停、QRA_PY_MAXLIVE=2 LRU 池；预装 qra_runtime（prime 完全体）——`qra.run` 递归子代理、harness 文件店、agent_message |
| 记忆 | `.hermes/plugins/qra_memory/` | Mem0 式 ADD 协议：三重去重（会话/精确/近似≥0.85）+ 价格锚放宽 + 叙事链；显式检索回忆 |
| 验证 | `.hermes/plugins/qra_verify/` | claims 账本 + 4 类确定性检查器（行情/文件/内容/区间）+ 回合末守卫强制续跑 |
| 评审 | `.hermes/plugins/qra_refine/` | prime 准入门三段流水线移植（官方 background_review 钩，fail-loud 启动自检），拒绝→零写入 |
| 日报 | qra_daily 技能 | 信号→行情→方法论→记忆→撰写→验证卡 6 步，3 条可验证预测 |
| 同步 | `src/qra/vendor_sync.py` | 三上游同步：hermes=managed（门禁+回滚）/ prime·dsh=essence（钉针+diff 报告），嫁接面漂移自动拦截 |
| 评测 | `bench/` | 30 题×3 域，动态 gold + 机械评分 + 幻觉双口径，可任意时刻复算 |

## 快速开始

```bash
# 1. 底座（pin 11c5aae，钉针记录见 docs/vendor_sync_log.md）
git clone https://github.com/NousResearch/hermes-agent.git vendor/hermes-agent
cd vendor/hermes-agent && git checkout 11c5aae104cb95b5141744dcb277448ef8b24dce && cd ../..

# 2. 依赖 + 底座安装（uv 环境；内核另需 jupyter_client/ipykernel/dill）
uv venv .venv-v7 --python 3.12
uv pip install -e vendor/hermes-agent --python .venv-v7/bin/python
uv pip install jupyter_client ipykernel dill --python .venv-v7/bin/python

# 3. 凭据：零落盘——入口自动从 ~/.claude/settings.json 提取
#    env.ANTHROPIC_AUTH_TOKEN（也可 export ANTHROPIC_TOKEN 显式注入）

# 4. 运行（加 PATH 后任意目录可用）
ln -s "$(pwd)/bin/qra" ~/.local/bin/qra     # 一次性，无需 sudo
qra                                         # 进系统：CoT 多轮交互（Ctrl+T 折叠思考）
qra -z "查一下贵州茅台现价"                  # 单发问答
qra sync                                    # 同步 hermes 上游（门禁+回滚）

# 5. 跑评测（30 题，动态 gold 自动重采行情）
.venv-v7/bin/python bench/run_qra.py
.venv-v7/bin/python bench/scorer.py --results bench/results/qra
```

改代码后：`scripts/verify_qra.sh`（六层回归门禁，约 4-7 分钟）。
无 key 环境：`scripts/verify_qra.sh --offline`（CI 同款离线层）。

## 评测结果（2026-08-14 复算）

| 系统 | 30 题 | 幻觉率 | 时延 |
|---|---|---|---|
| QRA | 100% | 0/30 | 41s |
| Claude Code（同题对照） | 100% | 0/30 | 79s |

长程记忆：跨会话写入→回忆→判重 3/3。口径细节见 `bench/README.md`；能力上限
对比需难题层——诚实边界见 `docs/W6_QRA-Bench评测完成记录_2026-08-14.md`。

## 文档地图

**新 session / 新 agent 接手：先读根目录 `HANDOFF_新session必读.md`，再读
`docs/README.md`。** 全文档索引（架构/开发规范/CI/参考手册/已知坑/决策记录）
都在那里，每类信息只有一个权威源。

```
docs/README.md          文档地图（按角色/场景导航）
docs/architecture.md    架构总览（分层/执行线/目录/插件面/钉针）
docs/development.md     开发规范（工作流/铁律/测试/提交推送/脱敏红线）
docs/ci.md              CI/CD 管道与本地/CI 矩阵
docs/reference.md       参考手册（命令/工具/qra_runtime API/env/config）
docs/troubleshooting.md 已知坑（症状→根因→解法）
docs/decisions/         ADR D001-D010（「为什么长这样」全在这里）
docs/vendor_sync_log.md 上游同步账本（钉针/嫁接面/回滚点）
```

## 架构一句话

**Hermes 骨架（核心循环+持久记忆，vendor 零修改）+ QRA 嫁接层（全走官方插件面）**。
上游更新无损跟进：`qra sync` 按钉针快进 + 嫁接面核对，漂移即拦。完整分层图、
执行线与铁律见 `docs/architecture.md`；历史蓝图见 `docs/融合架构_v1.0_2026-08-14.md`。

## 许可证与安全

MIT（见 LICENSE）。`vendor/` 为上游仓库副本（NousResearch/PrimeIntellect/
deepseek-ai，各自许可证为准），不属于本仓发布物。

- **零凭据铁律**：明文 key 永不入库；凭据走环境变量；push 前跑
  `scripts/scan_credentials.sh`，CI 同款扫描自动拦截。
- `HANDOFF_新session必读.md` 含本地环境信息，已 gitignore。
- kb_sources 已脱敏（14 处人称替换，两 KB 同步重灌）。
- 完整规则见 `docs/development.md`「脱敏红线」。
