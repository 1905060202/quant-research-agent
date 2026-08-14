#!/usr/bin/env python3
"""W7 长程记忆评测运行器：多题共享隔离库，每题独立 -z 新会话。

与 run_qra.py 的差别：不逐题清隔离库（跨会话持久性是本题被测对象），
不做行情基准。开跑前清一次库保证从零状态。

用法：
    .venv-v7/bin/python bench/run_qra_multi.py [--timeout 300]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "bench"))

from run_qra import run_one, ISOLATION  # noqa: E402

BENCH = json.loads(
    (PROJECT / "bench" / "multi_bench.json").read_text(encoding="utf-8")
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    outdir = PROJECT / "bench" / "results" / "multi"
    outdir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(ISOLATION, ignore_errors=True)  # 从零状态，此后三题共享
    ISOLATION.mkdir(parents=True, exist_ok=True)

    questions = BENCH["questions"]
    print(f"共 {len(questions)} 题（共享隔离库 {ISOLATION}，每题独立新会话）")
    for q in questions:
        run_one(q, args.timeout, outdir)
    print("✅ 长程评测跑完。验证：.venv-v7/bin/python bench/score_multi.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
