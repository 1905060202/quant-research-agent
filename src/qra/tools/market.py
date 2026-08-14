"""行情工具：新浪实时行情（移植自主项目 .hermes/plugins/qra/quote.py 已验证模式）。

已验证的坑（照抄自己项目的教训）：
- 必须带 Referer 头，否则新浪 403
- 返回 GBK，必须 decode('gbk')
- 字段位置：0=名称 1=今开 2=昨收 3=现价 4=最高 5=最低 8=成交量(股) 30=日期 31=时间
- 停牌股现价为 0，需显式提示
"""
from __future__ import annotations

import re
import urllib.request

SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbol}"
SINA_REFERER = "https://finance.sina.com.cn"
TIMEOUT = 8


def _normalize_symbol(raw: str) -> str:
    """归一化成新浪符号：600519→sh600519、sz159558→sz159558、600519.SH→sh600519"""
    s = raw.strip().lower()
    if "." in s:
        code, _, mkt = s.partition(".")
        if mkt.lower() in ("sh", "sz"):
            s = mkt.lower() + code
    if re.fullmatch(r"sh\d{6}|sz\d{6}", s):
        return s
    if re.fullmatch(r"\d{6}", s):
        if s.startswith(("60", "68", "90")):
            return "sh" + s
        if s.startswith(("00", "30", "20")):
            return "sz" + s
        raise ValueError(f"无法推断交易所（北交所新浪源不支持），请写 sh/sz 前缀：{raw}")
    raise ValueError(f"无法识别的代码：{raw}（示例：600519 或 sh600519）")


def _fetch(sina_sym: str) -> str:
    url = SINA_QUOTE_URL.format(symbol=sina_sym)
    req = urllib.request.Request(url, headers={"Referer": SINA_REFERER})
    data = urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("gbk")
    m = re.search(r'=.*?"([^"]*)"', data)
    if not m or not m.group(1):
        raise ValueError(f"{sina_sym} 无数据（代码有误或非交易时段）")
    return m.group(1)


def query(args: dict) -> str:
    sym = _normalize_symbol(str(args["symbol"]))
    fields = _fetch(sym).split(",")
    if len(fields) < 32:
        return f"错误：{sym} 数据不完整"
    name, prev_close, price = fields[0], fields[2], fields[3]
    if price in ("", "0", "0.00"):
        return f"{name}（{sym}）当前停牌或无成交，昨收 {prev_close} 元"
    p = float(price)
    pc = float(prev_close)
    pct = (p - pc) / pc * 100 if pc else 0.0
    vol = int(float(fields[8])) if fields[8] else 0
    # 成交量按手换算（1手=100股），A股习惯口径
    return (f"{name}（{sym}）现价 {p:.3f} 元，"
            f"{'涨' if pct >= 0 else '跌'} {abs(pct):.2f}%（昨收 {pc}），"
            f"成交量 {vol/100:.0f} 手，时间 {fields[30]} {fields[31]}")
