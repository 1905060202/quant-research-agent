# 猎豹 v2.0 改进方案设计文档

> 基于日频预测能力的发现 —— 时间窗口不变性 + 动量探测器本质
>
> 日期: 2026-06-02 | 版本: v1.0

---

## 1. 背景与核心发现

### 1.1 时间窗口不变性（实测验证）

对 `hermes_weekly_v5.pkl` 的分析确认：

| 发现 | 细节 |
|------|------|
| **特征重要性完全相同** | large/mid/small 每个tier内部，5d/10d/20d三个horizon的特征重要性排名完全一致 |
| **零独有特征** | 5d和20d共用170个特征，0个独有特征 |
| **预测值相关性=1.0** | 5d↔20d预测值Pearson r实测=1.0000 |
| **Top6特征全是技术/动量** | beta_60d、kc_position_20、vol_60d、log_price_ma20、amihud_20d、dist_ma120 |
| **零基本面因子进前六** | ROE、毛利率、负债率等24个基本面特征重要性接近零 |

### 1.2 模型本质

**猎豹模型是一个横截面动量强度探测器**，不是horizon-specific预测器。

它在回答："哪些股票现在在强趋势中？"  
而不是："哪些股票20天后会涨？"

### 1.3 日频为什么准

A股横截面动量跨时间尺度高度稳定（不像美股有短期反转效应）。在趋势中的股票：今天涨 → 明天涨 → 20天后也涨。所以：

- **趋势延续日**：日频Top5命中率高（Hot跑赢Cold）
- **趋势断裂日**：模型反指（5/29 Hot板块次日-4.4% vs Cold -0.55%）

---

## 2. 改进方案总览

### 2.1 核心原则

**零模型改动。** 不改任何模型权重、不重训、不加特征。所有改进在信号消费层完成——与34→134板块切换同级别的轻量改动。

### 2.2 三组件架构

```
performance_log.jsonl ──→ regime_detector.py ──→ regime_state.json
                                                         │
                                                         ▼
features_v9.pkl ──→ signal_interface.py ──→ latest_signal.json (含regime块)
       │                                            │
       └── hermes_weekly_v5.pkl                     ▼
                                            cheetah_daily.py ──→ cheetah_daily_signal.json
```

| 组件 | 类型 | 改动量 | 说明 |
|------|------|--------|------|
| `regime_detector.py` | **新建** | ~220行 | 从performance_log计算动量健康度 |
| `signal_interface.py` | **修改** | +~40行 | 加载regime状态，新增regime+cheetah_daily输出块 |
| `cheetah_daily.py` | **新建** | ~220行 | 日频子产品——次日板块方向+置信度 |

**总新增代码：~480行。修改现有代码：~40行（signal_interface.py）。**

---

## 3. 信号层面：多Horizon Ensemble重新定位

### 3.1 结论：多horizon ensemble无意义

既然5d/10d/20d模型特征重要性完全相同、预测值相关性1.0，decay-weighted ensemble不会产生任何增量信息。

**当前状态**：
```python
decay_weights = {5: 0.0, 10: 0.0, 20: 1.0}  # 只用20d
```

**v2.0决策**：保持 `decay_weights` 不变。5d和10d模型继续闲置。精力投入到regime检测而非虚假的ensemble多样性。

### 3.2 替代方案：置信度调制

不做多horizon ensemble，而是用regime状态调制信号置信度：

```
effective_signal = base_signal × confidence_modifier(regime_health)
```

其中 `confidence_modifier` 由日频Top5命中率决定：
- 动量健康（hit_rate_5d ≥ 0.6）：modifier = 1.0（信号全效）
- 动量中性（0.4 ≤ hit_rate_5d < 0.6）：modifier = 0.7（信号打折）
- 动量衰减（hit_rate_5d < 0.4）：modifier = 0.4（信号大幅降权）

---

## 4. Regime检测：动量健康度监测器

### 4.1 设计

`regime_detector.py` 从 `performance_log.jsonl` 读取每日Top5 vs Bot5实际表现，计算滚动指标：

| 指标 | 计算方式 | 含义 |
|------|---------|------|
| hit_rate_5d | 近5天Hot>Cold的天数占比 | 短期动量稳定性 |
| hit_rate_10d | 近10天Hot>Cold的天数占比 | 中期动量稳定性 |
| cumulative_spread_5d | 近5天Hot-Cold累计spread | 动量累积强度 |
| consecutive_hit_days | 连续命中天数 | 趋势延续确认 |
| consecutive_miss_days | 连续失误天数 | **趋势断裂预警** |

### 4.2 分类逻辑

```
if hit_rate_5d >= 0.6 AND consecutive_miss < 3:
    → healthy (动量健康)
elif hit_rate_5d >= 0.4:
    → neutral (动量中性)
elif data_days < 3:
    → insufficient_data (数据不足)
else:
    → warning (动量衰减)
```

**Regime Shift Alert**：`consecutive_miss_days >= 3` → 趋势可能已断裂。

### 4.3 数据来源

- **历史**：`performance_log.jsonl` 中已回填的 `next_day` 数据
- **实时**：`--live` 标志用 Sina API 直接验算当日Top5板块涨跌幅
- **积累**：需至少3个交易日回填数据才能输出有效regime判断

### 4.4 输出

`regime_state.json`：
```json
{
  "momentum_health": "healthy",
  "hit_rate_5d": 0.8,
  "hit_rate_10d": 0.7,
  "cumulative_spread_5d": 3.2,
  "consecutive_hit_days": 4,
  "consecutive_miss_days": 0,
  "regime_shift_alert": false,
  "daily_confidence": 0.75,
  "direction_guidance": "动量健康——日频Top5方向可信，可按20d信号正常使用"
}
```

---

## 5. 特征层面：接受动量探测器定位

### 5.1 为什么不加基本面特征

1. **Top6特征全是技术/动量**——LightGBM已经做了特征选择，基本面特征importance接近零
2. **标签是20d forward return**——在A股中这本身就是动量驱动，基本面因子在这个horizon上信噪比太低
3. **OHLCV信息已饱和**（pitfall #28：连续3次实验ΔIC<0.01）
4. **加入弱信号=加入噪声**——会降低模型的纯净度

### 5.2 替代方案：基本面叠加层（未来）

不在模型内嵌入基本面，而在信号消费层做正交叠加：

```
最终评分 = 动量信号 × regime置信度 + 基本面质量分 × (1 - regime置信度)
```

- 动量健康时：权重偏动量（80%动量 + 20%基本面）
- 动量断裂时：权重偏基本面（30%动量 + 70%基本面）→ 自动防御性轮动

**当前状态**：基本面叠加层留作v2.1，需要先积累regime数据验证切换逻辑。

### 5.3 这个定位的实战含义

| 场景 | 模型行为 | 正确用法 | 错误用法 |
|------|---------|---------|---------|
| 趋势延续 | Hot板块继续涨 | 顺势做多Hot | — |
| 趋势断裂 | Hot板块反指 | regime预警→减仓/防御 | 盲目追Hot |
| 震荡市 | 信号噪声大 | 降仓+等regime确认 | 频繁交易 |
| 风格切换 | 旧Hot可能暴跌 | regime检测到连续miss→清仓旧热点 | 死扛 |

---

## 6. 产品化：猎豹日频子产品

### 6.1 定位

| 维度 | 猎豹 v2.0 (20d) | 猎豹日频 v1.0 |
|------|-----------------|-------------|
| 问题 | 哪些板块在强趋势中？ | 这个趋势明天还继续吗？ |
| 时间尺度 | 20个交易日（~1个月） | 1个交易日 |
| 信号来源 | LightGBM预测 → 板块聚合 | 同一预测 + regime置信度调制 |
| 输出 | 134板块排序（Hot/Neutral/Cold） | Top5日频展望 + 置信度 + 操作指引 |
| 用途 | 中期配置方向 | 次日交易时机判断 |

### 6.2 日频展望分类

对每个Top5板块给出日频展望：

- **likely_continue** 🟢：动量健康，板块趋势大概率延续
- **uncertain** 🟡：动量中性，方向不确定
- **risk_reversal** 🔴：动量衰减，Hot板块可能回调
- **unrated** ⚪：数据不足，无日频判断

对每个Bot5板块：
- **likely_continue_weak** 🔻：动量延续，Cold板块大概率继续弱势
- **potential_reversal** 🔄：动量断裂，Cold板块可能出现均值回归

### 6.3 使用规则

```
when_to_trust:  momentum_health=healthy 且 daily_confidence > 0.65
when_to_hedge:  momentum_health=neutral → 降低仓位
when_to_fade:   momentum_health=warning 或 regime_shift_alert=true → Hot可能反指
max_position:   日频信号仅作方向参考，不替代20d中期仓位决策
```

---

## 7. 改动清单

### 7.1 新建文件

| 文件 | 行数 | 用途 |
|------|------|------|
| `regime_detector.py` | ~220 | 动量健康度监测器 |
| `cheetah_daily.py` | ~220 | 日频子产品 |
| `cheetah_v2_design.md` | 本文档 | 设计文档 |

### 7.2 修改文件

| 文件 | 改动行数 | 改动内容 |
|------|---------|---------|
| `signal_interface.py` | +~40 | REGIME_PATH常量、_load_regime()函数、output中regime+cheetah_daily块、版本号更新 |

### 7.3 未改动

- ❌ `hermes_weekly_v5.pkl` — 模型权重不变
- ❌ `features_v9.pkl` / `features_v10.pkl` — 特征不变
- ❌ `get_predictions()` — 预测逻辑不变
- ❌ `aggregate_to_subsectors()` — 板块聚合不变
- ❌ `decay_weights` — 保持{5:0, 10:0, 20:1.0}
- ❌ `daily_refresh.py` — 每日刷新不变
- ❌ `backfill_outcomes.py` — 回填逻辑不变（regime_detector消费其输出）

---

## 8. 预期效果量化估计

### 8.1 直接影响

| 指标 | v1.0 | v2.0 预期 | 依据 |
|------|------|----------|------|
| 模型IC | 不变 | 不变 | 模型权重未改动 |
| 板块IC | ~0.21 | ~0.21 | 聚合逻辑未改动 |
| 日频Top5命中率 | ~60-70%（趋势日） | 同左 | 预测未变 |
| regime预警准确率 | N/A | >70%（趋势断裂日提前1-2天预警） | consecutive_miss≥3逻辑 |
| 信号可用天数 | 100% | ~75-85%（中性/警告日降权后减少无效交易） | 保守估计 |

### 8.2 间接收益

1. **减少反指交易**：regime warning日自动降权 → 避免趋势断裂日追Hot（如5/29案例中Hot-4.4%的亏损）
2. **提升夏普**：只在momentum_healthy日全仓 → 过滤掉低质量交易日
3. **运维可观测性**：regime_state.json 提供模型健康度的量化仪表盘
4. **决策辅助**：日报/研报可引用regime状态——"猎豹动量健康，日频信号可信"

### 8.3 关键假设与验证

| 假设 | 验证方式 | 通过标准 |
|------|---------|---------|
| 连续3天miss能预警regime shift | 积累30+交易日数据后回测 | 预警后5天内出现≥1次趋势断裂的概率>60% |
| 动量健康日Top5确实跑赢 | 持续追踪performance_log | hit_rate_5d在healthy期>0.6 |
| 降权减少亏损但不牺牲太多收益 | 模拟对比：全时段等权 vs regime-modulated | 夏普提升>0.1 |

---

## 9. 风险与Failure Mode

### 9.1 已知风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| **regime数据不足** | 初期100% | 日频置信度低，退回v1.0行为 | insufficient_data→confidence=0.5，不改变信号 |
| **consecutive_miss假阳性** | 中 | 过早预警导致踏空 | 3天阈值可调整；预警≠强制空仓，是降权 |
| **regime切换滞后** | 中 | 趋势已断裂1-2天才预警 | consecutive_miss检测天然有1-2天滞后，但比无检测好 |
| **performance_log数据质量** | 低 | regime计算偏差 | 依赖backfill_outcomes.py正确运行；regime_detector有--live独立验算 |
| **板块涨跌幅计算偏差** | 低 | 少数成分股无价格→板块等权偏差 | 容忍≤20%缺失（pitfall #37约束） |
| **regime_state.json过期** | 中 | 使用旧regime判断 | signal_interface输出中包含generated时间戳；下游使用前检查 |

### 9.2 不会发生的风险

- ❌ **模型IC退化** — 模型权重完全不变
- ❌ **信号排名翻转** — 预测逻辑不变，仅添加元数据
- ❌ **管道阻塞** — 所有regime加载有try/except优雅降级
- ❌ **回滚困难** — regime块是output中的独立字段，删除不影响任何现有consumer

### 9.3 回滚方案

```bash
# 回到v1.0行为：删除regime输出块即可
git checkout HEAD~1 -- signal_interface.py
# 或手动注释掉output中的regime和cheetah_daily块
# 下游consumer忽略未知字段，天然向后兼容
```

---

## 10. 下一步演进路径

### v2.1: 基本面叠加层
- 当regime数据积累>20天，验证 momentum_health 切换逻辑
- 实现：`fundamental_overlay.py`（sector级ROE/负债率/营收增速加权）
- 在warning期自动切到防御性板块

### v2.2: Regime预测（非仅检测）
- 用regime历史标签训练一个简单的regime分类器
- 特征：市场宽度、涨跌比、成交额变化、VIX-like波动
- 目标：在regime断裂前1-3天预警（当前consecutive_miss只能在断裂后检测）

### v2.3: 日频独立模型
- 如果regime检测验证有效，考虑训练一个独立的1d-horizon模型
- 特征工程方向：intraday momentum、overnight gap、auction imbalance
- 与20d模型正交（不同horizon、不同特征集）

---

## 附录A: 与34→134板块切换的类比

| 维度 | 34→134 (v1.0→v1.0.1) | regime集成 (v1.0→v2.0) |
|------|----------------------|------------------------|
| signal_interface改动 | 4行 | ~40行 |
| 模型权重 | 不变 | 不变 |
| 预测逻辑 | 不变 | 不变 |
| 板块排名 | 不变（映射表换了但排名结构同） | 不变 |
| 新增能力 | 更细粒度板块分类 | 信号置信度+日频方向 |
| 下游影响 | 中文名显示变化 | 新增可选字段，向后兼容 |
| 风险 | 低（纯映射替换） | 低（纯元数据追加） |

---

## 附录B: 关键文件路径汇总

```
~/hermes_output/quant/
├── hermes_weekly_v5.pkl          # 生产模型（不变）
├── features_v9.pkl               # 生产特征（不变）
├── features_v10.pkl              # 训练用冻结特征（不变）
├── signal_interface.py           # 信号接口（+40行）
├── regime_detector.py            # 🆕 动量健康度监测器
├── regime_state.json             # 🆕 regime状态输出
├── cheetah_daily.py              # 🆕 日频子产品
├── cheetah_daily_signal.json     # 🆕 日频信号输出
├── latest_signal.json            # 板块信号（新增regime+cheetah_daily块）
├── performance_log.jsonl         # 表现追踪日志（regime_detector消费）
├── backfill_outcomes.py          # 收盘回填（不变）
├── sw_l2_smart_merge_map.pkl     # 板块映射（不变）
└── sw_sector_names.json          # 板块中文名（不变）
```
