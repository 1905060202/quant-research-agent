# D001：底座选择 hermes-agent（触发器翻盘）

- 状态：accepted
- 日期：2026-08-14

## 背景

底座候选：prime-agent（NousResearch）、hermes-agent、自研。对比分析 v1.0 推荐 prime；
对抗审计修正两条硬伤（prime 本机实为 flash 非 pro；Hermes bug 簇 2026-04 已闭环），
修正后两者几乎平级。用户对"完整体"的标准 = 真实 fork + 改核心，不是给 harness 写技能——
这条标准此前已推倒过 v1-v7 五次方案，是项目最深层约束。

## 决策

底座 = hermes-agent（vendor/ 钉针 + 自有 git 基线）。AskUserQuestion 三问中选了 prime，
但判 prime 为"SKILL.md-only 形态=简化版"→ 按对比分析 v1.1 第九节事先写明的触发器 #1
（判定简化版→翻盘 Hermes）自动翻盘。同一把尺子推倒过 v1-v7，现在用它选底座。

## 备选项

- prime-agent：完整体判定不达标（本机形态只读 SKILL.md，无真实 fork 改核心路径）
- 从零自研：违背"不重复造轮子"；核心循环/工具协议/记忆体系均需重造

## 后果

- 融合架构成为唯一可行路径：Hermes 骨架 + grafts，铁律=嫁接不得修改核心循环
- 插件契约三坑（handler 签名/register_tool 参数/auth 无裸 token）成为长期约束
- 明文 key sk-40…（HANDOFF）必须轮换；开源仓库零凭据铁律成立
