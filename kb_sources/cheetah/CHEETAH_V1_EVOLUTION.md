# 猎豹v1.0 (Cheetah v1.0) 量化模型 · 完整演进文档

> 最后更新: 2026-05-24  
> 作者: Hermes + Claude Code  
> 状态: 🟢 生产运行中

---

## 一、模型演进全史

### v1 — 多任务v1（2026-05-22·已废弃）
- 5任务共享表示（HistGBDT）：次日收益/方向分类/5日收益/波动率/超额收益
- IC虚高（0.83/0.90），确认为全局切分信息泄露
- 教训：IC太高先怀疑泄露，不要先高兴

### v4 — GBR基线（2026-05-23·已归档）
- 44特征纯GBR，严格walk-forward
- Daily Mean IC=0.034±0.020，Blind IC=0.041
- IC decay在t+2起转负——短周期预测力弱
- 关键发现：A股预测力在板块共模中，个股残差收益杀信号
- 模型文件: hermes_multitask_v3.pkl (925KB)

### v5 — 周频截面排名（2026-05-23·已废弃）
- Walk-forward CV IC=0.2343, Blind IC=0.2075
- IC decay单调递减（正常），11个板块分类
- 问题：①LassoCV前向泄露（tail选特征含未来数据）②行业映射仅38% ③板块信号压缩无区分度
- 教训：覆盖率<80%时IC虚高是铁律

### v5 daily — 日频截面排名（2026-05-23·已废弃）
- IC=0.81→确认为数据泄露（dropna导致日期分布偏移）
- 覆盖修复后IC归零到0.08
- 教训：NaN→0虚高IC陷阱——41%覆盖率IC=0.81(假)，100%覆盖率IC=0.08(真)

### v8 — 三层LightGBM（2026-05-24·已归档）
- 大/中/小盘独立LGB模型，sqrt(MV)加权
- 诚实IC=0.06（所有已知bug修复后）
- 教训：修复所有泄露后真实IC远低于直觉预期

### v9/猎豹v1.0 — features_v5.py版（2026-05-24·已废弃）
- P0-1: 24基本面 + P0-2: 多周期ensemble + P0-3: 2512股票池
- 30分钟全量重训，IC崩塌至0.035
- 根因：P0-3股票池盲目扩展引入噪音，小盘从827→2382只微盘垃圾票
- 教训：不是数据越多越好，质量>数量

### 猎豹v1.0 — features_v10.py版（2026-05-24·🟢 生产）
- **纯20日周期**，单一最强信号，不搞ensemble噪音
- 3层市值分层LGB，5折purged walk-forward
- **IC: Large@20d=0.207, Mid@20d=0.254, Small@20d=0.120**
- 5.5分钟重训，172特征（148 PV + 24 funda）
- 1024只预测，34子板块
- 关键设计决策：20日>5日——A股短周期噪音大，中周期信噪比更高

---

## 二、核心架构

```
┌─────────────────────────────────────────────────────┐
│                   猎豹v1.0 架构                       │
├─────────────────────────────────────────────────────┤
│                                                      │
│  [训练] features_v10.py ──→ hermes_weekly_v9.pkl     │
│      │                   └─→ hermes_weekly_v5.pkl    │
│      │  (5.5min, 纯20d LGB)                          │
│      │                                               │
│  [刷新] daily_refresh.py                             │
│      │  Step 0: yfinance拉新行情 (~60s)              │
│      │  Step 1-3: 从raw_1k.pkl算特征 (~60s)          │
│      │  Step 4-5: Rank + Neutralize (~20s)           │
│      │  Save: features_v9.pkl                        │
│      │  (2.3min total, 不重训模型)                   │
│      │                                               │
│  [信号] signal_interface.py                          │
│      │  读features_v9.pkl + hermes_weekly_v5.pkl     │
│      │  3-tier decay-weighted prediction             │
│      └─→ latest_signal.json (<1s)                   │
│                                                      │
│  [消费] 6条产品线                                     │
│      锚点早报 / 预期差距 / 收盘晚报                    │
│      标的分析研报 / 标的分析快答 / 全景深度             │
│                                                      │
└─────────────────────────────────────────────────────┘
```

### 特征体系（172个neut_cols）
- **价格量特征（148个）**：returns多窗口、MA偏离/交叉、波动率、RSI、布林带、ATR、随机指标、Williams %R、CMO、MFI、OBV、新高新低、涨跌天数、回撤、缺口、加速度、成交量比率、效率比、CCI、TRIX、Keltner通道、Alpha/Beta、EOM、偏度、价格位置、MACD、PPO、Sharpe、Amihud
- **基本面特征（24个）**：PE/PB/PS/市值/ROE/ROA/负债率/营收增速/盈利增速/毛利率/净利率/流动比率/速动比率/股息率/自由现金流/经营现金流/EBITDA/企业价值/Beta/做空比率/流通股+衍生EP/BP/SP/EV-EBITDA/P-FCF/对数市值/对数价格/Amihud

### 中性化流程
1. 截面排名（rank pct within date）
2. 市值分层去均值（demean within date+cap_tier）
3. → `{feature}_r_n` = 172个neut_cols

---

## 三、关键决策记录

### 为什么20日周期而不是5日？
- 5日IC=0.058，20日IC=0.126，信号累积效应
- A股短周期噪音淹没问题严重，中周期动量信号浮现
- 对比实验：features_v5.py多周期ensemble(5d/10d/20d)→IC=0.035，features_v10.py纯20d→IC=0.126

### 为什么1024只而不是2512只？
- P0-3扩展引入大量微盘垃圾票，加权IC从0.144崩塌至0.035
- 2512只训练但信号只输出1024只（有cap_tier分类的）
- 教训：股票池质量>数量，微盘票噪音淹没了大中盘信号

### 为什么features_v10.py而不是features_v5.py？
- features_v5.py：多周期ensemble+全量P0-3，30分钟，IC=0.035
- features_v10.py：纯20d LGB，5.5分钟，IC=0.126
- 简洁性带来鲁棒性——单一目标周期避开了ensemble的噪音混合

### 为什么不用update_model.py增量warm-start？
- warm-start严格要求特征数完全匹配（170→172就崩）
- 失败后模型被覆盖为7KB空壳（2026-05-24实战教训）
- 增量特征计算+全量重训(5.5min)比修复warm-start更可靠

### 为什么daily_refresh.py不重训模型？
- 模型参数稳定，特征随行情变化足够产生新信号
- 重训需要full walk-forward验证，2.3min刷新够用
- 数据>7天过期时跑features_v10.py全量重训

---

## 四、失败教训库

| # | 日期 | 问题 | 根因 | 预防 |
|---|------|------|------|------|
| 1 | 0524 | update_model.py --force覆盖模型为空壳 | warm-start特征数170→172不匹配 | 禁用--force，用features_v10.py |
| 2 | 0524 | features_v5.py --full IC崩塌 | P0-3微盘噪音 | 1024只股票池 |
| 3 | 0524 | CC在Hermes不知情时启动update_model | 两人并行操作模型文件 | /tmp/cc_hermes_coordination.md |
| 4 | 0524 | signal_interface.py硬编码IC值 | 未从模型动态读取 | 已修复为ic_decay.get(tier,{}).get(20,0) |
| 5 | 0523 | IC=0.81是数据泄露 | dropna导致日期分布偏移 | NaN用nan_to_num，不dropna |
| 6 | 0523 | LassoCV前向泄露 | tail(100000)选特征含未来数据 | LightGBM原生重要性+fold内筛选 |

---

## 五、运维手册

### 每日操作（交易日收盘后）
```bash
cd ~/hermes_output/quant
python3 daily_refresh.py
# 2.3分钟 → latest_signal.json 更新
```

### 每周/数据过期后（>7天）
```bash
cd ~/hermes_output/quant
python3 features_v10.py
cp hermes_weekly_v9.pkl hermes_weekly_v5.pkl
python3 signal_interface.py
# 5.5分钟 → 模型+信号全量刷新
```

### 紧急恢复
```bash
# 如果hermes_weekly_v5.pkl损坏
cp hermes_weekly_v5_prod_backup.pkl hermes_weekly_v5.pkl
cp hermes_weekly_v9_prod_backup.pkl hermes_weekly_v9.pkl
```

### 验证模型健康
```bash
python3 signal_interface.py 2>&1 | grep -E "IC|neut_cols|Decay|FATAL"
# 期望: IC>0.05, Decay=True, 无FATAL, 172 neut_cols
```

---

## 六、文件清单

| 文件 | 大小 | 用途 |
|------|------|------|
| `hermes_weekly_v5.pkl` | 16MB | 生产模型（3层LGB） |
| `hermes_weekly_v9.pkl` | 16MB | features_v10.py输出→cp到v5 |
| `features_v9.pkl` | 2.8GB | 特征缓存（df_ranked + neut_cols） |
| `raw_1k.pkl` | 22MB | 1030只股票OHLCV源数据 |
| `features_v10.py` | 7KB/125行 | 20d纯LGB训练脚本 |
| `daily_refresh.py` | 24KB/570行 | 每日特征刷新管道 |
| `signal_interface.py` | 15KB/384行 | 信号输出（latest_signal.json） |
| `latest_signal.json` | 11KB | 34子板块信号 |
| `subsector_map_merged.pkl` | - | 股票→子板块映射 |

### 备份
- `hermes_weekly_v5_prod_backup.pkl` — 62MB（features_v5版）
- `hermes_weekly_v9_prod_backup.pkl` — 16MB（features_v10版）
- `archive/features_v9_*` — 特征文件历史版本

---

## 七、禁止操作

```bash
# 🚫 以下命令会导致模型损坏或IC崩塌
python3 update_model.py --force    # warm-start特征不匹配→模型损坏
python3 update_model.py --full     # 30min且IC=0.035
python3 features_v5.py             # 同上
```

---

## 八、CC协作


## 九、表现追踪系统（2026-05-29启用）

### 设计目标
积累每日预测 vs 实际表现数据，为模型升级提供实证基础。

### 追踪链路
```
pipeline_v101.sh → log_signal.py → performance_log.jsonl (信号记录)
收盘后 → backfill_outcomes.py → performance_log.jsonl (回填实际)
```

### 追踪指标
| 指标 | 说明 |
|------|------|
| 方向准确率 | sentiment方向 vs 次日创业板涨跌 |
| Hot-Cold spread | Hot Top5 平均涨幅 - Cold Bot5 平均涨幅 |
| 板块命中率 | Hot板块实际跑赢 Cold板块的比例 |
| 累计趋势 | 滚动窗口准确率变化 |

### 文件
- `performance_log.jsonl` — 核心数据，每行一天
- `log_signal.py` — pipeline自动调用
- `backfill_outcomes.py` — 收盘后手动/脚本调用

### 首批数据
- 2026-05-29: slightly_bullish, Hot=49, Cold=41, top5=文娱用品/建筑装饰/电力设备综合


## 十、CC协作（不限工具/不限预算/不限时间）
- Hermes负责：需求表达+结果验收+产品线集成
- 协调文件：`/tmp/cc_hermes_coordination.md`
- 任务文件：`/tmp/cc_daily_refresh_task.md`
- **铁律**：CC和Hermes不能同时操作模型文件
