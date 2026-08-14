"""QRA W1 mini1 · LangGraph 状态定义"""
from typing import TypedDict, Annotated, Optional
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    """Agent 的全局状态"""
    messages: Annotated[list, add_messages]   # 对话历史（LangGraph 标准消息列表）
    tool_choice: Optional[str]                # Agent 选择的工具名
    tool_result: Optional[str]                # 工具返回结果
    final_answer: Optional[str]               # 最终回答
