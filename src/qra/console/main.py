"""QRA Console — prime 式 CoT 全展示终端（D007 Phase 1 + P0 命令面 + D011 追加式渲染）。

机理（见 docs/机理研究_prime与CC逆向_2026-08-14.md）：
  - CoT 是消息的一等 content block：默认全展开逐 token 流式渲染（无框灰暗），
    Ctrl+T 折叠，折叠态显示 recap（推理中最后一个 **加粗标题**）——prime 算法
  - 工具调用默认折叠为一行摘要（⏺ 工具 … ▸），鼠标点击或 /fold 展开全文；
    delegate_task 类子代理以 ⎇ 子代理 前缀区分
  - footer 显示成本（反 prime 品牌选择：量化场景成本要可见）
  - 多轮交互：同一 AIAgent 实例 + SessionState.history 续聊

P0 命令面（CC/prime/hermes 原生功能对齐，2026-08-16）：
  - / 命令：help/resume/sessions/clear/compact/export/model/yolo/usage/status/
    memory/loop/fold/agents；输入 / 即弹候选面板（↑↓ 选择、Tab 补全、Esc 关闭）
  - ! 直达 shell（vendor bang_shell：同 terminal 工具同门审批，输出不进上下文）
  - ↑↓ 历史 / Tab 补全 / 大块粘贴确认 / 双路由切换（deepseek ↔ opus@8789）
  - ←→/Home/End 光标编辑 + 鼠标点击定位光标（LineBuffer 行编辑，D011 v2 P0）
  - /loop 自动继续（CC 对齐，进程内调度器：每轮自动同 prompt 重跑，Ctrl+C 退出）

D011 v4 固定输入框帧（2026-08-17，CC 对齐）：
  - DECSTBM 滚动区域：内容在 [1..R] 内自然滚动，输入框钉在屏底 [R+1..H]
    永不滚动。帧内四带自上而下：提示符带（busy 反显 = CC 式输入框）
    → 菜单带（/ 候选面板）→ 活动条带（shell/工具/子代理/思考运行中
    标注 + 计时）→ 面板带（Tab 切入看 shell 输出，←/Esc 返回）
  - 已定型内容只 print 一次（自然滚动 + 消灭 rich Live 全帧重绘的重复输出）
  - 单一写入者 TermIO + 光标追踪 + 整序列原子绘制（tio.locked）——
    「输出时打字终端崩」根因修复；busy 中打字实时回显（CC 对齐）
  - 帧高开合纪律：变高不滚内容（底部行被帧覆盖，记恢复判据）；变矮
    清释放行，offset 未变则按行精确重印被覆盖内容（诚实降级不印错位）

架构（零 vendor 改动）：
  导入 run_agent.AIAgent（hermes 根模块公开类，oneshot/tui_gateway 同款先例），
  注入 reasoning_callback / stream_delta_callback / 工具回调，显示层 100% 接管。
  核心循环、provider、持久化全部复用 vendor。
  InputLayer 迁至 qra.console.input_layer（本模块 re-export 保测试门禁路径），
  命令注册/分发在 qra.console.commands，处理器在 qra.console.handlers，
  渲染在 qra.console.renderer（行账本折叠），终端低层在 qra.console.termio。

回调线程 → 事件队列 → 渲染线程（每次 drain 后即渲染，prime pi-tui 同款思路）。
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time

from rich.console import Console
from rich.text import Text

# 入口自举：脚本入口是 `python -m src.qra.console.main`（包根在 CWD），
# qra.* 顶层导入需要 src/ 在 sys.path 上。vendor 顶层模块靠 editable 安装
# 解析不受影响；qra 自身没有安装面，此处与 test_inputlayer.py 同约定自补。
# 以 qra.console.main 形式导入时该路径已在 sys.path，guard 是 no-op。
_src_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _src_root not in sys.path:
    sys.path.insert(0, _src_root)

# InputLayer/_char_width/detect_paste 从 input_layer.py 迁出后 re-export：
# test_inputlayer.py 依赖 qra.console.main 此路径（门禁兼容，不破不改）
from qra.console.frame import Frame
from qra.console.input_layer import InputLayer, _char_width, detect_paste  # noqa: F401
from qra.console.renderer import TurnRenderer
from qra.console.termio import TermIO


# ---------------------------------------------------------------- 状态与事件

class TurnState:
    """一轮对话的显示状态（渲染线程读写，主线程只读 dirty）。"""

    def __init__(self, fold_thinking: bool) -> None:
        self.blocks: list[dict] = []      # 时间序块：thinking / text（诊断用）
        self.statuses: list[str] = []     # 最近 3 条生命周期/警告
        self.show_thinking = not fold_thinking  # Ctrl+T 切换
        self.dirty = False
        # D011 流式闭合跟踪（apply 内部状态）
        self.thinking_open = False
        self.thinking_started = 0.0
        self.text_open = False
        self.has_text = False             # 本轮是否已流式输出文本
        self.model = ""                   # footer 用


def _log_turn_error(exc: Exception) -> None:
    """回合异常落盘（终端可能已假死/无法复制——日志文件是唯一可靠现场）。

    写 HERMES_HOME/logs/console_errors.log，任何失败都静默（兜底自身不能炸）。
    """
    try:
        import datetime
        import traceback
        hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        log_path = os.path.join(hermes_home, "logs", "console_errors.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass


def _result_looks_ok(result: str) -> bool:
    """工具结果粗判：QRA 工具约定错误以文本形式返回。"""
    if not result:
        return True
    head = result[:200]
    for marker in ("工具调用失败", "未知工具", "参数错误", "Error", "错误"):
        if marker in head:
            return False
    return True


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


def apply(state: TurnState, ev, renderer: TurnRenderer) -> None:
    """渲染线程：把事件应用到状态并增量渲染（追加式，D011）。"""
    kind = ev[0]
    if kind == "reasoning":
        if not state.thinking_open:
            state.thinking_open = True
            state.thinking_started = time.time()
            state.blocks.append({"kind": "thinking", "text": ""})
        state.blocks[-1]["text"] += ev[1]
        renderer.reasoning(ev[1])
    elif kind == "delta":
        text = ev[1]
        # None = 回合内流结束信号（conversation_loop 约定）
        if text is None:
            if state.thinking_open:
                renderer.reasoning_close(time.time() - state.thinking_started)
                state.thinking_open = False
            if state.text_open:
                renderer.text_close()
                state.text_open = False
            return
        state.has_text = True
        if not state.text_open:
            state.text_open = True
            state.blocks.append({"kind": "text", "text": ""})
        state.blocks[-1]["text"] += text
        renderer.text_delta(text)
    elif kind == "tool_gen":
        state.statuses.append(f"[工具] {ev[1]} 参数生成中…")
        renderer.status(f"{ev[1]} 参数生成中…")
    elif kind == "tool_start":
        _, tool_call_id, name, args = ev
        if state.thinking_open:
            renderer.reasoning_close(time.time() - state.thinking_started)
            state.thinking_open = False
        renderer.tool_start(tool_call_id, name, args)
    elif kind == "tool_complete":
        _, tool_call_id, name, args, result = ev
        if state.thinking_open:
            renderer.reasoning_close(time.time() - state.thinking_started)
            state.thinking_open = False
        text_result = str(result) if result else ""
        renderer.tool_complete(tool_call_id, name, args, text_result,
                               _result_looks_ok(text_result))
    elif kind == "status":
        state.statuses.append(f"[{ev[1]}] {ev[2]}")
        renderer.status(f"[{ev[1]}] {ev[2]}")
    elif kind == "turn_end":
        result = ev[1]
        usage = None
        if isinstance(result, dict) and result.get("input_tokens"):
            usage = result
        renderer.finish(usage, state.model)
    elif kind == "click":
        _, cy, cx = ev
        renderer.click(cy, cx)
    elif kind == "toggle_thinking":
        renderer.toggle_thinking()


# ---------------------------------------------------------------- 渲染循环

def _render_loop(events: "queue.Queue", state: TurnState,
                 renderer: TurnRenderer, frame=None) -> None:
    """消费事件并增量渲染；空闲心跳驱动 Frame 活动条/面板（D011 v4）。

    事件到达时间戳 trace（QRA_EVENT_TRACE=1 时写 /tmp/qra_event_trace.txt，
    诊断流式节奏用：判断上游是逐 delta 实时到达还是攒批一次性到达）。
    """
    trace = [] if os.getenv("QRA_EVENT_TRACE") else None
    trace_t0 = time.time()
    while True:
        try:
            ev = events.get(timeout=0.1)
        except queue.Empty:
            if frame is not None:
                frame.tick()
            continue
        if ev[0] == "sentinel":
            break
        # 排空已到事件，逐条增量渲染
        while True:
            if trace is not None:
                trace.append(f"{time.time() - trace_t0:7.2f}s  {ev[0]}"
                             f"{' ' + str(ev[1])[:60] if len(ev) > 1 else ''}")
            if ev[0] == "sentinel":
                break
            try:
                apply(state, ev, renderer)
            except Exception:
                # 渲染异常不杀渲染线程：否则事件无人消费，输入层一直
                # 静音，console 假死（2026-08-16「戳错误后无法运行」）
                logging.exception("console render apply failed")
                try:
                    renderer.emergency_note(f"渲染异常：{ev[0]}")
                except Exception:
                    pass
            try:
                ev = events.get_nowait()
            except queue.Empty:
                break
        if ev[0] == "sentinel":
            break
        try:
            if frame is not None:
                frame.tick()
        except Exception:
            logging.exception("console frame tick failed")
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
             conversation_history, tio: TermIO, renderer: TurnRenderer,
             plain: bool, frame=None) -> dict:
    """执行一轮：追加式渲染 + 回调采集；plain 模式不渲染只回结果。

    frame：交互模式的固定输入框（渲染线程空闲心跳驱动其活动条/面板）；
    -z/plain 传 None 跳过。
    """
    logging.disable(logging.CRITICAL)  # 静默 stdlib logger（文件 handler 不受影响）

    if plain:
        result = agent.run_conversation(prompt, conversation_history=conversation_history)
        return result

    import io
    from contextlib import redirect_stderr, redirect_stdout

    buf_out, buf_err = io.StringIO(), io.StringIO()
    # 渲染经 TermIO 写出：构造时刻已绑定真实 stdout 文件对象，回合内
    # redirect_stdout（捕获工具打印防撕裂）不影响它（D011 单一写入者约定）。
    state.model = getattr(agent, "model", "") or ""
    renderer.begin()
    render_thread = threading.Thread(
        target=_render_loop, args=(events, state, renderer, frame),
        daemon=True, name="qra-console-render")
    render_thread.start()

    result: dict = {}
    failure = None
    try:
        # 捕获 agent 执行树里的 stdout/stderr（工具打印等），渲染不受影响
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            result = agent.run_conversation(
                prompt, conversation_history=conversation_history)
    except BaseException as exc:  # noqa: BLE001
        failure = exc
    finally:
        # 流结束 → 兜底文本（流式未覆盖时）→ 收尾 → 关闭渲染线程
        if failure is None:
            if not state.has_text:
                final = result.get("final_response") or ""
                if final.strip():
                    events.put(("delta", final))
                    events.put(("delta", None))
        events.put(("delta", None))
        events.put(("turn_end", result))
        events.put(("sentinel", None))
        render_thread.join(timeout=5)

    captured_err = buf_err.getvalue().strip()
    if captured_err:
        # 走 append_line 进内容区记账（直接 tio.print 会绕过行账本）
        renderer.append_line("stderr 捕获：" + captured_err[-2000:], style="red")

    if failure is not None:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        renderer.append_line(f"agent 失败：{failure}", style="bold red")
        return {"final_response": "", "failed": True}

    return result


# ---------------------------------------------------------------- CLI

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="qra console",
        description="QRA 控制台：prime 式 CoT 全展示（D007 + P0 命令面 + D011 追加式渲染）",
        epilog="交互中 /help 看全部命令（/resume /model /yolo /loop /fold /agents …）、"
               " ! 直达 shell、输入 / 弹命令面板（Enter 即执行）、↑↓ 历史、←→ 光标编辑、"
               " Ctrl+T 折叠思考、/mouse on 开点击展开；空输入或 Ctrl+D 退出。"
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


class _ContentConsole:
    """命令输出 Console 薄包装：print → renderer.append_line。

    handlers/commands 全部经 ctx.console.print 输出（/help 表格、/status
    Panel 等）；有固定帧后直接写 tty 会绕过渲染账本（光标模型与折叠
    行号漂移），必须进内容区记账（D011 v4）。
    """

    def __init__(self, renderer: TurnRenderer) -> None:
        self._renderer = renderer

    @property
    def width(self) -> int:
        return self._renderer._tio.width

    def print(self, *objs, **kw) -> None:
        for obj in objs:
            self._renderer.append_line(obj, **kw)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.plain or not sys.stdout.isatty():
        args.plain = True
    plain_console = Console()   # plain 模式输出（无样式码，管道友好）
    # 单一写入者：构造时刻绑定真实 stdout——渲染/回显/菜单/提示符全经它
    # 串行写出（「输出时打字终端崩」根因修复，D011）
    tio = TermIO(sys.stdout)
    # dsh 精华：fail-loud 配置门卫——坏配置当场 exit 2，不带病运行
    from qra.config_guard import guard_config
    guard_config()

    agent = session_db = None
    try:
        agent, session_db, events, state, sess = build_agent(args)
        renderer = TurnRenderer(tio, state)
        # 命令输出 console 绑定渲染账本：handlers 的 print 全部进内容区
        # （直接写 tty 会绕过光标模型与折叠行号，D011 v4）
        console = _ContentConsole(renderer)
        # 固定输入框帧：仅多轮交互分支创建接线（-z/plain 为 None）
        frame = None

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
            state.thinking_open = False
            state.text_open = False
            state.has_text = False
            result = run_turn(agent, session_db, events, state, prompt,
                              sess.history, tio, renderer, args.plain, frame)
            final = result.get("final_response") or ""
            sess.history.append({"role": "user", "content": prompt})
            sess.history.append({"role": "assistant", "content": final})
            if args.plain:
                if final:
                    plain_console.print(final)
            return result

        def loop_mode(prompt: str) -> None:
            """CC /loop 对齐：自动继续模式（进程内调度器，不依赖 cron）。

            每轮结束 sleep 间隔后自动以同 prompt 重跑；Ctrl+C 在任意时刻
            （回合中/间隔中）打断并退出循环回到提示符。间隔默认 60s，
            QRA_LOOP_INTERVAL 环境变量覆盖（秒，正数）。
            循环中打字由 InputLayer 后台 reader 缓冲（回显静音），
            退出循环后提示符重画时照常显示。
            """
            try:
                interval = max(1.0, float(os.environ.get("QRA_LOOP_INTERVAL", "60")))
            except ValueError:
                interval = 60.0
            label = prompt[:40] + "…" if len(prompt) > 40 else prompt
            renderer.append_line(
                f"⟳ /loop 模式：每 {interval:.0f}s 自动继续「{label}」",
                style="bold cyan")
            renderer.append_line("Ctrl+C 退出循环回到提示符", style="dim")
            n = 0
            inp.set_busy(True)   # 整个循环：提示符带反显（v4 实时回显）
            try:
                while True:
                    n += 1
                    renderer.append_line(f"⟳ 第 {n} 轮", style="bold cyan")
                    try:
                        one_turn(prompt)
                    except KeyboardInterrupt:
                        renderer.append_line("(中断本轮，退出循环)",
                                             style="yellow")
                        return
                    try:
                        time.sleep(interval)
                    except KeyboardInterrupt:
                        renderer.append_line("(退出循环)", style="yellow")
                        return
            finally:
                inp.set_busy(False)

        if args.prompt:
            try:
                result = one_turn(args.prompt)
            except KeyboardInterrupt:
                return 130
            if result.get("failed") and not (result.get("final_response") or "").strip():
                return 2
            if not (result.get("final_response") or "").strip():
                plain_console.print("console: 未产生最终答复，视为失败。")
                return 1
            return 0

        # 多轮交互：会话级输入层 + 固定输入框帧（D011 v4：输入框钉屏底，
        # 活动条标注 + Tab 面板，CC 对齐）
        frame = Frame(tio, prompt="❯ ")
        frame.offset_provider = renderer.offset
        frame.restore_cb = renderer.reprint_abs
        frame.rows_provider = lambda: renderer._row
        renderer.append_line("quant-agent · 量化研究智能体", style="bold cyan")
        renderer.append_line("prime 式 CoT 全展示 · /help 看命令 · ! 直达 shell · "
                             "Ctrl+T 折叠思考 · /mouse on 开点击展开 · "
                             "Tab 看面板 · ←/Esc 返回 · 空行退出", style="dim")
        from qra.console import commands
        from qra.console.session_state import CommandContext, ConsoleHistory

        inp = InputLayer(state, history=ConsoleHistory(),
                         completer=commands.complete, prompt="❯ ",
                         tio=tio, menu_provider=commands.menu_items,
                         frame=frame)
        inp.set_event_sink(events.put)   # 回合中鼠标点击/Ctrl+T 直达渲染线程
        ctx = CommandContext(agent=agent, db=session_db, sess=sess,
                             console=console, inp=inp, events=events,
                             plain=args.plain, renderer=renderer)

        # 活动条/面板内容源：运行中的 shell 作业优先；无作业回退本轮
        # 活动（state.statuses）与渲染器状态（工具/思考）
        def _activity_now():
            for job in reversed(ctx.shell_jobs):
                if not job.done:
                    return ("shell", job.command[:40], job.started)
            return renderer.activity()

        def _panel_now():
            jobs = ctx.shell_jobs
            if jobs:
                job = next((j for j in reversed(jobs) if not j.done), jobs[-1])
                title = f"! {job.command}"
                if not job.done:
                    title += "（运行中）"
                return title, job.tail(9) or ["（尚无输出…）"]
            return "本轮活动", state.statuses[-9:] or ["（暂无活动输出…）"]

        frame.activity_provider = _activity_now
        frame.panel_provider = _panel_now
        inp.start()
        try:
            while True:
                inp.redraw()   # 提示符 + 草稿 + 光标定位（帧区域同步点）
                if state.dirty:   # 提示符阶段 Ctrl+T：翻折叠 + 区域重印 + 光标复位
                    state.dirty = False
                    renderer.toggle_thinking()
                    inp.redraw()
                try:
                    user_input = inp.pop()
                except EOFError:
                    break
                except KeyboardInterrupt:
                    renderer.append_line("^C", style="yellow")
                    continue
                # 非行项：点击折叠 / ! shell 完成哨兵（D011 v4 帧交互）
                if isinstance(user_input, tuple):
                    kind = user_input[0]
                    if kind == "click":
                        _, cy, cx = user_input
                        renderer.click(cy, cx)
                        inp.redraw()
                    elif kind == "shell_done":
                        _, job = user_input
                        mark = "✓" if job.rc == 0 else f"✗ rc={job.rc}"
                        style = "green" if job.rc == 0 else "red"
                        renderer.append_line(
                            Text(f"⏺ ! {job.command} 完成 · {mark} · "
                                 f"用时 {time.time() - job.started:.0f}s",
                                 style=style))
                    continue
                if not user_input.strip():
                    break
                line = user_input.strip()
                # 输入行回显进内容区（帧原位清草稿后补 transcript，CC 对齐）
                renderer.append_line(Text(f"❯ {line}", style="bold cyan"))
                # /resume 待选号模式：纯数字行直接恢复对应会话
                if commands.maybe_pending(ctx, line):
                    renderer.append_line()
                    continue
                # 分流：! 直达 → / 命令 → 普通 prompt
                if commands.dispatch(ctx, line) == "prompt":
                    inp.set_busy(True)
                    try:
                        one_turn(line)
                    except KeyboardInterrupt:
                        renderer.append_line("(中断本轮)", style="yellow")
                    except Exception as exc:
                        # 任何意外都不能炸死交互循环（终端 raw 模式残留 =
                        # 「戳了错误之后无法运行」）——打印后继续收输入
                        renderer.append_line(f"⚠ 本轮异常：{exc}",
                                             style="bold red")
                        _log_turn_error(exc)
                    finally:
                        inp.set_busy(False)
                # /loop 消费点：命令处理器置位后，主循环进入自动继续模式
                if ctx.loop_prompt:
                    pending_prompt = ctx.loop_prompt
                    ctx.loop_prompt = None
                    loop_mode(pending_prompt)
                renderer.append_line()
        finally:
            inp.close()
        return 0
    finally:
        cleanup(agent, session_db)


if __name__ == "__main__":
    sys.exit(main())
