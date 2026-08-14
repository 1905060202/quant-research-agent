"""QRA 信号工具：读猎豹 v2.1 最新信号快照 latest_signal.json。

数据源：~/hermes_output/quant/latest_signal.json（daily_refresh 管道生成）。
不重算信号，只做可信摘要——模型的 IC 指标、数据新鲜度、HOT/COLD
板块榜、个股排名，全部原样转述并带上新鲜度诚实标记（is_stale）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_SIGNAL_PATH = Path("~/hermes_output/quant/latest_signal.json").expanduser()

MAX_HOT_COLD = 20  # 防手滑返回全榜（51+43 条），agent 上下文有限


def _resolve_path(raw: str | None) -> Path:
    """路径优先级：调用参数 path > 环境变量 QRA_SIGNAL_PATH > 默认猎豹路径。"""
    if raw:
        return Path(raw).expanduser()
    env = os.environ.get("QRA_SIGNAL_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    return DEFAULT_SIGNAL_PATH


def _clip_tier(tiers: list, top_n: int) -> list:
    """板块榜裁剪成 agent 友好的短表。"""
    return [
        {
            "display_name": t.get("display_name", ""),
            "signal": round(float(t["signal"]), 3),
            "rank": t.get("rank"),
        }
        for t in tiers[:top_n]
    ]


def qra_signal(args: dict, **_kw) -> str:
    """QRA 信号工具 handler：返回猎豹信号摘要 JSON。

    Args:
        args: {"top_n": 10(默认), "code": "600710"(可选，查个股排名), "path": 可选}
    """
    try:
        top_n = int(args.get("top_n") or 10)
    except (TypeError, ValueError):
        top_n = 10
    top_n = max(1, min(top_n, MAX_HOT_COLD))
    code = str(args.get("code", "") or "").strip() or None

    path = _resolve_path(args.get("path"))
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return json.dumps(
            {"error": f"信号文件不存在：{path}（猎豹管道未生成或路径不对）"},
            ensure_ascii=False,
        )
    except json.JSONDecodeError as e:
        return json.dumps(
            {"error": f"信号文件不是合法 JSON：{e}"}, ensure_ascii=False
        )

    hot = d.get("subsector_tiers", {}).get("hot", [])
    cold = d.get("subsector_tiers", {}).get("cold", [])
    freshness = d.get("model_freshness", {})

    digest = {
        "model": d.get("model_name") or d.get("model"),
        "generated": d.get("generated"),
        "last_data_date": d.get("last_data_date"),
        # 诚实标记：模型参数是否过期（数据是每日刷新，参数不一定是）
        "model_is_stale": bool(freshness.get("is_stale")),
        "days_since_model_update": freshness.get("days_since_update"),
        "market_score": d.get("market_score"),
        "market_sentiment": d.get("market_sentiment"),
        "regime": d.get("regime", {}).get("status"),
        "hot_top": _clip_tier(hot, top_n),
        "cold_top": _clip_tier(cold, top_n),
        "summary": d.get("summary", ""),
    }

    if code:
        for sp in d.get("stock_predictions", []):
            if str(sp.get("code")) == code or str(sp.get("symbol", "")).split(".")[0] == code:
                digest["stock"] = {
                    "code": code,
                    "symbol": sp.get("symbol"),
                    "pred_rank": sp.get("pred_rank"),
                    "cap_tier": sp.get("cap_tier"),
                }
                break
        if "stock" not in digest:
            digest["stock"] = {"code": code, "error": "该代码不在预测池内（universe 5176 只）"}

    return json.dumps(digest, ensure_ascii=False)
