"""QRA 插件：量化研究工具集。

通过 Hermes 官方插件面注册（PluginContext.register_tool），
不修改核心循环（融合架构铁律 A 类嫁接）。
启用方式：config.yaml 里 plugins.enabled 包含 "qra"。
"""

from __future__ import annotations

from .quote import qra_quote

QUOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": (
                "A股股票代码，支持 600519（自动推断交易所）、"
                "sh600519、000001.SZ 等写法。北交所不支持。"
            ),
        }
    },
    "required": ["symbol"],
}

# (name, toolset, schema, handler, emoji, description)
_TOOLS = [
    (
        "qra_quote",
        "qra",
        QUOTE_SCHEMA,
        qra_quote,
        "📈",
        "查询 A 股实时行情：现价/涨跌幅/成交量/成交额（新浪源）。"
        "返回 JSON：含 price、change_pct、volume_shares、amount_yuan 等字段。",
    ),
]


def register(ctx) -> None:
    """插件入口：被 PluginManager 在 plugins.enabled 命中时调用一次。"""
    for name, toolset, schema, handler, emoji, description in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            description=description,
            emoji=emoji,
        )
