# QRA-Bench v1.0

W5-8 核心产物：30 题 × 3 域（工具 T01-T10 / 知识 K01-K10 / 研究 R01-R10）。
旧分数（77%/97%/100%）作废，新架构从零测。可复算性是硬要求。

## 设计口径

| 项 | 口径 |
|---|---|
| 数值 gold | **动态基准**：评测运行时直调 qra_quote/qra_signal 抓快照（`baseline.json` + 每题开跑前重采 `quote_baseline`），防价格时间衰减 |
| 行情容差 | 价格相对 0.5%，涨跌幅绝对 0.3pp（涨跌幅为 0 时命中 "0" 也算，如实接受） |
| 知识域 gold | 静态要素集（KB 文档核实过的事实），要素命中数分级 |
| 评分粒度 | 0 / 0.5 / 1，复合题取子项平均 |
| 幻觉率 | 两道计数：① 诚实题出现题目之外的数字（年份豁免、题目自带数字回显豁免）② 引用不存在的 .md 文档 |
| 会话隔离 | QRA 每题 `hermes -z` 新会话；记忆/验证库经 QRA_MEMORY_DB/QRA_VERIFY_DB 指向 bench/isolation/，不污染真实账本 |
| 成本 | `--usage-file` 逐题落花费 JSON |
| CC 对比 | 同题同 gold 同评分器；环境差异如实记录（CC 无 qra_* 工具，有 Bash/WebSearch/文件读写，知识域可直读 kb_sources/） |

## 文件

```
qra_bench_v1.json      30 题 + gold 定义（唯一事实源）
collect_baseline.py    基准采集（行情直调新浪 + 信号快照，含全冷榜白酒Ⅱ排名）
scorer.py              机械评分器（0/0.5/1 + 幻觉计数 + 时延聚合）
run_qra.py             QRA 运行器（-z 单发 + env 隔离 + 逐题行情基准 + usage-file）
run_cc.py              CC 运行器（claude -p 独立进程，同题同基准）
baseline.json          运行时生成的基准快照（不入库）
results/<sys>/<id>.json   每题结果 {answer, latency_s, quote_baseline, exit_code}
```

## 运行

```bash
# 1. 基准采集（评测开始前跑一次）
.venv-v7/bin/python bench/collect_baseline.py

# 2. 冒烟（2 题验证管道）
.venv-v7/bin/python bench/run_qra.py --ids T01,T02
.venv-v7/bin/python bench/scorer.py --system qra

# 3. 全量（30 题 × 2 系统，每题独立会话，建议后台跑 + 成本周检）
.venv-v7/bin/python bench/run_qra.py
.venv-v7/bin/python bench/run_cc.py

# 4. 报告
.venv-v7/bin/python bench/scorer.py --system qra    # bench/score_qra.md
.venv-v7/bin/python bench/scorer.py --system cc     # bench/score_cc.md
.venv-v7/bin/python bench/scorer.py --compare       # bench/score_compare.md
```

## 已知边界（诚实声明）

- 板块名匹配做罗马数字后缀归一（白酒Ⅱ≈白酒）；答文说"白酒行业"不含"白酒"二字则不命中
- 排名题容差 ±5；gold 用工具上报的**全池 94 板块全局排名**（白酒Ⅱ=98，不是冷榜位置 7）
- 诚实题若模型把错误代码回显进答文（如 "abc123 不存在"），123 属题目自带数字，豁免
- 信号 stale 判定只查关键词（未更新/过期/stale/陈旧/71），模型说"参数很久没更新"无关键词则封顶 0.5
- 基准采集失败（如停牌）的标的自动满分放行，报告会显示"缺数据"而非错误扣分
