"""对比实验（论文锚点 2）：同一任务 × 两种编排

- LangGraph 版：单 agent 图式编排（agent_node 内手写循环，工具 registry）
- AutoGen 版：三人小组对话式编排（RoundRobinGroupChat + TERMINATE）

记录：时延 / 工具调用次数（AutoGen 数 ToolCallExecutionEvent）/ 结论对比。
对比的价值不在"谁赢"，在看清两种编排的成本结构差异（面试弹药）。

用法（src/qra/ 下）：../../.venv-v7/bin/python agents/run_compare.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import app  # noqa: E402  LangGraph 编译图
from team import build_team, human_checkpoint, _extract_conclusion  # noqa: E402

TASKS = [
    "查一下比亚迪最新价",
    "查一下贵州茅台现价，并说明做量化研究为什么要多假设竞争",
]


def run_langgraph(task: str) -> tuple[str, float, int]:
    t0 = time.time()
    result = app.invoke({"messages": [{"role": "user", "content": task}]})
    dt = time.time() - t0
    tools = len(result.get("tool_log", []))
    return result.get("final_answer", ""), dt, tools


async def run_team_measured(task: str) -> tuple[str, float, int]:
    t0 = time.time()
    result = await build_team().run(task=task)
    dt = time.time() - t0
    # 数工具执行事件（analyst/librarian 的 FunctionTool 真执行次数）
    tool_calls = sum(
        1
        for m in result.messages
        if m.type == "ToolCallExecutionEvent"
        and getattr(m, "source", "") in ("analyst", "librarian")
    )
    conclusion = _extract_conclusion(result.messages)
    if not conclusion:
        for m in reversed(result.messages):
            t = getattr(m, "content", "")
            if isinstance(t, str) and t.strip():
                conclusion = t.strip()
                break
    passed, reason = human_checkpoint(conclusion)
    verdict = "✅" if passed else f"⛔({reason})"
    return f"{verdict} {conclusion}", dt, tool_calls


async def main() -> None:
    print("=" * 70)
    print("对比实验：LangGraph 单 agent（图式） vs AutoGen 小组（对话式）")
    print("=" * 70)
    for i, task in enumerate(TASKS, 1):
        print(f"\n### 任务 {i}：{task}\n")
        ans_lg, dt_lg, tools_lg = run_langgraph(task)
        ans_team, dt_team, tools_team = await run_team_measured(task)
        print(f"--- LangGraph（{dt_lg:.1f}s，工具 {tools_lg} 次）---")
        print("  " + ans_lg[:320].replace("\n", "\n  "))
        print(f"\n--- AutoGen 小组（{dt_team:.1f}s，工具 {tools_team} 次）---")
        print("  " + ans_team[:320].replace("\n", "\n  "))
    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
