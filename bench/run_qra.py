#!/usr/bin/env python3
"""QRA 评测运行器：30 题逐题 `hermes -z` 单发新会话。

设计要点：
- 每题独立会话（-z 单发，只看最终回复文本，审批自动放行）
- env 隔离：QRA_MEMORY_DB / QRA_VERIFY_DB → bench/isolation/（防污染真实记忆/验证账本）
- 每题开跑前直调 qra_quote 重采该题涉及的行情（漂移窗口压到秒级）
- --usage-file 逐题落成本 JSON（估算花费/令牌/模型，失败也写）
- 超时 420s/题（R09 全链路工具链最长）

用法：
    .venv-v7/bin/python bench/run_qra.py                     # 全量 30 题
    .venv-v7/bin/python bench/run_qra.py --ids T01,T02       # 只跑指定题
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / ".hermes" / "plugins"))

from collect_baseline import collect_quotes  # noqa: E402

BENCH = json.loads((PROJECT / "bench" / "qra_bench_v1.json").read_text(encoding="utf-8"))
ISOLATION = PROJECT / "bench" / "isolation"
R09_FILE = Path("/tmp/qra_bench_r09.txt")


def gold_quote_symbols(g: dict) -> set[str]:
    """递归收集 gold 里涉及行情基准的标的。"""
    out: set[str] = set()
    if g["type"] == "composite":
        for p in g["parts"]:
            out |= gold_quote_symbols(p)
    elif g["type"] in ("quote", "quote_any"):
        out.update(g["symbols"])
    return out


def run_one(q: dict, timeout: int, outdir: Path) -> None:
    symbols = sorted(gold_quote_symbols(q["gold"]))
    qbase = collect_quotes(symbols) if symbols else {}

    usage = outdir / f"{q['id']}.usage.json"
    cmd = [
        str(PROJECT / "scripts" / "run_qra.sh"),
        "-z", q["prompt"],
        "--usage-file", str(usage),
    ]
    env = dict(os.environ)
    env["QRA_MEMORY_DB"] = str(ISOLATION / "qra_memory.db")
    env["QRA_VERIFY_DB"] = str(ISOLATION / "qra_verify.db")

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=PROJECT, env=env, capture_output=True,
            text=True, timeout=timeout,
        )
        latency = time.monotonic() - t0
        answer = (proc.stdout or "").strip()
        rec = {
            "id": q["id"], "prompt": q["prompt"], "answer": answer,
            "latency_s": round(latency, 1), "exit_code": proc.returncode,
            "quote_baseline": qbase,
            "stderr_tail": (proc.stderr or "")[-800:],
        }
    except subprocess.TimeoutExpired:
        rec = {
            "id": q["id"], "prompt": q["prompt"], "answer": "",
            "latency_s": timeout, "exit_code": "timeout",
            "quote_baseline": qbase,
            "stderr_tail": f"超时 {timeout}s 未完成",
        }

    (outdir / f"{q['id']}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    preview = (rec["answer"] or "")[:90].replace("\n", " ")
    print(f"[{q['id']}] {rec['latency_s']:6.1f}s 退出={rec['exit_code']}  {preview}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="逗号分隔题号，如 T01,T02（缺省全量）")
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args()

    questions = BENCH["questions"]
    if args.ids:
        want = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in want]
        if len(questions) != len(want):
            print(f"⚠️ 只匹配到 {len(questions)}/{len(want)} 题")

    outdir = PROJECT / "bench" / "results" / "qra"
    outdir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(ISOLATION, ignore_errors=True)  # 每题独立会话，但隔离库全局清一次
    ISOLATION.mkdir(parents=True, exist_ok=True)
    R09_FILE.unlink(missing_ok=True)

    print(f"共 {len(questions)} 题，超时 {args.timeout}s/题，隔离库 {ISOLATION}")
    for q in questions:
        run_one(q, args.timeout, outdir)
    print("✅ 全部跑完。评分：.venv-v7/bin/python bench/scorer.py --system qra")
    return 0


if __name__ == "__main__":
    sys.exit(main())
