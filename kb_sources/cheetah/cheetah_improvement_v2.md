# 猎豹模型改进方案 v2.0：基于日频预测能力的发现

> 日期: 2026-06-02
> 状态: 已实施（cheetah_regime.py + cheetah_daily.py + signal_interface.py v2.0 补丁）
> 作者: Claude Code + 用户

---

## 1. 问题诊断

### 1.1 核心发现

通过对 `hermes_weekly_v5.pkl` 的特征重要性分析，发现：

1. **5d/10d/20d 三个模型本质相同**
   - 每个 tier 内部，三个 horizon 的特征重要性排名完全相同
   - 5d 和 20d 共用 170 个特征，0 个独有特征
   - 5d↔20d 预测值相关性实测 = 1.0000

2. **模型实际学到的是横截面动量强度，不是 horizon-specific 预测**
   - Top6 特征全部是技术/动量指标：beta_60d、kc_position_20、vol_60d、log_price_ma20、amihud_20d、dist_ma120
   - 零基本面因子进入前六
   - 模型回答的是"哪些股票在强趋势中"，不是"X天后哪些会涨"

3. **日频为什么准**
   - A 股横截面动量跨时间尺度高度稳定（不像美股有短期反转效应）
   - 趋势延续日：日频 Top5 命中率高
   - 趋势断裂日：模型反指（5/29 Hot 板块次日 -4.4% vs Cold -0.55%）

### 1.2 当前模型状态

```
hermes_weekly_v5.pkl:
  models: {large/mid/small: {5/10/20: LGB model}}
  decay_weights: {5: 0.0, 10: 0.0, 20: 1.0}  ← 只用20d
  5d/10d 模型训练好了但闲置（因为与 20d 完全相同）
```

### 1.3 表现追踪现状

- performance_log.jsonl 只有 5/29 一条记录
- backfill_outcomes.py 依赖外部的 Sina API
- 每日 Top5 表现没有系统追踪

---

## 2. 改进方案设计

### 2.1 信号层面：接受动量探测器定位，放弃多 horizon ensemble

**决策：不改变 decay_weights，继续只用 20d 模型。**

理由：
- 5d/10d/20d 模型完全相同 → ensemble 无增量信息
- 纯 20d 权重 (1.0) 是当前最优配置
- 改变 decay 只会稀释信号，不增加多样性

**替代方案：在输出层面区分"动量信号"和"基本面信号"。**
- 动量信号 = 模型原始输出（横截面动量排名）
- 基本面约束 = 独立计算的基本面评分（待 P1 实施）
- 最终信号 = 动量信号 × 基本面约束

### 2.2 Regime 检测：日频 Top5 命中率追踪器（已实施）

**方案：cheetah_regime.py**

核心逻辑：
1. 每日收盘后，读取 features_v9.pkl 的 close 价格 + 板块映射表
2. 计算昨日信号中 Top5/Cold5 板块的实际次日收益（等权成分股）
3. 回填到 performance_log.jsonl 的 next_day 字段
4. 用加权滚动窗口（5天，时间衰减）计算命中率
5. 分类 regime：
   - `momentum_continuation`：命中率 ≥ 60% → 信号可信，adjustment=1.15x
   - `neutral`：命中率 20%-60% → 正常参考，adjustment=1.0x
   - `momentum_break`：命中率 ≤ 20% → 反指风险，adjustment=0.50x
   - `unknown`：数据不足（<3条）→ 待积累

**关键设计决策：**
- 使用加权（指数衰减）命中率，近期表现更重要
- 不用外部 API——从 features_v9.pkl 直接算板块收益
- 渐进式：数据不足时返回 unknown，不强行给判断

### 2.3 特征层面：接受"动量探测器"定位，基本面约束作为 P1

**当前阶段（P0）：接受动量探测器定位**
- 模型本质是动量探测器，这是 A 股市场的结构性特征
- 日频命中率高是因为 A 股动量跨时间尺度稳定
- 不强行加入基本面特征稀释动量信号

**下一阶段（P1）：基本面约束层**
- 独立计算 24 个基本面特征的板块级评分
- 当动量信号高但基本面弱 → 标记为"投机性动量"
- 当动量+基本面共振 → 标记为"质量动量"
- 实现方式：独立模块 `cheetah_fundamentals.py`（不改变现有模型）

### 2.4 产品化：猎豹日频子产品（已实施）

**方案：cheetah_daily.py**

与 20 天中期信号的区别：

| 维度 | 猎豹 v2.0 (signal_interface.py) | 猎豹日频 (cheetah_daily.py) |
|------|-------------------------------|---------------------------|
| 用途 | 中期配置参考 | 次日方向判断 |
| 输出 | 134 板块完整排名 | Top10 看多 + Bottom5 看空 |
| Regime | 元数据标注 | 驱动的置信度分级 |
| 嵌入 | latest_signal.json | daily_signal.json |
| 消费 | 日报/研报/深度分析 | 早报 Hero 区/晨会笔记 |

**置信度分级：**
- 动量延续 regime → 高置信度（direction_confidence=high）
- 中性 regime → 中置信度（medium）
- 动量断裂 regime → 低置信度（low），信号压制
- 数据不足 → 正常输出但标注 unrated

### 2.5 可行性：轻量改动（已完成）

| 改动 | 文件 | 行数 | 影响范围 |
|------|------|------|---------|
| Regime 检测器 | cheetah_regime.py | ~290 行 | 新增模块 |
| 日频子产品 | cheetah_daily.py | ~200 行 | 新增模块 |
| 信号接口补丁 | signal_interface.py | +25 行 | 新增 regime 字段 |
| 总改动 | 3 个文件 | ~515 行 | 零破坏性 |

**参照 34→134 板块切换（4 行改动），本次改动同样轻量：**
- signal_interface.py 只增加了 ~25 行（导入 + regime 检测）
- 不改模型权重、不改特征、不改股票预测逻辑
- 回滚：删除导入 + 3 行代码即可

---

## 3. 实施细节

### 3.1 管道集成

每日收盘后管道（更新后的 pipeline_v101.sh 建议）：

```bash
# 1. 刷新数据
python3 daily_refresh.py

# 2. 回溯昨日信号表现（用 features_v9.pkl 计算板块收益）
python3 cheetah_regime.py --update

# 3. 生成今日信号
python3 signal_interface.py

# 4. 记录今日信号（供明天回溯）
python3 log_signal.py

# 5. 输出日频信号
python3 cheetah_daily.py
```

### 3.2 数据流

```
features_v9.pkl (close prices)
    ↓
cheetah_regime.py --update     ← 回填昨日 Top5/Cold5 实际收益
    ↓
performance_log.jsonl          ← 累积历史命中率数据
    ↓
cheetah_regime.py --detect     ← 计算当前 regime
    ↓
signal_interface.py             ← 嵌入 regime 到 latest_signal.json
cheetah_daily.py                ← 独立输出 daily_signal.json
    ↓
早报/晨会/研报                  ← 消费日频信号
```

### 3.3 关键文件

| 文件 | 用途 | 大小 |
|------|------|------|
| cheetah_regime.py | Regime 检测器（检测 + 回填） | ~290 行 |
| cheetah_daily.py | 日频信号输出（Top10 + 置信度） | ~200 行 |
| signal_interface.py | 生产信号接口（v2.0 补丁） | +25 行 |
| daily_signal.json | 日频输出（机器消费） | ~5KB |
| performance_log.jsonl | 历史表现日志（含实际收益） | 逐日增长 |

---

## 4. 预期效果量化估计

### 4.1 Regime 检测准确率（估计）

基于 A 股动量特征：
- 趋势延续日占比约 60-70%（牛市偏高，震荡市偏低）
- 动量延续 regime 下，日频 Top5 预期命中率 ≥ 60%
- 动量断裂 regime 下，日频 Top5 预期命中率 ≤ 20%
- 中性 regime 下，命中率约 40-60%

### 4.2 Regime 调整效果

| Regime | 预期频率 | 信号调整 | 预期命中率 | 实战效果 |
|--------|---------|---------|-----------|---------|
| momentum_continuation | ~40% | 1.15x 提升 | ≥60% | 加仓跟随 Top3 |
| neutral | ~45% | 1.0x 不变 | 40-60% | 正常参考 |
| momentum_break | ~15% | 0.50x 压制 | ≤20% | 防御/观望/看 Cold |
| unknown | 前 3 天 | 1.0x 不变 | 未知 | 待积累 |

### 4.3 关键指标追踪

- 累计命中率趋势（weekly）：是否稳定在 50% 以上
- 最大连续失效天数（consecutive_miss_days）：超过 3 天 → 警惕
- 平均 spread（Hot-Cold）：正向且稳定 = 信号有区分度
- Regime 切换频率：过于频繁 → 市场无序，信号不可信

---

## 5. 风险与 Failure Mode

### 5.1 已知风险

1. **冷启动期（前 3-5 天）**：
   - Regime 为 unknown，无法提供调整
   - 缓解：正常输出信号，标注 unrated

2. **震荡市期间**：
   - 命中率在 40-60% 波动 → 频繁切换 neutral/momentum_break
   - 缓解：加权命中率有惯性，不会因为一天失效就切换

3. **板块映射变更风险**：
   - sw_l2_smart_merge_map.pkl 变更 → 成分股变化 → 历史表现不可比
   - 缓解：映射稳定后影响很小（申万 L2 分类变更频率低）

4. **features_v9.pkl 延迟风险**：
   - daily_refresh 失败 → features 未更新 → 回填时找不到次日数据
   - 缓解：cheetah_regime.py 会 skip 并返回原因

### 5.2 明确不做的事

1. **不改变模型权重** — decay_weights 保持 {20: 1.0}
2. **不重训模型** — 模型文件不变
3. **不改变 stock_preds.json 输出格式** — 下游兼容
4. **不增加外部 API 依赖** — 只用本地数据
5. **不在 signal_interface.py 中加载 features_v9.pkl** — regime 检测是独立模块

### 5.3 回滚方案

如果 regime 检测引入问题：
```bash
# 删除 cheetah_regime.py 和 cheetah_daily.py
# 在 signal_interface.py 中删除 CheetahRegime 相关 3 行导入 + 25 行检测代码
# 恢复：git checkout signal_interface.py
```

---

## 6. 下一步

### 短期（本周）
- [x] cheetah_regime.py 部署并验证
- [x] cheetah_daily.py 部署并验证
- [x] signal_interface.py v2.0 补丁
- [ ] 管道脚本集成（pipeline_v101.sh 添加 cheetah_regime --update）
- [ ] 早报模板添加 Regime 状态块

### 中期（2-4 周后）
- [ ] 积累 10+ 交易日数据 → 首次 regime 分类验证
- [ ] cheetah_fundamentals.py：基本面约束层（P1）
- [ ] 回测 regime 调整的实际效果（命中率提升）

### 长期
- [ ] 引入正交信号源（非动量特征）
- [ ] 融资融券/龙虎榜特征（P1 管道已建）
- [ ] Meta-Labeling：regime + OOF 预测 → 元模型
