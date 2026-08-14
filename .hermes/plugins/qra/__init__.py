"""QRA 插件：量化研究工具集。

通过 Hermes 官方插件面注册（PluginContext.register_tool），
不修改核心循环（融合架构铁律 A 类嫁接）。
启用方式：config.yaml 里 plugins.enabled 包含 "qra"。

⚠️ handler 契约（与 bundled spotify/google_meet 一致）：
handler 第一个位置参数是模型填的参数 dict，框架注入的
task_id/session_id/user_task 等额外 kwarg 用 **_kw 吸收。
"""

from __future__ import annotations

from .kb import qra_kb_fts
from .quote import qra_quote
from .signal import qra_signal

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

SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "top_n": {
            "type": "integer",
            "description": "HOT/COLD 榜各取前 N 名，默认 10，最大 20",
        },
        "code": {
            "type": "string",
            "description": "可选：查单只股票在预测池的排名，如 600710",
        },
    },
}

KB_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "检索词（≥3 字符走 trigram 精确匹配，短词走子串兜底）",
        },
        "limit": {
            "type": "integer",
            "description": "返回条数，默认 5，最大 10",
        },
    },
    "required": ["query"],
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
    (
        "qra_signal",
        "qra",
        SIGNAL_SCHEMA,
        qra_signal,
        "📡",
        "读取猎豹 v2.1 最新信号快照：市场温度/情绪/regime、HOT/COLD 板块榜、"
        "模型 IC 与新鲜度（诚实标记 is_stale）、个股预测排名。"
        "可选参数 top_n（默认 10）与 code（个股查询）。",
    ),
    (
        "qra_kb_fts",
        "qra",
        KB_SCHEMA,
        qra_kb_fts,
        "📚",
        "知识库全文检索（FTS5 trigram，中英文通吃）：给定检索词返回"
        "最相关文档片段（含出处 doc_name 与命中片段）。默认查 data/kb_fts.db。",
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
