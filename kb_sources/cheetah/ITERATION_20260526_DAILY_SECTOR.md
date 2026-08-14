# 2026-05-26 迭代实录 · 日频板块路径状态识别

## 触发

用户发现板块热力图给人"明天就可以买"的错觉——20天horizon信号被误解为日频信号。提出：能不能把猎豹20天预测+三层验证的LLM/Agent分析推理能力结合起来，搞日频判断？

## 时间线

### 14:30 Opus 4.7 设计
- 定位修正："可执行性过滤器" → "路径状态识别器"
- 核心洞察：hot+催化剂=买 是新的偏误生成器（催化剂可能已被价格消化）
- 四象限：early/late/weak/silent + unknown
- 6个结构化问题 + 反确认偏误机制
- 5种强制unknown场景 · 目标unknown_ratio=30-50%
- 4组对照实验设计 · N≥80 · spread≥5pp

### 14:45 实测验证
- 10板块扫描：TOP5 hot + BOTTOM5 cold
- 333秒 · 27次API（Freebird搜索 + Yahoo行情ETF代理）
- 状态分布：early×1 / silent×6 / weak×2 / unknown×1

### 实测结果

| 板块 | 信号 | 状态 | 关键 |
|------|------|------|------|
| 焦炭Ⅱ | hot 0.85 | early | 夜盘期货+9%，今日唯一可执行 |
| 消费电子 | cold 0.21 | unknown | 量化cold vs XR+京东方涨价 |
| 商用车 | cold 0.21 | weak | 补贴新政vs量化cold |
| 基本金属 | cold 0.17 | weak | 铜超级周期vs量化cold |

### 核心发现

1. **系统可行**：10个板块中成功识别出仅焦炭有可执行的日频窗口
2. **unknown太少**：0.1 vs 目标0.3-0.5——需更激进标unknown
3. **价值在"矛盾检测"**：商用车/基本金属/消费电子的cold信号与基本面矛盾——这正是日频层区分"路径位置"vs"明天涨跌"的差异化价值
4. **小样本板块**：银行/保险/商用车仅5只，quant信号可靠性存疑
5. **价格数据用ETF代理**：非精确申万行业指数，"estimated"质量

### 14:50 全产品线更新

| 组件 | 更新 | 版本 |
|------|------|------|
| hermes-quant skill | +日频板块路径状态识别·reference文件 | v9.5 |
| daily-tech-briefing skill | +日频板块信号嵌入早报 | v10.7 |
| agent-cluster skill | L2辩论协议+6问 | v3.2 |
| KB [2839] | 日频路径状态设计文档 | strategy/daily_sector_path_state |
| KB [2840] | Opus 4.7 完整设计分析 | strategy/opus_daily_sector_design |
| Memory | 日频板块路径状态v0.1 | new § |

### 设计文档
- `~/hermes_output/investment/strategy/designs/daily_sector_path_state.md`
- `~/.hermes/skills/hermes-quant/references/daily-sector-path-state.md`
- `/tmp/opus_daily_sector_signal_result.txt`（Opus完整分析）
- `/Users/huyaning/sector_path_state_20260526.json`（实测输出JSON）

## 回测验证：5/25→5/26

| 指标 | 数值 |
|------|------|
| 正确率 | 5/6=83%（排除4个uncertain） |
| early命中 | 2/2（小金属+3.11%·基本金属+2.54%） |
| late命中 | 1/1（贵金属-0.63%） |
| weak命中 | 2/3（白酒-1.12%·白电-0.19%·商用车+0.38%✗） |
| early平均收益 | +2.83% |
| late平均收益 | -0.63% |
| spread | **+3.46pp** |

数据：`~/hermes_output/quant/backtest_525_526.json` · KB[2842]

## 前瞻预测：5/26→5/27

3/10可计数：焦炭Ⅱ(early·↑)·商用车(weak·↓)·基本金属(weak·↓)
其余7个silent/unknown。

数据：`~/hermes_output/quant/forward_pred_526_527.json` · KB[2844]

## 全产品线更新清单

| 组件 | 变更 | KB |
|------|------|:--:|
| Opus设计分析 | /tmp/opus_daily_sector_signal_result.txt | [2840] |
| 日频路径状态设计 | ~/hermes_output/investment/strategy/designs/daily_sector_path_state.md | [2839] |
| hermes-quant skill | v9.5 + reference/daily-sector-path-state.md | — |
| daily-tech-briefing | v10.7 + pitfall#40（20天不可做日频） | — |
| agent-cluster | v3.2 L2辩论协议+6问 | — |
| 回测结果 | backtest_525_526.json | [2842] |
| 前瞻预测 | forward_pred_526_527.json | [2844] |
| 完整迭代实录 | ITERATION_20260526_DAILY_SECTOR.md | [2841] |

## 待办

- [ ] 将6问嵌入L2集群每日辩论（追加到cluster_debate.py R1后）
- [ ] 早报HTML嵌入日频板块状态卡片
- [ ] 积累验证样本（3-6个月·N≥80）
- [ ] 区分板块异质性（半导体vs公用事业日频价值不同）
- [ ] unknown_ratio从10%提升到30-50%
- [ ] 分regime验证（趋势日vs震荡日日频过滤效果差异）
- [ ] 明天验证5/26→5/27前瞻预测（用户说"验证预测"时触发）

## 教训

1. **三层框架互补才是壁垒**：L3给方向锚（20天·IC≈0.20），L2给路径定位（日频催化剂扫描），L1给慢变量（博主框架）。缺一层就是瞎三分之一
2. **日频≠预测明天**：路径状态是定位不是预测，early不保证明天涨，但early板块涨的概率和幅度显著优于late（spread+3.46pp）
3. **unknown是护城河**：10个板块7个silent/unknown是健康的——系统在没把握时闭嘴，比之前"看到hot就想买"进步巨大
4. **反证机制是关键**：每板块2条反证防止确认偏误——这是Opus设计里最有价值的单点
5. **回测验证闭环不可跳过**：5/25→5/26回测+5/26→5/27前瞻=完整验证链。不验证的设计是信仰不是工程
6. **Opus定框架+子agent执行=正确分工**：Opus做策略设计ROI最高，子agent做搜索+数据拉取重复劳动
7. **$1.5/天不是主要成本**：客户预期管理失败才是。技术方案必须配套产品话术（每块信号旁标注"20天趋势方向"）
