"""审批桥：会话键同步 + 主线程模态审批 + 跨路由 reasoning 过滤。

vendor 的审批面板绑定 prompt_toolkit UI（cli._approval_callback），console 自有
行编辑无法复用 → 用读线程模态取行实现等价语义。agent 工具线程每 turn 新建
（tool_executor.py），审批回调 TLS 只对主线程 `!` 生效——/yolo off 时 agent
自身的危险命令走 fail-closed：无回调 → 自动拒绝（config approvals.timeout 兜底）。
"""

from __future__ import annotations

import os


def sync_session_key(session_id: str) -> None:
    """把会话键同步到审批与工具可见的全部通道。

    - tools.approval.set_current_session_key：approval 自己的 contextvar
    - gateway.session_context.set_current_session_id：HERMES_SESSION_ID 的
      ContextVar + os.environ 双写（工具经 get_session_env 读回）
    - env HERMES_SESSION_KEY：approval.get_current_session_key 的 env 兜底
      （CLI / 裸线程路径）
    """
    if not session_id:
        return
    try:
        from tools.approval import set_current_session_key
        set_current_session_key(session_id)
    except Exception:
        pass
    try:
        from gateway.session_context import set_current_session_id
        set_current_session_id(session_id)
    except Exception:
        pass
    os.environ["HERMES_SESSION_KEY"] = session_id


def make_modal_approval_callback(inp):
    """构造主线程用的危险命令审批回调（签名对齐 cli._approval_callback）。

    返回 "once"/"session"/"always"/"deny"——terminal_tool._check_all_guards 的
    审批协议。模态问答经 InputLayer.ask_modal 委托给读线程，不与读线程抢 stdin。
    """

    def _cb(command, description, *, allow_permanent=True, smart_denied=False):
        head = (command or "").strip().replace("\n", " ")
        if len(head) > 160:
            head = head[:160] + "…"
        prompt = f"⚠ 危险命令批准？ {head}"
        if description:
            prompt += f"（{description}）"
        prompt += " [y=一次/s=会话级/a=永久/n=拒绝] "
        try:
            ans = inp.ask_modal(prompt).strip().lower()
        except Exception:
            return "deny"
        if ans in ("a", "always"):
            return "always"
        if ans in ("s", "session"):
            return "session"
        if ans in ("y", "yes"):
            return "once"
        return "deny"

    return _cb


def strip_reasoning(messages: list) -> list:
    """跨路由重放前剥掉 thinking 内容块。

    deepseek 的 reasoning_content 以 thinking 块形式存在于历史里，喂给
    opus（CC proxy 8789，Anthropic Messages 协议）会 400。切换/恢复到
    opus 路由时对重放历史统一过滤。
    """
    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        kept = [b for b in content
                if not isinstance(b, dict)
                or b.get("type") not in ("thinking", "reasoning")]
        m2 = dict(m)
        m2["content"] = kept
        out.append(m2)
    return out
