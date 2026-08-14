"""QRA W2 · LLM Agent 节点（function calling 模式）

流程：LLM 读用户问题 + 工具清单 → 决定调用工具(JSON) → 执行 → 结果回填 LLM → 生成最终回答
借鉴 prime-agent：工具按契约注册，LLM 按描述调用（不是规则匹配）
"""
import json, re
from llm import chat
from tools.registry import tool_specs_for_prompt, call_tool

SYSTEM = f"""你是一个量化研究助手。你有以下工具可用：
{tool_specs_for_prompt()}

规则：
1. 当需要信息时，先调用工具，再基于工具结果回答。
2. 工具调用格式（严格 JSON 单行输出）：{{"tool": "工具名", "args": {{"参数": "值"}}}}
3. 不需要工具时，直接回答用户。
4. 回答要简洁、口语化，像跟朋友说话。"""

def _extract_tool_call(text: str):
    """从 LLM 输出中提取工具调用 JSON（兼容混合文本 + OpenAI/自定义风格）

    策略：①找含工具名的 JSON 片段 ②找首尾大括号 json 解析
    """
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    # 已知工具名集合
    KNOWN = {"market_query", "kb_search", "行情查询", "知识库检索"}
    # 先找含工具名的 {...} 片段（支持嵌套）
    for m in re.finditer(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}', text):
        seg = m.group(0)
        if any(k in seg for k in KNOWN):
            try:
                data = json.loads(seg)
                return _parse_call(data)
            except Exception:
                continue
    # 兜底：开头就是 { 的完整 JSON
    stripped = text.lstrip()
    if stripped.startswith('{'):
        try:
            return _parse_call(json.loads(stripped))
        except Exception:
            pass
    return None

def _parse_call(data: dict):
    """统一解析 OpenAI 风格与自定义风格"""
    if "name" in data:
        args = data.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                args = {}
        return data["name"], args
    if "tool" in data:
        return data.get("tool"), data.get("args", {})
    return None

def _extract_tool_calls(text: str) -> list:
    """从 LLM 输出中提取全部工具调用（支持并行多调用）"""
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    KNOWN = {"market_query", "kb_search", "行情查询", "知识库检索"}
    calls = []
    # 找所有含工具名的 JSON 对象
    for m in re.finditer(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}', text):
        seg = m.group(0)
        if any(k in seg for k in KNOWN):
            try:
                data = json.loads(seg)
                call = _parse_call(data)
                if call:
                    calls.append(call)
            except Exception:
                continue
    return calls

def _chat_with_retry(system: str, prompt: str, max_tokens: int = 600, retries: int = 2):
    """LLM 调用 + 空响应重试（T05 稳定性修复：DeepSeek 偶发返回空）"""
    for attempt in range(retries + 1):
        resp = chat(system, prompt, max_tokens=max_tokens)
        if resp and resp.strip():
            return resp
        print(f"  ⚠️ LLM 空响应，重试 {attempt+1}/{retries}")
    return ""  # 全部失败

def agent_node(state, max_rounds: int = 3):
    """LLM 编排节点：可多轮调用工具（最多 max_rounds 轮）

    P1 增强：
    - 空响应自动重试（2 次）
    - 多工具并行调用
    - 最终回答为空时给兜底提示
    """
    msgs = state["messages"]
    user_q = msgs[-1].content if msgs else ""
    def _msg_str(m):
        return f"{getattr(m, 'type', 'msg')}: {getattr(m, 'content', str(m))}"
    history = "\n".join(_msg_str(m) for m in msgs[-6:])

    current_q = user_q
    tool_log = []
    last_resp = ""
    for _ in range(max_rounds):
        prompt = f"对话历史：\n{history}\n\n当前用户问题：{current_q}"
        if tool_log:
            prompt += f"\n\n已完成的工具调用结果（如果还有没查的标的，继续调用工具；查完了就汇总回答）：\n{'\n'.join(tool_log)}"
        resp = _chat_with_retry(SYSTEM, prompt, max_tokens=600)
        last_resp = resp
        calls = _extract_tool_calls(resp)   # 支持一次多个工具调用
        if calls:
            for tool_name, args in calls:
                result = call_tool(tool_name, args)
                tool_log.append(f"[{tool_name} {json.dumps(args, ensure_ascii=False)}] → {result}")
            current_q = user_q
            continue
        # 无工具调用 → 这就是最终回答
        if resp.strip():
            return {"final_answer": resp.strip(), "tool_log": tool_log}
        # 空响应且无工具 → 兜底
        return {"final_answer": "抱歉，我暂时没拿到结果。请再问一次，或者换个说法。", "tool_log": tool_log}

    # 达到轮数上限：强制汇总（不带工具选项，只基于已得结果回答）
    if tool_log:
        final_prompt = (f"对话历史：\n{history}\n\n原始问题：{user_q}\n\n"
                        f"工具结果汇总（请基于这些结果直接回答用户，不要输出JSON）：\n{'\n'.join(tool_log)}")
        final_resp = _chat_with_retry(SYSTEM, final_prompt, max_tokens=600)
        if final_resp.strip():
            return {"final_answer": final_resp.strip(), "tool_log": tool_log}
    # 最后兜底
    if last_resp and last_resp.strip() and not last_resp.strip().startswith("{"):
        return {"final_answer": last_resp.strip(), "tool_log": tool_log}
    return {"final_answer": "抱歉，处理超时。请简化问题再试。", "tool_log": tool_log}
