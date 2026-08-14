# 猎豹v2.0 改进方案：基于日频预测能力的发现

> 版本: v1.0 | 日期: 2026-06-02
> 前提: 5d/10d/20d模型特征重要性完全相同，预测相关性=1.0

## 执行摘要

猎豹v1.0三个horizon学到同一个信号——横截面动量强度排名。改进方向：
1. 废弃多horizon ensemble（无增益）
2. 建立regime检测层（动量延续 vs 断裂）
3. 接受动量探测器定位
4. 孵化猎豹日频子产品

核心新增~380行，3个新文件，2个文件轻量修改。

## 1. 信号层面：废弃多horizon ensemble

发现: 5d/10d/20d特征重要性完全相同，预测值相关性=1.0。
根因: A股横截面动量跨时间尺度稳定。三horizon目标高度共线。
决策: 简化为单一20d模型。signal_interface.py去掉horizon循环(~15行)。

## 2. Regime检测

每日: spread = Top5 Hot次日涨跌幅 - Top5 Cold次日涨跌幅
5日滚动命中率 = (spread>0天数)/5
三分类: >=60% continuation, 40-60% neutral, <=40% break
Confirm机制: 连续2天确认才切换
实现: cheetah_backfill_daily.py + cheetah_regime.py -> regime_status.json

## 3. 特征层面：接受动量探测器定位

Top6特征全部动量/技术指标。基本面特征最高第8名(FI<Top1的50%)。
不是特征不足——20d收益目标天然由动量驱动。
决策: 接受定位，不做强制基本面约束。动量+基本面=互补判断。

## 4. 产品化：猎豹日频

momentum_continuation -> 正常Top5 Hot
momentum_neutral -> 降信号强度
momentum_break -> Top5 Hot转避雷信号，contrarian_warning=true

输出: daily_signal.json

## 5. 可行性

新增: cheetah_backfill_daily.py(~120行) + cheetah_regime.py(~80行) + cheetah_daily.py(~150行)
修改: signal_interface.py(~15行) + log_signal.py(~20行)
总改动~385行，无新依赖，无破坏性改动

## 6. 风险

1. Regime滞后(2.5天): 单日spread异常检测+盘中异动预警
2. 冷启动期: insufficient_data标记+回填脚本
3. Break反转双杀: 连续2天确认+反转信号降强度
4. 数据质量: >=3只有效股票+停牌排除
5. 误解为交易建议: 显式disclaimer

## 7. 文件索引

cheetah_v2_improvement_plan.md - 本方案
cheetah_backfill_daily.py - 历史回填
cheetah_regime.py - Regime检测
cheetah_daily.py - 日频产品
signal_interface.py - 修改(简化horizon+regime字段)
log_signal.py - 修改(regime+累计统计)
regime_status.json - 新增输出
daily_signal.json - 新增输出
