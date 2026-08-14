#!/usr/bin/env python3
"""W7 长程记忆评测验证：复用 scorer.score_gold，逐题打印得分与原文摘录。

用法：.venv-v7/bin/python bench/score_multi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "bench"))

from scorer import score_gold  # noqa: E402

BENCH = json.loads(
    (PROJECT / "bench" / "multi_bench.json").read_text(encoding="utf-8")
)


def main() -> int:
    outdir = PROJECT / "bench" / "results" / "multi"
    scores = []
    for q in BENCH["questions"]:
        rf = outdir / f"{q['id']}.json"
        if not rf.is_file():
            print(f"{q['id']}: ❌ 无结果")
            scores.append(0.0)
            continue
        r = json.loads(rf.read_text(encoding="utf-8"))
        answer = r.get("answer", "") or ""
        s = score_gold(q["gold"], answer, q["prompt"], {}, {})
        scores.append(s)
        preview = answer[:150].replace("\n", " ")
        print(f"{q['id']}: {s*100:.0f}%  {preview}")
    avg = sum(scores) / len(scores) * 100
    print(f"\n长程记忆总分: {avg:.0f}%")
    return 0 if avg == 100 else 1


if __name__ == "__main__":
    sys.exit(main())
