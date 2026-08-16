"""QRA Console — prime 式 CoT 全展示终端（ADR D007 · Phase 1 + P0 命令面）。

机理（见 docs/机理研究_prime与CC逆向_2026-08-14.md）：
  - CoT 是消息的一等 content block：默认全展开逐 token 流式渲染，Ctrl+T 折叠，
    折叠态显示 recap（推理中最后一个 **加粗标题**）——prime 原版算法
  - 工具调用/结果实时块（args 摘要 + 结果预览 + 时长）
  - footer 显示成本（反 prime 品牌选择：量化场景成本要可见）
  - 多轮交互：同一 AIAgent 实例 + SessionState.history 续聊

P0 命令面（CC/prime/hermes 原生功能对齐，2026-08-16）：
  - / 命令：help/resume/sessions/clear/compact/export/model/yolo/usage/status/memory/loop
  - ! 直达 shell（vendor bang_shell：同 terminal 工具同门审批，输出不进上下文）
  - ↑↓ 历史 / Tab 补全 / 大块粘贴确认 / 双路由切换（deepseek ↔ opus@8789）
  - /loop 自动继续（CC 对齐，进程内调度器：每轮自动同 prompt 重跑，Ctrl+C 退出）

架构（零 vendor 改动）：
  导入 run_agent.AIAgent（hermes 根模块公开类，oneshot/tui_gateway 同款先例），
  注入 reasoning_callback / stream_delta_callback / 工具回调，显示层 100% 接管。
  核心循环、provider、持久化全部复用 vendor。
  InputLayer 迁至 qra.console.input_layer（本模块 re-export 保测试门禁路径），
  命令注册/分发在 qra.console.commands，处理器在 qra.console.handlers。

回调线程 → 事件队列 → 渲染线程（每次 drain 后一帧 reconcile，prime pi-tui 同款思路）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

# 入口自举：脚本入口是 `python -m src.qra.console.main`（包根在 CWD），
# qra.* 顶层导入需要 src/ 在 sys.path 上。vendor 顶层模块靠 editable 安装
# 解析不受影响；qra 自身没有安装面，此处与 test_inputlayer.py 同约定自补。
# 以 qra.console.main 形式导入时该路径已在 sys.path，guard 是 no-op。
_src_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _src_root not in sys.path:
    sys.path.insert(0, _src_root)

# InputLayer/_char_width 从 input_layer.py 迁出后 re-export：
# test_inputlayer.py 依赖 qra.console.main 此路径（门禁兼容，不破不改）
from qra.console.input_layer import InputLayer, _char_width, detect_paste  # noqa: F401
from qra.console.session_state import PRICE_USD, USD_CNY

# 工具结果预览上限（字符），超出截断并注明
_RESULT_PREVIEW_MAX = 3000


# ---------------------------------------------------------------- 状态与事件

class TurnState:
    """一轮对话的显示状态（仅渲染线程读写）。"""

    def __init__(self, fold_thinking: bool) -> None:
        self.blocks: list[dict] = []      # 时间序块：thinking / text / tool
        self.statuses: list[str] = []     # 最近 3 条生命周期/警告
        self.show_thinking = not fold_thinking  # Ctrl+T 切换
        self.dirty = True


def _thinking_recap(text: str) -> str:
    """prime 的折叠摘要算法：取推理中最后一个 **加粗标题**。"""
    last = None
    idx = 0
    while True:
        idx = text.find("**", idx)
        if idx == -1:
            break
        end = text.find("**", idx + 2)
        if end != -1:
            last = text[idx + 2 : end].strip()
            idx = end + 2
        else:
            break
    return last or "思考中…"


def render(state: TurnState, usage: dict | None, model: str) -> Group:
    parts: list = []
    for blk in state.blocks:
        if blk["kind"] == "thinking":
            if state.show_thinking:
                style = "grey62"
                content: object = Markdown(blk["text"], style=style)
                # Claude Code 式思考提示：标题带计时（"思考 3s"）
                if blk.get("open"):
                    elapsed = time.time() - blk.get("started", time.time())
                    title = f"✻ 思考 {elapsed:.0f}s"
                else:
                    title = "✻ 思考"
                parts.append(
                    Panel(content, border_style=style, box=box.ROUNDED,
                          title=title, title_align="left", padding=(0, 1))
                )
            else:
                parts.append(Text(f"✻ 思考 · {_thinking_recap(blk['text'])}",
                                  style="grey62"))
        elif blk["kind"] == "text":
            parts.append(Markdown(blk["text"]))
        elif blk["kind"] == "tool":
            _render_tool_block(parts, blk)
    if state.statuses:
        for line in state.statuses[-3:]:
            parts.append(Text(f"  · {line}", style="dim yellow"))
    if usage:
        parts.append(_render_usage(usage, model))
    if not parts:
        # 首 token 前的等待帧：发问后立刻有反馈，杜绝"一片空白"
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        f = frames[int(time.time() * 8) % len(frames)]
        parts.append(Text(f"{f} 思考中…", style="dim"))
    return Group(*parts)


def _render_tool_block(parts: list, blk: dict) -> None:
    """Claude Code 式工具块：⏺ 名称 + args 摘要 + 缩进结果预览。
    delegate_task 是 hermes 的 subagent 委派工具——识别为嵌套子代理面板。"""
    name = blk["name"]
    is_subagent = name in ("delegate_task", "spawn_task", "spawn")
    title = "⎇ 子代理" if is_subagent else "⏺ 工具"
    if blk.get("status") == "generating":
        parts.append(Text(f"{title} {name} 参数生成中…", style="cyan"))
        return
    if blk.get("status") == "running":
        inner = Text(f"{name}", style="bold cyan")
        args = _compact_args(blk.get("args"))
        if args:
            inner.append(Text(f"\n  {args}", style="dim"))
        inner.append(Text("\n  ⠋ 执行中…", style="cyan"))
        parts.append(Panel(inner, border_style="cyan", box=box.ROUNDED,
                           title=title, title_align="left", padding=(0, 1)))
        return
    # done
    inner = Text(f"{name}", style="bold cyan")
    inner.append(Text(f" · {blk.get('duration', 0):.2f}s", style="dim"))
    args = _compact_args(blk.get("args"))
    if args:
        inner.append(Text(f"\n  {args}", style="dim"))
    result = blk.get("result") or ""
    if len(result) > _RESULT_PREVIEW_MAX:
        result = result[:_RESULT_PREVIEW_MAX] + (
            f"\n…（已截断，共 {len(blk.get('result') or '')} 字符）")
    if result.strip():
        # 缩进呈现（CC 式嵌套内容），换行保持缩进
        indented = "\n  ".join(result.strip().splitlines())
        inner.append(Text("\n  " + indented, style="grey74"))
    ok = blk.get("ok", True)
    parts.append(Panel(
        inner,
        border_style="green" if ok else "red",
        box=box.ROUNDED,
        title=f"{title} ✓" if ok else f"{title} ✗",
        title_align="left", padding=(0, 1),
    ))


def _compact_args(args) -> str:
    if not args:
        return ""
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    if len(s) > 300:
        s = s[:300] + "…"
    return s


def _render_usage(usage: dict, model: str) -> Text:
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cache = usage.get("cache_read_tokens") or 0
    api = usage.get("api_calls") or 0
    usd = (inp * PRICE_USD["input"] + out * PRICE_USD["output"]
           + cache * PRICE_USD["cache_read"]) / 1e6
    t = Text("▸ ", style="bold")
    t.append(f"{model}", style="bold cyan")
    t.append(f" · in {inp:,} / out {out:,} / cache读 {cache:,} · {api} 次调用", style="dim")
    t.append(f" · ≈¥{usd * USD_CNY:.3f}", style="green")
    return t


# ---------------------------------------------------------------- 事件管道

def _make_callbacks(events: "queue.Queue", state: TurnState) -> dict:
    """构造注入 AIAgent 的回调。回调来自工作线程：只入队，不改状态。"""

    def on_reasoning(text: str) -> None:
        events.put(("reasoning", text))

    def on_delta(text: str | None) -> None:
        events.put(("delta", text))

    def on_tool_gen(name: str) -> None:
        events.put(("tool_gen", name))

    def on_tool_start(tool_call_id, name, args) -> None:
        events.put(("tool_start", tool_call_id, name, args))

    def on_tool_complete(tool_call_id, name, args, result) -> None:
        events.put(("tool_complete", tool_call_id, name, args, result))

    def on_status(kind: str, message: str) -> None:
        events.put(("status", kind, message))

    return {
        "reasoning_callback": on_reasoning,
        "stream_delta_callback": on_delta,
        "tool_gen_callback": on_tool_gen,
        "tool_start_callback": on_tool_start,
        "tool_complete_callback": on_tool_complete,
        "status_callback": on_status,
    }


def apply(state: TurnState, ev) -> None:
    """渲染线程：把事件应用到状态（块时间序，prime message 模型同构）。"""
    kind = ev[0]
    if kind == "reasoning":
        if not state.blocks or state.blocks[-1]["kind"] != "thinking":
            state.blocks.append({"kind": "thinking", "text": "", "open": True,
                                 "started": time.time()})
        state.blocks[-1]["text"] += ev[1]
    elif kind == "delta":
        text = ev[1]
        # None = 回合内流结束信号（conversation_loop 约定）
        if text is None:
            for blk in reversed(state.blocks):
                if blk["kind"] == "thinking":
                    blk["open"] = False
                    break
            return
        if not state.blocks or state.blocks[-1]["kind"] != "text":
            state.blocks.append({"kind": "text", "text": ""})
        state.blocks[-1]["text"] += text
    elif kind == "tool_gen":
        state.blocks.append(
            {"kind": "tool", "name": ev[1], "status": "generating",
             "started": time.time(), "ok": True})
    elif kind == "tool_start":
        _, tool_call_id, name, args = ev
        _close_thinking(state)
        state.blocks.append(
            {"kind": "tool", "id": tool_call_id, "name": name, "args": args,
             "status": "running", "started": time.time(), "ok": True})
    elif kind == "tool_complete":
        _, tool_call_id, name, args, result = ev
        _close_thinking(state)
        for blk in reversed(state.blocks):
            if blk["kind"] == "tool" and blk.get("id") == tool_call_id:
                blk["status"] = "done"
                blk["result"] = str(result) if result else ""
                blk["duration"] = time.time() - blk["started"]
                blk["ok"] = _result_looks_ok(str(result or ""))
                return
        state.blocks.append(
            {"kind": "tool", "id": tool_call_id, "name": name, "args": args,
             "status": "done", "result": str(result or ""),
             "duration": 0.0, "started": time.time(), "ok": True})
    elif kind == "status":
        state.statuses.append(f"[{ev[1]}] {ev[2]}")


def _close_thinking(state: TurnState) -> None:
    for blk in reversed(state.blocks):
        if blk["kind"] == "thinking":
            blk["open"] = False
            break


def _result_looks_ok(result: str) -> bool:
    """工具结果粗判：QRA 工具约定错误以文本形式返回。"""
    if not result:
        return True
    head = result[:200]
    for marker in ("工具调用失败", "未知工具", "参数错误", "Error", "错误"):
        if marker in head:
            return False
    return True


# ---------------------------------------------------------------- 渲染循环

def _render_loop(events: "queue.Queue", state: TurnState, live: Live,
                 result_holder: dict) -> None:
    """消费事件，drain 后一帧 reconcile（prime pi-tui 同款节流）。"""
    # 事件到达时间戳 trace（QRA_EVENT_TRACE=1 时写 /tmp/qra_event_trace.txt，
    # 诊断流式节奏用：判断上游是逐 delta 实时到达还是攒批一次性到达）
    trace = [] if os.getenv("QRA_EVENT_TRACE") else None
    trace_t0 = time.time()
    while True:
        try:
            ev = events.get(timeout=0.25)
        except queue.Empty:
            # 无事件时只刷新等待帧（spinner 动画）；有内容后不空转
            if not state.blocks and not state.statuses:
                live.update(render(state, None, result_holder["model"]))
            continue
        if ev[0] == "sentinel":
            break
        if trace is not None:
            trace.append(f"{time.time() - trace_t0:7.2f}s  {ev[0]}"
                         f"{' ' + str(ev[1])[:60] if len(ev) > 1 else ''}")
        apply(state, ev)
        done = False
        # 排空已到事件，一次渲染
        while True:
            try:
                nxt = events.get_nowait()
            except queue.Empty:
                break
            if nxt[0] == "sentinel":
                done = True
                break
            apply(state, nxt)
        live.update(render(state, None, result_holder["model"]))
        if done or ev[0] == "turn_end":
            break
    if trace is not None:
        try:
            with open("/tmp/qra_event_trace.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(trace) + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------- agent 构造

def _resolve_model_provider(args) -> tuple[str, str | None]:
    """与 oneshot 同款解析：显式 → env → config 默认；只给 model 时自动探测 provider。"""
    from hermes_cli.config import load_config
    from hermes_cli.models import detect_provider_for_model

    cfg = load_config()
    model_cfg = cfg.get("model") or {}
    cfg_model = model_cfg if isinstance(model_cfg, str) else (
        model_cfg.get("default") or model_cfg.get("model") or "")
    cfg_provider = ""
    if isinstance(model_cfg, dict):
        cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
    env_model = os.getenv("HERMES_INFERENCE_MODEL", "").strip()
    explicit = (args.model or "").strip() or env_model
    model = explicit or cfg_model
    if not explicit and "/" in model:
        # config 模型若带厂商前缀（如 deepseek/deepseek-v4-pro）剥成裸名：
        # provider=anthropic 时 agent_init 的正规化只剥匹配前缀，异厂商前缀
        # 会原样送到 API 触发 HTTP 400。与 run_qra.sh 传裸名行为一致。
        # 不 lower()——保原大小写，仅动分隔符结构。
        model = model.split("/", 1)[1]

    provider = (args.provider or "").strip() or None
    if provider is None and explicit:
        try:
            from hermes_cli import model_switch as _ms
            _ms._ensure_direct_aliases()
            direct = _ms.DIRECT_ALIASES.get(explicit.strip().lower())
        except Exception:
            direct = None
        if direct is not None:
            model, provider = direct.model, direct.provider
        else:
            current = (cfg_provider
                       or os.getenv("HERMES_INFERENCE_PROVIDER", "").strip().lower()
                       or "auto")
            detected = detect_provider_for_model(explicit, current)
            if detected:
                provider, model = detected
    if provider is None:
        # 无显式参数：尊重 config 的 provider（QRA 约定=anthropic 端点，
        # 与 run_qra.sh 的 --provider anthropic 一致；否则 resolve_runtime_provider
        # 会按模型前缀把 deepseek/deepseek-v4-pro 路由到原生端点）
        provider = cfg_provider or None
    return model, provider


def build_agent(args):
    """构造 AIAgent（照抄 oneshot 最小参数集 + 自有显示回调）。零 vendor 改动。

    返回 5 元组 (agent, session_db, events, state, sess)。
    YOLO 不走 HERMES_YOLO_MODE env：approval.py import 时冻结为
    _YOLO_MODE_FROZEN（运行期设置无效）——改用 session 级开关
    enable_session_yolo，/yolo 可切、行持久化、resume 可恢复（默认开）。
    """
    os.environ["HERMES_INTERACTIVE"] = "1"   # 终端交互标志（sudo 提示等路径）
    os.environ["HERMES_ACCEPT_HOOKS"] = "1"
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()

    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_state import SessionDB
    from run_agent import AIAgent

    cfg = load_config()
    model, provider = _resolve_model_provider(args)
    runtime = resolve_runtime_provider(
        requested=provider, target_model=model or None, explicit_base_url=None)

    toolsets = None
    if args.toolsets:
        from hermes_cli.oneshot import _normalize_toolsets
        toolsets = _normalize_toolsets(args.toolsets)
    if toolsets is None:
        toolsets = sorted(_get_platform_tools(cfg, "cli"))
        # QRA 插件全系注册在 toolset="qra"（qra_quote/qra_signal/qra_kb_fts/
        # qra_sync/qra_verify/qra_python）。hermes 的 cli 平台默认集只有 19 个
        # 内置集，不含 qra——2026-08-16 qra.run 冒烟实测：插件加载成功但工具
        # 不在会话工具表。并集补上（显式 --toolsets 覆盖时尊重用户意图）。
        toolsets = sorted(set(toolsets) | {"qra"})

    ensure_mcp_discovery_before_agent_build(
        logger=logging.getLogger(__name__), single_query=True)

    session_db = SessionDB()
    events: queue.Queue = queue.Queue()
    state = TurnState(fold_thinking=args.fold_thinking)
    cbs = _make_callbacks(events, state)

    def _clarify(question, choices=None, multi_select=False):
        # console 暂不支持阻塞式追问：让模型自选默认（oneshot 同款语义）
        if choices:
            return ("[console 模式：无交互追问。从以下选项自行选择最合理者继续。] "
                    f"{choices}")
        return "[console 模式：无交互追问。自行做最合理假设并继续。]"

    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        requested_provider=runtime.get("requested_provider"),
        api_mode=runtime.get("api_mode"),
        model=model,
        enabled_toolsets=toolsets,
        quiet_mode=True,
        platform="cli",
        session_db=session_db,
        credential_pool=runtime.get("credential_pool"),
        fallback_model=get_fallback_chain(cfg) or None,
        clarify_callback=_clarify,
        **cbs,
    )
    # oneshot 同款保险：不让 agent 自己的输出污染终端
    agent.suppress_status_output = True

    # ---- QRA console 会话装配（P0 命令面）----
    from qra.console import approvals, models_router
    from qra.console.session_state import SessionState, new_session_id
    from tools.approval import enable_session_yolo

    models_router.capture_primary(agent)
    sid = agent.session_id or new_session_id()
    approvals.sync_session_key(sid)
    # 默认开（用户拍板）；_ensure_db_session 惰性建行时把活动 yolo 写进
    # 创建行 model_config，--resume 可恢复
    enable_session_yolo(sid or "default")
    sess = SessionState(
        session_id=sid,
        model=agent.model or "",
        provider=getattr(agent, "provider", None) or None,
        base_url=getattr(agent, "base_url", "") or "",
        api_mode=getattr(agent, "api_mode", "") or "",
        route_name=models_router.infer_route_name(
            getattr(agent, "base_url", "") or ""),
        yolo=True,
    )
    return agent, session_db, events, state, sess


def cleanup(agent, session_db) -> None:
    """照抄 oneshot finally 的清理顺序；不用 os._exit。"""
    if agent is not None:
        try:
            session_messages = getattr(agent, "_session_messages", None)
            if isinstance(session_messages, list):
                agent.shutdown_memory_provider(session_messages)
            else:
                agent.shutdown_memory_provider()
        except Exception:
            logging.debug("console memory cleanup failed", exc_info=True)
        try:
            agent.close()
        except Exception:
            logging.debug("console agent cleanup failed", exc_info=True)
    if session_db is not None:
        try:
            session_db.close()
        except Exception:
            logging.debug("console session store cleanup failed", exc_info=True)


# ---------------------------------------------------------------- 单轮执行

def run_turn(agent, session_db, events, state, prompt: str,
             conversation_history, console: Console, plain: bool) -> dict:
    """执行一轮：Live 渲染 + 回调采集；plain 模式不渲染只回结果。"""
    logging.disable(logging.CRITICAL)  # 静默 stdlib logger（文件 handler 不受影响）

    result_holder = {"model": getattr(agent, "model", "") or ""}
    if plain:
        result = agent.run_conversation(prompt, conversation_history=conversation_history)
        return result

    import io
    from contextlib import redirect_stderr, redirect_stdout

    buf_out, buf_err = io.StringIO(), io.StringIO()
    # 渲染必须绑定此刻的真实 stdout：run_conversation 期间 redirect_stdout
    # 会把 sys.stdout 换成 StringIO（捕获工具打印防撕裂），而 rich Console
    # 无 file 参数时动态跟随 sys.stdout——渲染帧会被吞进缓冲区，表现为
    # 整轮屏幕空白、结束后内容一次性涌出。显式绑定后 redirect 不影响渲染。
    render_console = Console(file=sys.stdout)
    live = Live(render(state, None, result_holder["model"]), console=render_console,
                refresh_per_second=20, vertical_overflow="visible")
    live.start()
    renderer = threading.Thread(
        target=_render_loop, args=(events, state, live, result_holder),
        daemon=True, name="qra-console-render")
    renderer.start()

    result: dict = {}
    failure = None
    try:
        # 捕获 agent 执行树里的 stdout/stderr（工具打印等），不让其撕裂 Live
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            result = agent.run_conversation(
                prompt, conversation_history=conversation_history)
    except BaseException as exc:  # noqa: BLE001
        failure = exc
    finally:
        # 流结束 → 收尾状态 → 渲染终帧 → 关闭渲染
        events.put(("delta", None))
        events.put(("turn_end", result))
        events.put(("sentinel", None))
        renderer.join(timeout=5)
        if failure is None:
            # 流式未覆盖时兜底：把 final_response 作为文本块显示
            if not any(b["kind"] == "text" and b["text"].strip()
                       for b in state.blocks):
                final = result.get("final_response") or ""
                if final.strip():
                    state.blocks.append({"kind": "text", "text": final})
            # 终帧必须在 live.stop() 之前 update——stop 后 update 不再渲染，
            # 此前 footer 因此从未出现（pty 捕获实证）
            live.update(render(state, result, result_holder["model"]))
        live.stop()

    captured_err = buf_err.getvalue().strip()
    if captured_err:
        console.print(Text("stderr 捕获：", style="bold red") +
                      Text(captured_err[-2000:], style="red"))

    if failure is not None:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        console.print(Text(f"agent 失败：{failure}", style="bold red"))
        return {"final_response": "", "failed": True}

    return result


# ---------------------------------------------------------------- CLI

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="qra console",
        description="QRA 控制台：prime 式 CoT 全展示（D007 Phase 1 + P0 命令面）",
        epilog="交互中 /help 看全部命令（/resume /model /yolo /loop /export …）、"
               " ! 直达 shell、↑↓ 历史、Ctrl+T 折叠思考；空输入或 Ctrl+D 退出。"
               " 输出被管道时自动降级为 plain（只打最终答复）。")
    p.add_argument("-z", "--prompt", help="单发提问（省略则进入多轮交互）")
    p.add_argument("--model", help="模型覆盖（默认 deepseek-v4-pro）")
    p.add_argument("--provider", help="provider 覆盖（默认按模型自动探测）")
    p.add_argument("--toolsets", help="工具集覆盖（逗号分隔；默认 config 的 cli 集）")
    p.add_argument("--fold-thinking", action="store_true",
                   help="启动时思考折叠（Ctrl+T 可展开）")
    p.add_argument("--plain", action="store_true",
                   help="纯文本模式：只打印最终答复（管道/脚本用）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.plain or not sys.stdout.isatty():
        args.plain = True
    console = Console()
    # dsh 精华：fail-loud 配置门卫——坏配置当场 exit 2，不带病运行
    from qra.config_guard import guard_config
    guard_config()

    agent = session_db = None
    try:
        agent, session_db, events, state, sess = build_agent(args)

        def one_turn(prompt: str) -> dict:
            # 自动标题（每会话只试一次；set_auto_title_if_empty 幂等兜底）
            if not sess.title_done and sess.session_id:
                sess.mark_title_set()
                try:
                    session_db.set_auto_title_if_empty(sess.session_id, prompt[:48])
                except Exception:
                    pass
            # 注意：turn_context 会把当前 user 消息追加到 history 副本之后，
            # 所以这里传的 history 必须不含本轮消息（CLI 同款约定）。
            state.blocks.clear()
            state.statuses.clear()
            result = run_turn(agent, session_db, events, state, prompt,
                              sess.history, console, args.plain)
            final = result.get("final_response") or ""
            sess.history.append({"role": "user", "content": prompt})
            sess.history.append({"role": "assistant", "content": final})
            if args.plain:
                if final:
                    console.print(final)
            return result

        def loop_mode(prompt: str) -> None:
            """CC /loop 对齐：自动继续模式（进程内调度器，不依赖 cron）。

            每轮结束 sleep 间隔后自动以同 prompt 重跑；Ctrl+C 在任意时刻
            （回合中/间隔中）打断并退出循环回到提示符。间隔默认 60s，
            QRA_LOOP_INTERVAL 环境变量覆盖（秒，正数）。
            循环中打字由 InputLayer 后台 reader 缓冲，退出循环后照常回显。
            """
            try:
                interval = max(1.0, float(os.environ.get("QRA_LOOP_INTERVAL", "60")))
            except ValueError:
                interval = 60.0
            console.print(Text(
                f"⟳ /loop 模式：每 {interval:.0f}s 自动继续「{prompt[:40]}…」"
                if len(prompt) > 40 else
                f"⟳ /loop 模式：每 {interval:.0f}s 自动继续「{prompt}」",
                style="bold cyan"))
            console.print(Text("  Ctrl+C 退出循环回到提示符", style="dim"))
            n = 0
            while True:
                n += 1
                console.print(Text(f"⟳ 第 {n} 轮", style="bold cyan"))
                try:
                    one_turn(prompt)
                except KeyboardInterrupt:
                    console.print(Text("(中断本轮，退出循环)", style="yellow"))
                    return
                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    console.print(Text("(退出循环)", style="yellow"))
                    return

        if args.prompt:
            try:
                result = one_turn(args.prompt)
            except KeyboardInterrupt:
                return 130
            if result.get("failed") and not (result.get("final_response") or "").strip():
                return 2
            if not (result.get("final_response") or "").strip():
                console.print("console: 未产生最终答复，视为失败。", style="bold red")
                return 1
            return 0

        # 多轮交互：会话级输入层（回合中打字不丢失、不回显进重定向缓冲区）
        console.print(Text("quant-agent · 量化研究智能体", style="bold cyan"))
        console.print(Text("prime 式 CoT 全展示 · /help 看命令 · ! 直达 shell · "
                           "Ctrl+T 折叠思考 · 空行退出", style="dim"))
        from qra.console import commands
        from qra.console.session_state import CommandContext, ConsoleHistory

        inp = InputLayer(state, history=ConsoleHistory(),
                         completer=commands.complete, prompt="❯ ")
        ctx = CommandContext(agent=agent, db=session_db, sess=sess,
                             console=console, inp=inp, events=events,
                             plain=args.plain)
        inp.start()
        try:
            while True:
                console.print(Text("❯ ", style="bold green"), end="")
                inp.redraw()  # 回合中已有的半行草稿补回显到新提示符后
                try:
                    user_input = inp.pop()
                except EOFError:
                    console.print()
                    break
                except KeyboardInterrupt:
                    console.print(Text("^C", style="yellow"))
                    continue
                if not user_input.strip():
                    break
                line = user_input.strip()
                # /resume 待选号模式：纯数字行直接恢复对应会话
                if commands.maybe_pending(ctx, line):
                    console.print()
                    continue
                # 分流：! 直达 → / 命令 → 普通 prompt
                if commands.dispatch(ctx, line) == "prompt":
                    try:
                        one_turn(line)
                    except KeyboardInterrupt:
                        console.print(Text("(中断本轮)", style="yellow"))
                        continue
                # /loop 消费点：命令处理器置位后，主循环进入自动继续模式
                if ctx.loop_prompt:
                    pending_prompt = ctx.loop_prompt
                    ctx.loop_prompt = None
                    loop_mode(pending_prompt)
                console.print()
        finally:
            inp.close()
        return 0
    finally:
        cleanup(agent, session_db)


if __name__ == "__main__":
    sys.exit(main())
