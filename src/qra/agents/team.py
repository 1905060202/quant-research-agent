"""AutoGen W3-4 · 量化研究小组（多智能体对话编排）

对应论文 2308.08155 的三个锚点：
1. 角色分工：行情分析师(工具) / 文献员(工具) / 研究员(汇总)——GroupChat 轮流发言
2. 对比实验：同一任务 LangGraph 单 agent 图式 vs AutoGen 小组对话式（见 run_compare.py）
3. 人类检查点：研究员产出结论后，确定性"人"函数检查（长度/工具错误字样）
   通过才落盘 reports/team_daily.md——对应论文 human proxy 的 approval gating

与 LangGraph 版的本质区别（面试弹药）：
- LangGraph：显式边，我知道每一步去哪；AutoGen：会话流，agent 看上下文决定下一步
- LangGraph 的 agent_node 里 max_rounds 强制汇总是手写的终止条件；
  AutoGen 的 max_turns + TERMINATE 是框架能力
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.tools import FunctionTool
from autogen_ext.models.openai import OpenAIChatCompletionClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm import _load_config  # noqa: E402
from tools import market, kb  # noqa: E402

TERMINATE = "TERMINATE"
REPORT_PATH = Path(__file__).resolve().parents[3] / "reports" / "team_daily.md"


def _market_tool(symbol: str) -> str:
    """AutoGen FunctionTool 薄包装：框架按函数签名拆 kwargs，
    registry 的实现是单 dict 签名，直接挂会参数格式错误（实测踩坑）"""
    return market.query({"symbol": symbol})


def _kb_tool(query: str) -> str:
    return kb.search({"query": query})


def _make_client() -> OpenAIChatCompletionClient:
    base, key, model = _load_config()
    # llm.py 存的是 Anthropic 兼容端点（…/anthropic）；OpenAI SDK 要原生端点：
    # 剥掉 /anthropic 后缀再拼 /v1，否则请求打到 anthropic/v1/chat/completions 得 404
    openai_base = re.sub(r"/anthropic$", "", base.rstrip("/"))
    # 小组多轮对话用非思考模式 deepseek-chat：thinking 模型（如 v4-flash）在
    # OpenAI 协议下会把推理流混进 content，自我独白被当结论落盘（实测踩坑）
    team_model = "deepseek-chat" if model not in ("deepseek-chat", "deepseek-reasoner") else model
    return OpenAIChatCompletionClient(
        model=team_model,
        api_key=key,
        base_url=openai_base + "/v1",
        max_tokens=1000,
        # 非 OpenAI 官方模型名必须显式 model_info，否则 get_info 抛错
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": False,
            "family": "unknown",
        },
    )


def build_team():
    """组装三人小组：分析师(行情工具) + 文献员(KB工具) + 研究员(汇总)"""
    client = _make_client()

    analyst = AssistantAgent(
        "analyst",
        model_client=client,
        tools=[
            FunctionTool(
                _market_tool,
                name="market_query",
                description="查 A 股实时行情（现价/涨跌幅/成交量）。参数 symbol：代码如 600519、sz159558",
            )
        ],
        system_message=(
            "你是行情分析师。用户问行情/指数时用 market_query 工具拿真实数据，"
            "把结果转述清楚（含名称、现价、涨跌幅、数据时间）。不要编造价格。"
            "查完就简明回答，不要替别人写结论。"
        ),
    )

    librarian = AssistantAgent(
        "librarian",
        model_client=client,
        tools=[
            FunctionTool(
                _kb_tool,
                name="kb_search",
                description="在量化研究方法论文档库中检索。参数 query：自然语言问题",
            )
        ],
        system_message=(
            "你是方法论文献员。用户问方法论/框架/复盘类问题时用 kb_search 工具检索"
            "文档库，引用检索到的内容（注明来自哪篇文档）。没有检索到就直说没有。"
            "不要编造文档。"
        ),
    )

    researcher = AssistantAgent(
        "researcher",
        model_client=client,
        system_message=(
            "你是研究员。综合 analyst 和 librarian 的产出，给用户一份简洁结论"
            "（要点式，标注哪些数据来自行情、哪些来自文档）。"
            "结论写完后单独一行输出 TERMINATE。"
        ),
    )

    team = RoundRobinGroupChat(
        [analyst, librarian, researcher],
        termination_condition=(
            MaxMessageTermination(max_messages=12) | TextMentionTermination(TERMINATE)
        ),
    )
    return team


def human_checkpoint(conclusion: str) -> tuple[bool, str]:
    """人类检查点（论文 human proxy 的 approval gating 简化版）。

    真人场景应接 CLI 交互等用户点头；这里用确定性检查模拟门槛：
    结论不能太短（敷衍）、不能含工具错误字样（带病结论不落盘）。
    """
    if len(conclusion) < 50:
        return False, "结论过短，疑似敷衍"
    if re.search(r"未知工具|缺少参数|执行失败|LLM错误", conclusion):
        return False, "结论携带工具错误信息"
    return True, "ok"


def _extract_conclusion(messages) -> str:
    """从会话流里取研究员的消息：优先含 TERMINATE 的那条（去掉 TERMINATE 行），
    否则取最后一条。取'最后'而非'最长'——thinking 模型的自我独白往往是最长的。"""
    text = ""
    for m in messages:
        if getattr(m, "source", "") == "researcher":
            t = getattr(m, "content", "")
            if isinstance(t, str):
                text = t
    if not text:
        return ""
    if TERMINATE in text:
        # TERMINATE 之前的正文是结论；若结论被终止条件截断，取 TERMINATE 所在行前
        text = text.split(TERMINATE)[0]
    return text.strip()


async def run(task: str) -> str:
    team = build_team()
    result = await team.run(task=task)
    conclusion = _extract_conclusion(result.messages)
    if not conclusion:
        # 研究员没说话（可能被终止条件截断）：退回最后一条消息
        for m in reversed(result.messages):
            t = getattr(m, "content", "")
            if isinstance(t, str) and t.strip():
                conclusion = t.strip()
                break
    passed, reason = human_checkpoint(conclusion)
    if passed:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            f"# 量化研究小组日报（{datetime.now():%Y-%m-%d %H:%M}）\n\n"
            f"## 任务\n\n{task}\n\n## 结论\n\n{conclusion}\n",
            encoding="utf-8",
        )
        return f"✅ 已落盘 {REPORT_PATH.name}\n\n{conclusion}"
    return f"⛔ 检查点拒绝（{reason}），不落盘。原始结论：\n\n{conclusion}"
