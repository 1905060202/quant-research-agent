#!/usr/bin/env python3
"""成本周检（W9-12 审计遗产：#1 连续 2 周 >¥350/周 → 停机复查）

数据源：hermes --usage-file 输出的 JSON（bench/results/qra/*.usage.json 等）。
estimated_cost_usd 恒为 0.0（cost_status=unknown），所以按 tokens × 价格表估算。

用法：
    .venv-v7/bin/python scripts/check_cost.py [目录]          # 目录下所有 *.usage.json
    .venv-v7/bin/python scripts/check_cost.py --since 7       # 只看最近 7 天（mtime）

价格表默认 DeepSeek 公开标准价（美元/M tokens），可用 --prices 覆盖。
⚠️ 估算口径，真实成本以服务商账单为准——本脚本用于周检趋势与阈值报警。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# DeepSeek 公开标准价（$/1M tokens）：输入(cache miss) / 输出 / 缓存读(cache hit)
DEFAULT_PRICES = {"input": 0.27, "output": 1.10, "cache_read": 0.027}
WEEKLY_BUDGET_CNY = 350.0
USD_CNY = 7.2  # 汇率固定口径，周检够用
HISTORY = Path(__file__).resolve().parents[1] / "data" / "cost_history.json"


def load_usage_files(directory: Path, since_days: int | None) -> list[dict]:
    files = sorted(directory.glob("*.usage.json"))
    if since_days is not None:
        cutoff = time.time() - since_days * 86400
        files = [f for f in files if f.stat().st_mtime >= cutoff]
    usages = []
    for f in files:
        try:
            usages.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return usages


def estimate_cost(u: dict, prices: dict) -> float:
    """单次运行成本估算（USD）"""
    miss = u.get("input_tokens", 0) + u.get("cache_write_tokens", 0)
    hit = u.get("cache_read_tokens", 0)
    out = u.get("output_tokens", 0)
    return (
        miss * prices["input"] + hit * prices["cache_read"] + out * prices["output"]
    ) / 1_000_000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", nargs="?", default="bench/results/qra")
    ap.add_argument("--since", type=int, default=None, help="只看最近 N 天（mtime）")
    ap.add_argument(
        "--prices",
        default=None,
        help="价格覆盖 JSON：{\"input\":..,\"output\":..,\"cache_read\":..}",
    )
    args = ap.parse_args()

    prices = DEFAULT_PRICES
    if args.prices:
        prices.update(json.loads(args.prices))

    d = Path(args.directory)
    if not d.is_dir():
        print(f"❌ 目录不存在：{d}")
        return 2
    usages = load_usage_files(d, args.since)
    if not usages:
        print(f"{d} 下没有 usage 文件（--since {args.since}）")
        return 1

    total = {
        "runs": len(usages),
        "input": sum(u.get("input_tokens", 0) for u in usages),
        "output": sum(u.get("output_tokens", 0) for u in usages),
        "cache_read": sum(u.get("cache_read_tokens", 0) for u in usages),
        "cache_write": sum(u.get("cache_write_tokens", 0) for u in usages),
        "api_calls": sum(u.get("api_calls", 0) for u in usages),
    }
    cost_usd = sum(estimate_cost(u, prices) for u in usages)
    cost_cny = cost_usd * USD_CNY

    print(f"成本周检（{d}，{total['runs']} 次运行）")
    print(f"  tokens：输入 {total['input']:,} / 输出 {total['output']:,} / "
          f"缓存读 {total['cache_read']:,} / 缓存写 {total['cache_write']:,}")
    print(f"  API 调用 {total['api_calls']} 次")
    print(f"  估算成本：${cost_usd:.2f} ≈ ¥{cost_cny:.2f}（预算 ¥{WEEKLY_BUDGET_CNY}/周）")

    # 阈值 + 连续两周判定（历史记录）
    weeks_over = []
    if HISTORY.is_file():
        try:
            hist = json.loads(HISTORY.read_text(encoding="utf-8"))
            weeks_over = [w for w in hist.get("weeks", []) if w.get("over_budget")]
        except Exception:
            weeks_over = []
    if cost_cny > WEEKLY_BUDGET_CNY:
        if len(weeks_over) >= 1:
            print(f"  🔴 连续 ≥2 周超预算 → 按审计遗产停机复查！")
        else:
            print(f"  ⚠️ 本周超预算（第 1 周），记录中")
        weeks_over.append({"date": time.strftime("%Y-%m-%d"), "cost_cny": cost_cny})
    else:
        print(f"  ✅ 预算内")
        weeks_over = []  # 未超预算则重置连续计数
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps({"weeks": weeks_over}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
