#!/usr/bin/env python3
"""Claude Code 同题评测运行器：与 QRA 同题、同 gold、同评分器。

环境差异（如实记录，不做公平性伪装）：
- CC 没有 qra_* 专属工具，但有 Bash/WebSearch/WebFetch/文件读写
- 知识域题目 CC 可直接读本项目 kb_sources/*.md（这是 CC 的真实环境能力）
- 行情题 CC 需自寻数据源（网络搜索），数值容差与 QRA 相同（相对 0.5%）
- CC 每题为独立 `claude -p` 进程（无会话记忆，与 QRA -z 新会话对齐）

用法：
    .venv-v7/bin/python bench/run_cc.py                     # 全量 30 题
    .venv-v7/bin/python bench/run_cc.py --ids T01,T02       # 只跑指定题
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / ".hermes" / "plugins"))

from collect_baseline import collect_quotes  # noqa: E402
from run_qra import BENCH, gold_quote_symbols  # noqa: E402


def run_one(q: dict, timeout: int, outdir: Path) -> None:
    symbols = sorted(gold_quote_symbols(q["gold"]))
    qbase = collect_quotes(symbols) if symbols else {}

    cmd = [
        "claude", "-p", q["prompt"],
        "--output-format", "text",
        "--dangerously-skip-permissions",
    ]
    env = dict(os.environ)

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

    outdir = PROJECT / "bench" / "results" / "cc"
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"共 {len(questions)} 题，超时 {args.timeout}s/题（claude -p 独立进程）")
    for q in questions:
        run_one(q, args.timeout, outdir)
    print("✅ 全部跑完。评分：.venv-v7/bin/python bench/scorer.py --system cc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
