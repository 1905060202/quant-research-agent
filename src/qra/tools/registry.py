"""工具注册表：契约注册 + LLM 提示词生成 + 统一调用入口。

每个工具一个契约（name/description/params/impl），LLM 按描述自主选择——
这是 prime-agent 的 D2 移植，也是 W2 的 P4。
"""
import json

_TOOLS: dict[str, dict] = {}


def register(name: str, description: str, params: dict, impl):
    """注册一个工具。

    params: {"参数名": "参数说明"} —— LLM 生成 JSON 时的字段
    impl: callable(args_dict) -> str
    """
    _TOOLS[name] = {
        "name": name,
        "description": description,
        "params": params,
        "impl": impl,
    }


def tool_specs_for_prompt() -> str:
    """把注册表渲染成给 LLM 的工具清单文本"""
    lines = []
    for t in _TOOLS.values():
        params = "、".join(f"{k}（{v}）" for k, v in t["params"].items()) or "无"
        lines.append(f"- {t['name']}: {t['description']} 参数: {params}")
    return "\n".join(lines)


def call_tool(name: str, args: dict) -> str:
    """统一调用入口：未知工具/缺参数给可读错误（不抛异常，回给 LLM 让它自纠）"""
    if name not in _TOOLS:
        known = "、".join(_TOOLS)
        return f"错误：未知工具 '{name}'。可用工具：{known}"
    tool = _TOOLS[name]
    for k in tool["params"]:
        if not args.get(k):
            return f"错误：调用 {name} 缺少参数 '{k}'。请补全后重试。"
    try:
        return tool["impl"](args)
    except Exception as e:  # 工具自身异常也要回给 LLM，不打断会话
        return f"错误：{name} 执行失败：{e}"


# ---- 工具注册（import 即生效）----
from tools import market, kb  # noqa: E402

register(
    "market_query",
    "查 A 股实时行情（现价/涨跌幅/成交量）。单个标的，多个标的请分次调用。",
    {"symbol": "股票代码或指数代码，如 600519、sz159558、000001"},
    market.query,
)

register(
    "kb_search",
    "在量化研究方法论文档库中检索（向量语义检索）。适合'怎么做X'类方法论问题。",
    {"query": "检索问题，用自然语言描述，如'多假设竞争怎么做'"},
    kb.search,
)
