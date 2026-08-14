# W1 mini项目①：行情工具调用 Agent（骨架已就绪）

> 目标：输入"查一下 sz159558 的现价和涨跌幅" → Agent 决定调用行情工具 → 返回结构化结果
> 技术：LangGraph（StateGraph）· 新浪行情 API · 工具节点模式
> 完成标准：命令行可运行；metrics.md 记录任务成功率与延迟

## 目录
- main.py —— 入口（命令行交互）
- state.py —— LangGraph 状态定义
- tools/quote.py —— 行情工具（新浪 API 封装）
- agent.py —— Agent 节点（工具选择+调用+回答）

## 使用
```bash
# 安装依赖
pip install langgraph langchain-core requests

# 运行
python main.py "查一下 sz159558 的现价和涨跌幅"
python main.py "今天上证指数怎么样"
```

## 下一步（自己完成的部分）
- 读取官方文档：StateGraph / add_node / add_edge / conditional_edges
- 把 tools/quote.py 换成你自己的行情封装（sina_quote 技能里已有现成逻辑）
- 加入"思考链"输出（Agent 先想：这个问题需要工具吗？哪个工具？）
- 在 reports/metrics.md 记录第一次运行的成功率和延迟
