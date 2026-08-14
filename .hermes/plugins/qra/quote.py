"""QRA 行情工具：新浪 hq.sinajs.cn 实时行情。

移植自猎豹量化 daily_refresh.py 的已验证模式：
- 必须带 Referer 头（https://finance.sina.com.cn），否则新浪拒绝
- 返回 GBK 编码，必须 decode('gbk')
- 字段位置陷阱：0=名称 1=今开 2=昨收 3=现价 4=最高 5=最低
  8=成交量(股) 9=成交额(元) 30=日期 31=时间
- 停牌股现价为 0 且部分字段为空，需显式处理
"""

from __future__ import annotations

import json
import re
import urllib.request

SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbol}"
SINA_REFERER = "https://finance.sina.com.cn"
TIMEOUT = 8


def _normalize_symbol(raw: str) -> str:
    """把用户输入归一化成新浪符号（sh600519 / sz000001）。

    接受：600519、sh600519、SZ000001、000001.SZ 等写法。
    不支持北交所（新浪 hq 无 bj 源），返回错误信息。
    """
    s = raw.strip().lower()
    # 去掉常见后缀格式 yahoo/akshare：600519.SH -> sh600519
    if "." in s:
        code, _, mkt = s.partition(".")
        s = (mkt.lower() + code) if mkt.lower() in ("sh", "sz") else s
    # 已有前缀
    if re.fullmatch(r"sh\d{6}|sz\d{6}", s):
        return s
    # 纯 6 位数字，按 A 股规则推断交易所
    if re.fullmatch(r"\d{6}", s):
        code = s
        if code.startswith(("60", "68", "90")):
            return "sh" + code
        if code.startswith(("00", "30", "20")):
            return "sz" + code
        raise ValueError(
            f"无法推断 {raw} 的交易所：北交所(8/4开头)新浪源不支持；"
            "请用 sh600519 / sz000001 显式指定"
        )
    raise ValueError(f"无法识别的股票代码：{raw}（示例：600519 或 sh600519）")


def _fetch_realtime(sina_sym: str) -> dict:
    """拉取实时行情，返回规范化 dict。抛异常时由上层转成错误文本。"""
    url = SINA_QUOTE_URL.format(symbol=sina_sym)
    req = urllib.request.Request(url, headers={"Referer": SINA_REFERER})
    data = urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("gbk")
    # 格式: var hq_str_sh600519="贵州茅台,今开,昨收,现价,最高,最低,...,日期,时间";
    m = re.search(r'=.*?"([^"]*)"', data)
    if not m or not m.group(1):
        raise ValueError(f"{sina_sym} 无数据（代码有误或非交易时段返回空）")
    fields = m.group(1).split(",")
    if len(fields) < 32:
        raise ValueError(f"{sina_sym} 返回字段异常（{len(fields)} 列）")

    name, open_, prev_close, price, high, low = fields[0:6]
    volume_shares, amount_yuan = fields[8], fields[9]
    date_, time_ = fields[30], fields[31]

    # 停牌特征：现价为 0 或 0.000，且时间戳为空
    if float(price) <= 0 and not time_:
        return {
            "symbol": sina_sym,
            "name": name,
            "suspended": True,
            "date": date_,
            "note": "已停牌或无实时行情",
        }

    price_f = float(price)
    prev_f = float(prev_close) if prev_close else price_f
    change = price_f - prev_f
    change_pct = (change / prev_f * 100) if prev_f else 0.0
    return {
        "symbol": sina_sym,
        "name": name,
        "price": round(price_f, 3),
        "open": float(open_) if open_ else None,
        "prev_close": round(prev_f, 3),
        "high": float(high) if high else None,
        "low": float(low) if low else None,
        "volume_shares": int(float(volume_shares)) if volume_shares else None,
        "amount_yuan": float(amount_yuan) if amount_yuan else None,
        "change": round(change, 3),
        "change_pct": round(change_pct, 2),
        "date": date_,
        "time": time_,
        "suspended": False,
    }


def qra_quote(args: dict, **_kw) -> str:
    """QRA 行情工具 handler：返回 JSON 文本。

    Hermes 插件工具调用约定（与 bundled spotify/google_meet 一致）：
    handler 第一个位置参数是模型填的参数 dict，框架注入的
    task_id / session_id / user_task 等额外 kwarg 由 **_kw 吸收。
    registry.dispatch() 是唯一调用路径，不要按 typed-kwargs 写。

    Args:
        args: 模型填写的参数 dict，含 symbol（如 600519、sh600519、000001.SZ）
    """
    symbol = str(args.get("symbol", "")).strip() if isinstance(args, dict) else str(args)
    if not symbol:
        return json.dumps(
            {"error": "缺少 symbol 参数：请提供股票代码，如 600519 或 sh600519"},
            ensure_ascii=False,
        )
    try:
        sina_sym = _normalize_symbol(symbol)
        quote = _fetch_realtime(sina_sym)
        return json.dumps(quote, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except urllib.error.URLError as e:
        return json.dumps(
            {"error": f"新浪行情请求失败（网络或限流）：{type(e).__name__}"},
            ensure_ascii=False,
        )
    except Exception as e:  # 兜底：行情接口任何异常都不该让 agent 崩
        return json.dumps(
            {"error": f"行情解析异常：{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )
