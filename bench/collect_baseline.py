#!/usr/bin/env python3
"""QRA-Bench 基准采集：评测开始时抓工具快照，作为 gold 动态基准。

为什么动态：行情价格会随时间衰减，硬编码 gold 会误杀正确答案。
所以数值 gold = 评测当日工具自身输出的快照（qra_quote / qra_signal 直调）。

用法：
    .venv-v7/bin/python bench/collect_baseline.py            # 全量采集 → bench/baseline.json
    .venv-v7/bin/python bench/collect_baseline.py --symbols sh600519   # 只采指定行情

运行器（run_qra.py）在每题开跑前会重新直调 qra_quote 采该题涉及标的，
把漂移窗口缩到秒级（写入每题结果 JSON 的 quote_baseline 字段）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / ".hermes" / "plugins"))

from qra.quote import qra_quote  # noqa: E402
from qra.signal import DEFAULT_SIGNAL_PATH  # noqa: E402

# 30 题全部涉及的行情标的（题面+评分均以这些为基准）
QUOTE_SYMBOLS = ["sh600519", "sh000001", "sh600176", "sh601899", "sz002414"]


def collect_quotes(symbols: list[str] | None = None) -> dict:
    """直调 qra_quote 抓现价/涨跌幅（新浪实时）。失败标 error 不中断。"""
    out = {}
    for s in symbols or QUOTE_SYMBOLS:
        try:
            d = json.loads(qra_quote({"symbol": s}))
        except Exception as e:  # 兜底：采集本身不该让评测崩
            out[s] = {"error": f"{type(e).__name__}: {e}"}
            continue
        out[s] = {
            "price": d.get("price"),
            "change_pct": d.get("change_pct"),
            "error": d.get("error"),
        }
    return out


def collect_signal() -> dict:
    """读猎豹信号快照：HOT 前 5 + COLD 全榜（白酒Ⅱ 排名可能超出 top_n 裁剪）。"""
    d = json.loads(Path(DEFAULT_SIGNAL_PATH).read_text(encoding="utf-8"))
    tiers = d.get("subsector_tiers", {})
    hot = tiers.get("hot", [])
    cold = tiers.get("cold", [])
    freshness = d.get("model_freshness", {})

    def tier(t):
        return {"name": t.get("display_name", ""), "score": t.get("signal")}

    def find_rank(tiers_list, key):
        """工具上报口径：rank 是全池 94 板块全局排名（hot 51 + cold 43），
        不是榜内位置序号。agent 看到的就是这个数，gold 必须对齐。"""
        for t in tiers_list:
            name = t.get("display_name", "")
            if key in name or name.rstrip("ⅠⅡⅢⅣⅤ") == key.rstrip("ⅠⅡⅢⅣⅤ"):
                return t.get("rank")
        return None

    return {
        "hot_top5": [tier(t) for t in hot[:5]],
        "cold_all": [tier(t) for t in cold],
        "cold_len": len(cold),
        "baijiu2_rank": find_rank(cold, "白酒Ⅱ"),
        "model_is_stale": bool(freshness.get("is_stale")),
        "days_since_model_update": freshness.get("days_since_update"),
        "last_data_date": d.get("last_data_date"),
        "generated": d.get("generated"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--out", default=str(PROJECT / "bench" / "baseline.json"))
    args = ap.parse_args()

    if args.symbols:  # 只采行情模式
        out = {"quotes": collect_quotes(args.symbols)}
    else:
        out = {"quotes": collect_quotes(), "signal": collect_signal()}

    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"✅ 基准已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
