"""QRA Console — prime 式 CoT 全展示终端（ADR D007 · Phase 1）。

机理（见 docs/机理研究_prime与CC逆向_2026-08-14.md）：
  - CoT 是消息的一等 content block：默认全展开逐 token 流式渲染，Ctrl+T 折叠，
    折叠态显示 recap（推理中最后一个 **加粗标题**）——prime 原版算法
  - 工具调用/结果实时块（args 摘要 + 结果预览 + 时长）
  - footer 显示成本（反 prime 品牌选择：量化场景成本要可见）
  - 多轮交互：同一 AIAgent 实例 + conversation_history 续聊

架构（零 vendor 改动）：
  导入 run_agent.AIAgent（hermes 根模块公开类，oneshot/tui_gateway 同款先例），
  注入 reasoning_callback / stream_delta_callback / 工具回调，显示层 100% 接管。
  核心循环、provider、持久化全部复用 vendor。

回调线程 → 事件队列 → 渲染线程（每次 drain 后一帧 reconcile，prime pi-tui 同款思路）。
"""

from __future__ import annotations

import argparse
import codecs
import json
import logging
import os
import queue
import select
import signal
import sys
import termios
import threading
import time
import unicodedata

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

# DeepSeek 公开价（USD / 1M tokens），¥ 按 7.2 折算
_PRICE_USD = {"input": 0.27, "output": 1.10, "cache_read": 0.027}
_USD_CNY = 7.2

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
                title = "💭 思考" + (" …" if blk["open"] else "")
                parts.append(
                    Panel(content, border_style=style, box=box.ROUNDED,
                          title=title, title_align="left", padding=(0, 1))
                )
            else:
                parts.append(Text(f"💭 {_thinking_recap(blk['text'])}",
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
    return Group(*parts)


def _render_tool_block(parts: list, blk: dict) -> None:
    if blk.get("status") == "generating":
        parts.append(Panel(
            Text(f"{blk['name']} 参数生成中…", style="cyan"),
            border_style="cyan", box=box.ROUNDED, title="⚙ 工具",
            title_align="left", padding=(0, 1),
        ))
        return
    if blk.get("status") == "running":
        inner = Text(f"{blk['name']}\n", style="bold cyan")
        inner.append(Text(_compact_args(blk.get("args")), style="dim"))
        inner.append(Text("\n执行中…", style="cyan"))
        parts.append(Panel(inner, border_style="cyan", box=box.ROUNDED,
                           title="⚙ 工具", title_align="left", padding=(0, 1)))
        return
    # done
    inner = Text(f"{blk['name']}", style="bold cyan")
    inner.append(Text(f" · {blk.get('duration', 0):.2f}s", style="dim"))
    inner.append(Text("\n", style="dim"))
    inner.append(Text(_compact_args(blk.get("args")), style="dim"))
    result = blk.get("result") or ""
    if len(result) > _RESULT_PREVIEW_MAX:
        result = result[:_RESULT_PREVIEW_MAX] + (
            f"\n…（已截断，共 {len(blk.get('result') or '')} 字符）")
    if result.strip():
        inner.append(Text("\n" + result.strip(), style="grey74"))
    ok = blk.get("ok", True)
    parts.append(Panel(
        inner,
        border_style="green" if ok else "red",
        box=box.ROUNDED,
        title="⚙ 工具 ✓" if ok else "⚙ 工具 ✗",
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
    usd = (inp * _PRICE_USD["input"] + out * _PRICE_USD["output"]
           + cache * _PRICE_USD["cache_read"]) / 1e6
    t = Text("▸ ", style="bold")
    t.append(f"{model}", style="bold cyan")
    t.append(f" · in {inp:,} / out {out:,} / cache读 {cache:,} · {api} 次调用", style="dim")
    t.append(f" · ≈¥{usd * _USD_CNY:.3f}", style="green")
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
            state.blocks.append({"kind": "thinking", "text": "", "open": True})
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


# ---------------------------------------------------------------- 输入层（会话级）
# prime 机理落地：单一持久读线程 + 自有行编辑，不依赖 input()/readline。
# 此前"每回合起停 key thread"的架构有交还竞态——key thread 读走字节后主循环
# 已越过缓冲检查，input() 永远等不到（pty 实证挂死）。会话级输入层没有
# raw/cooked 转换窗口：回合中敲入直接进行缓冲，回合后主循环从队列取行。

def _char_width(c: str) -> int:
    """终端显示列宽（CJK 全角占 2 列），退格回显用。"""
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


class InputLayer:
    """交互会话持有一个实例：cbreak-noecho + 读线程 + 行队列。

    行为：
    - Ctrl+T 折叠/展开思考（写 TurnState.dirty 触发渲染重画）
    - Ctrl+C 还原 SIGINT（cbreak 下 ISIG 已关）；Ctrl+Z → SIGTSTP
    - 回车提交整行进队列；退格删一个字符（按显示列宽回显）；^D 空行 = EOF
    - 回显直达 /dev/tty——回合中 stdout 被重定向进 StringIO，绕开它才可见
    """

    EOF = object()

    def __init__(self, state: TurnState) -> None:
        self._q: "queue.Queue" = queue.Queue()
        self._state = state
        self._fd = sys.stdin.fileno()
        self._tty_out = None
        try:
            self._tty_out = os.open("/dev/tty", os.O_WRONLY)
        except OSError:
            self._tty_out = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="qra-console-input")
        # 行编辑状态：raw 字节（提交用）+ 字符列表（退格按字符删）
        self._raw = bytearray()
        self._chars: list[tuple[str, int]] = []   # (字符, 字节数)
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def start(self) -> None:
        self._thread.start()

    def pop(self) -> str:
        item = self._q.get()
        if item is self.EOF:
            raise EOFError
        return item

    def draft(self) -> str:
        return "".join(c for c, _ in self._chars)

    def redraw(self) -> None:
        """回合结束后把已有草稿补回显到新提示符后（清理回合中乱插的回显）。"""
        if self._chars:
            self._echo(self.draft().encode("utf-8", "replace"))

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    # ------------------------------------------------------------ 内部

    def _echo(self, data: bytes) -> None:
        if self._tty_out is not None:
            try:
                os.write(self._tty_out, data)
            except OSError:
                pass

    def _submit(self) -> None:
        self._echo(b"\r\n")
        self._q.put(self._raw.decode("utf-8", "replace"))
        self._raw.clear()
        self._chars.clear()

    def _push_char(self, ch: str) -> None:
        n = len(ch.encode("utf-8"))
        self._chars.append((ch, n))
        self._raw.extend(ch.encode("utf-8"))

    def _backspace(self) -> None:
        if not self._chars:
            return
        ch, n = self._chars.pop()
        w = _char_width(ch)
        self._echo(b"\b" * w + b" " * w + b"\b" * w)
        del self._raw[-n:]

    def _run(self) -> None:
        # cbreak-noecho：只清 lflag 的 ICANON/ECHO/ISIG/IEXTEN，
        # 保留 OPOST（输出 \n→\r\n 转译照旧）与 iflag 其余位。
        # stdin 非 tty（单测/降级）时跳过 termios，裸 select+read 照常工作。
        try:
            old = termios.tcgetattr(self._fd)
        except termios.error:
            old = None
        if old is not None:
            tio = old
            tio[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            tio[6][termios.VMIN] = 0
            tio[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSADRAIN, tio)
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not ready:
                    continue
                try:
                    chunk = os.read(self._fd, 64)
                except OSError:
                    continue
                if not chunk:  # EOF
                    self._q.put(self.EOF)
                    break
                for b in chunk:
                    if self._handle(bytes([b])):
                        return  # EOF 路径：直接收线程（finally 还原 termios）
        finally:
            if old is not None:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, old)

    def _handle(self, b: bytes) -> bool:
        """处理单字节；返回 True 表示终止线程（EOF 路径）。"""
        if b == b"\x14":  # Ctrl+T 折叠
            self._state.show_thinking = not self._state.show_thinking
            self._state.dirty = True
        elif b == b"\x03":  # Ctrl+C → SIGINT（ISIG 已关，手动还原）
            os.kill(os.getpid(), signal.SIGINT)
        elif b == b"\x1a":  # Ctrl+Z → SIGTSTP
            os.kill(os.getpid(), signal.SIGTSTP)
        elif b in (b"\r", b"\n"):
            self._submit()
        elif b in (b"\x7f", b"\x08"):  # 退格
            self._backspace()
        elif b == b"\x04" and not self._chars:  # ^D 空行 = EOF
            self._q.put(self.EOF)
            return True
        elif b >= b" " or b == b"\t":  # 可见字符（含 Tab）
            for ch in self._dec.decode(b):
                self._push_char(ch)
                self._echo(ch.encode("utf-8"))
        return False


# ---------------------------------------------------------------- 渲染循环

def _render_loop(events: "queue.Queue", state: TurnState, live: Live,
                 result_holder: dict) -> None:
    """消费事件，drain 后一帧 reconcile（prime pi-tui 同款节流）。"""
    while True:
        ev = events.get()
        if ev[0] == "sentinel":
            break
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
    """构造 AIAgent（照抄 oneshot 最小参数集 + 自有显示回调）。零 vendor 改动。"""
    # 控制台单线程驻留：审批自动放行（QRA 工具为只读/验证类，风险低）。
    # 见 oneshot 同款注释。
    os.environ["HERMES_YOLO_MODE"] = "1"
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
    return agent, session_db, events, state


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
    live = Live(render(state, None, result_holder["model"]), console=console,
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
        description="QRA 控制台：prime 式 CoT 全展示（D007 Phase 1）",
        epilog="交互中 Ctrl+T 折叠/展开思考；空输入或 Ctrl+D 退出。"
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

    agent = session_db = None
    try:
        agent, session_db, events, state = build_agent(args)
        conversation_history: list = []

        def one_turn(prompt: str) -> dict:
            nonlocal conversation_history
            # 注意：turn_context 会把当前 user 消息追加到 history 副本之后，
            # 所以这里传的 history 必须不含本轮消息（CLI 同款约定）。
            state.blocks.clear()
            state.statuses.clear()
            result = run_turn(agent, session_db, events, state, prompt,
                              conversation_history, console, args.plain)
            final = result.get("final_response") or ""
            conversation_history.append({"role": "user", "content": prompt})
            conversation_history.append({"role": "assistant", "content": final})
            if args.plain:
                if final:
                    console.print(final)
            return result

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
        console.print(Text("QRA 控制台 · prime 式 CoT 全展示 · Ctrl+T 折叠思考 · "
                           "空输入退出", style="bold cyan"))
        inp = InputLayer(state)
        inp.start()
        try:
            while True:
                console.print(Text("你 › ", style="bold green"), end="")
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
                try:
                    one_turn(user_input.strip())
                except KeyboardInterrupt:
                    console.print(Text("(中断本轮)", style="yellow"))
                    continue
                console.print()
        finally:
            inp.close()
        return 0
    finally:
        cleanup(agent, session_db)


if __name__ == "__main__":
    sys.exit(main())
