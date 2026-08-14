"""AutoGen 小组 CLI：python agents/run_team.py "任务描述"

在 src/qra/ 下运行。
"""
import asyncio
import sys

from team import run


async def main() -> None:
    task = sys.argv[1] if len(sys.argv) > 1 else "给我一份今日 A 股速览，附方法论要点"
    print(f"任务：{task}\n" + "=" * 60)
    print(await run(task))


if __name__ == "__main__":
    asyncio.run(main())
