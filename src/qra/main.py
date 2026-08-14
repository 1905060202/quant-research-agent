"""QRA W2 · LLM Agent + 记忆（借鉴 prime-agent 的 P1/P2/P4/P5）

P4: 工具契约（LLM function calling）——已接入
P2: 分层记忆——memory.py 长期记忆 + messages 短期
P5: 精炼回路——Reflector 节点把结论写回记忆
"""
import sys
from langgraph.graph import StateGraph, START, END
from state import AgentState
from agent import agent_node
from memory import upsert, get, list_kind, record_refinement, plan_refinement
from memory_compat import remember, recall

def memory_node(state):
    """P5/ D4：精炼回路——把结论写回四层记忆 + 记录交互"""
    fa = state.get("final_answer", "")
    if fa and len(fa) > 20:
        upsert("memory", f"会话结论: {fa[:50]}", fa[:300])
    return {"final_answer": fa}

graph = StateGraph(AgentState)
graph.add_node("agent", agent_node)
graph.add_node("memory", memory_node)
graph.add_edge(START, "agent")
graph.add_edge("agent", "memory")
graph.add_edge("memory", END)
app = graph.compile()

def chat(text: str, show_tools: bool = True):
    result = app.invoke({"messages": [{"role": "user", "content": text}]})
    print("🧑", text)
    print("🤖", result["final_answer"][:400])
    if show_tools and result.get("tool_log"):
        for log in result["tool_log"]:
            print("   🔧", log[:120])
    print()

if __name__ == "__main__":
    print("=== QRA W2 · LLM Agent（function calling）===\n")
    chat("查一下 sz159558 的现价")
    chat("怎么做多假设竞争？")
    chat("顺便总结下：我现在想开始做量化日报自动化")
