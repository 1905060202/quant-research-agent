# ADR D010：ARC-AGI-3 挑战定位与 harness 增强路线

- **日期**：2026-08-15
- **状态**：已采纳
- **背景研究**：docs/机理研究_dsh论文与ARC3作战_2026-08-15.md（四路并行：dsh 论文全读/ARC 情报/prime 增量/pi 源码）

## 背景

雅宁提出：吸收 dsh（88 页论文+源码）、prime-agent、pi agent 的设计增强 QRA，并以 ARC-AGI-3 为战场验证"QRA+DeepSeek V4 Pro 能否达到 prime+Opus 5 的成绩"。此前 D008 的结论是"不追 ARC 榜，差异化在量化研究域"。

## 情报事实（决策依据）

1. prime+Opus 5 = 95.5% RHAE（公开集 Best@1，官方 scorecard 中位数 95.24%）；同 harness 换底模：Sol 78.3% / Terra 25.7% / GLM 8.6%——**底模决定上限（三源互证：prime 对照、arc-code 研究、本次）**。
2. 公开集已被打穿（社区榜榜首 Tycho 100%），官方榜禁 harness，Kaggle 评测断网（V4 Pro 仅 API 无法参赛）。QRA 唯一官方路径：社区榜 → 被选中验证 → 半私有集 ±15pp。
3. DeepSeek V4 Pro 在 AGI-3 零成绩——底模水平只能实测。
4. 公开集 = 免费的 harness benchmark：pip 包接口、可重复、成本 $650-$3,000/轮、测的恰好是我们要吸收的全部机制（长程记忆/压缩/自组装/隔离/验证循环）。

## 决策

1. **挑战定位 = benchmark 驱动架构迭代 + 诚实上榜，不是搏名次**：目标=公开集成本-性能前沿 + harness 乘数最大化（prime 乘数 3.2× 为对照）；里程碑=社区榜上榜 → 争取官方验证通道。Kaggle 弃赛（断网硬约束）。这修正但不推翻 D008——D008 反对的是"搏名次"与"DeepSeek 打同榜无对比意义"，本决策把 ARC-AGI-3 用作架构 benchmark，诚实边界保留。
2. **测量驱动**：Phase 0 先测 V4 Pro 裸测基线（25 公开环境，预算 ≤¥100），按裸测分数定目标档（<5% → 20-40%；5-20% → 40-70%；>20% → 冲 95%）。每个机制吸收=一次全量重跑记增量，无增量两周即收缩为纯 benchmark。
3. **机制吸收合并表**（dsh 13 + prime 16 + pi 13 + arc-code 4 去重后 29 项，P0 六项先落）：
   - P0：A1 log-as-memory 双写、A2 压缩日志条目（三源互证）、B1 外部校验门+continuation prompt、B2 假说回测优先+expect 预测、B3 playbook 规则沉淀、C1 EffectRegistry、C10 截断消息工具全拒、C7 快照 debounce（随 D007 P2）
   - P1：A4 剪枝+token 账本、A6 双轨、C2 运行时自组装（registry.register 已验证可行，unload=stub 换 handler）、C5 多环境 isolate、C6 技能双形态、C8 便宜模型、C11 缓存隔离、D1 驱动工厂、D2 重试分类
   - P2：A3/A5/C3/C4/C9/C12/C13/B5/B6/D3/D4
4. **零 vendor 承诺延续**：全部机制落 graft 层，D002 不动；两个技术前提已验证（registry.register 公开线程安全、console 已自建循环）。
5. **收益回流主业务**：所有机制（记忆/压缩/验证/成本）同时服务于量化研究主业务，打榜是训练场不是目的。

## 后果

- 正面：harness 层获得可度量迭代闭环；D007 P2 持久内核借势落地；成本控制实战化。
- 负面/风险：API 成本可能失控（Tycho $2,986 单跑先例）；V4 Pro 底模可能极弱（GLM 8.6% 先例）；公开集成绩不迁移半私有集。均以 Phase 0 摸底 + 预算红线 + 退出条件对冲。
