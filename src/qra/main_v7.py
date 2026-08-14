import os
"""QRA v7 · 入口（推倒重来版）

架构（v6 设计）：
- 核心循环 = QraLoop（调模型→跑工具→重复）
- 事件钩子 = 记忆预取 / 评审学习 / 指标记录
- 记忆 = 字符预算 JSON（Hermes 模式）
- 检索 = FTS5 trigram（Hermes 模式）
- 评审 = 双评审门（prime 模式）
"""
import sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os

from loop.agent_loop import QraLoop
import loop.hooks  # 注册钩子（import 即注册）
from core.events import SessionLog

if __name__ == "__main__":
    sl = SessionLog(os.path.expanduser("~/hermes_output/career/tools/quant_research_agent/data/session.jsonl"))
    loop = QraLoop(session_log=sl)

    tests = sys.argv[1:] or [
        "查一下 sz159558 的现价",
        "怎么做多假设竞争？",
        "同时查一下 sz159558 和 sh000001",
    ]
    for q in tests:
        t0 = time.time()
        r = loop.run(q)
        print(f"Q: {q}")
        print(f"A({time.time()-t0:.1f}s): {r['final_answer'][:200]}")
        print()
    print("=== 指标 ===")
    print(json.dumps(loop.metrics, ensure_ascii=False))
