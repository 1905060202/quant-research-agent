"""斜杠命令注册表 + 三分流 + Tab 补全 + 分发。

console 本地实现（不 import vendor CLI——审批面板绑定 prompt_toolkit，
console 自有行编辑不能复用它）。分流顺序：
    bang（! 直达，hermes_cli.bang_shell 复用）→ slash 命令 → 普通 prompt。
/resume 无参后进入"待选号模式"：直接敲数字等价 /resume <N>。
循环导入设计：commands 只在模块底部 _register_p0() 才 import handlers
（handlers 从不反向 import commands）；/help 实现留在本模块。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class CommandDef:
    name: str
    usage: str
    category: str
    help: str
    handler: Callable = field(compare=False)
    aliases: tuple = ()


_COMMANDS: dict[str, CommandDef] = {}


def register(defn: CommandDef) -> None:
    _COMMANDS[defn.name] = defn
    for alias in defn.aliases:
        _COMMANDS[alias] = defn


def all_commands() -> dict[str, CommandDef]:
    return _COMMANDS


def _looks_like_slash(text: str) -> bool:
    """本地复刻 cli.py:4120 路径防护：行首 / 且首词内不再含 /。

    "/tmp/x" 是文件路径不是命令；"/resume foo" 才是命令。
    """
    if not text.startswith("/"):
        return False
    first = text.split(None, 1)[0] if text.split() else text
    return "/" not in first[1:]


def parse_input(text: str):
    """三分流：("bang", cmd) / ("command", name, args) / ("prompt", text)。

    name 是剥掉前导 / 的裸名（注册表键即裸名，别名 h/r/? 按此命中）。
    """
    from hermes_cli.bang_shell import is_bang_command, parse_bang_command
    if is_bang_command(text):
        return "bang", parse_bang_command(text)
    if _looks_like_slash(text):
        parts = text.split(None, 1)
        name = parts[0][1:].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        return "command", name, args
    return "prompt", text


def menu_items(draft: str) -> list[tuple[str, str]]:
    """斜杠面板候选（D011）：draft 以 / 开头且无空格时，返回匹配的
    [(规范名, 说明)]。前缀过滤（"" 前缀 = 全部），只列规范名不含别名。"""
    if not draft.startswith("/") or " " in draft:
        return []
    want = draft[1:].lower()
    out = []
    for name, d in _COMMANDS.items():
        if name != d.name:
            continue
        if name.startswith(want):
            out.append((d.name, d.help))
    out.sort(key=lambda x: x[0])
    return out


def complete(draft: str) -> str | None:
    """Tab 补全：唯一前缀匹配加空格；多个候选给最长公共前缀。

    返回完整替换（含前导 /）——InputLayer._replace_draft 整行替换。
    """
    if not draft.startswith("/"):
        return None
    want = draft[1:].lower()   # 裸名前缀
    names = sorted({n for n in _COMMANDS if n.startswith(want)})
    if not names:
        return None
    if len(names) == 1:
        return "/" + names[0] + " "
    # 最长公共前缀（须比已输入的长，否则 Tab 无进展）
    lcp = names[0]
    for n in names[1:]:
        i = 0
        while i < len(lcp) and i < len(n) and lcp[i] == n[i]:
            i += 1
        lcp = lcp[:i]
    return "/" + lcp if len(lcp) > len(want) else None


def _run_bang(ctx, command: str) -> None:
    """! 直达：与 terminal 工具同门同黑名单；输出只上终端不进上下文。

    审批回调 TLS 只认主线程（tool_executor 每 turn 新建线程），dispatch 在主
    线程跑所以装得上。yolo 开时 check_bang_approval 自动放行。
    """
    from hermes_cli.bang_shell import check_bang_approval, resolve_bang_cwd, run_bang_command
    from tools.terminal_tool import set_approval_callback

    try:
        from qra.console.approvals import make_modal_approval_callback
        set_approval_callback(make_modal_approval_callback(ctx.inp))
    except Exception:
        pass
    try:
        gate = check_bang_approval(command)
    except Exception:
        gate = {"approved": True, "message": None}
    if not gate.get("approved"):
        ctx.console.print(f"  ⛔ {gate.get('message') or '命令未获批准'}")
        return
    cwd = None
    try:
        cwd = resolve_bang_cwd(ctx.agent.session_id or None)
    except Exception:
        pass

    def _writer(line: str) -> None:
        if ctx.plain:
            print(line)
        else:
            ctx.console.print(line, highlight=False)

    rc = run_bang_command(command, cwd=cwd, writer=_writer)
    if rc != 0:
        _say(ctx, f"  ! 退出码 {rc}")


def _say(ctx, text: str) -> None:
    if ctx.plain:
        print(text)
    else:
        ctx.console.print(text)


def _unknown(ctx, name: str) -> None:
    _say(ctx, f"  未知命令：{name}（/help 查看全部）")


def dispatch(ctx, line: str) -> str:
    """主循环分流点。返回 "bang" / "command" / "prompt" 三态之一。

    命令处理器异常必须兜住——崩了 console 等于死锁输入层。
    """
    res = parse_input(line)
    kind = res[0]
    if kind == "bang":
        _run_bang(ctx, res[1])
        return "bang"
    if kind == "command":
        name, args = res[1], res[2]   # parse_input 命令路是三元组
        d = _COMMANDS.get(name)
        if d is None:
            _unknown(ctx, name)
            return "command"
        try:
            d.handler(ctx, args)
        except Exception as exc:
            _say(ctx, f"  ⚠ {name} 执行失败：{exc}")
            if os.environ.get("QRA_CONSOLE_DEBUG"):
                import traceback
                traceback.print_exc()
        return "command"
    return "prompt"


def maybe_pending(ctx, line: str) -> bool:
    """/resume 待选号模式：纯数字行 → 当 /resume <N> 执行。返回是否已消费。"""
    if not (line.isdigit() and ctx.pending.get("resume")):
        return False
    from qra.console import handlers
    handlers.cmd_resume(ctx, line)
    return True


def _cmd_help(ctx, args: str) -> None:
    rows = []
    for name, d in _COMMANDS.items():
        if name != d.name:
            continue
        rows.append((name, d.usage, d.category, d.help))
    rows.sort(key=lambda r: (r[2], r[0]))
    if ctx.plain:
        for name, usage, cat, help_ in rows:
            _say(ctx, f"  {name:<10} {help_}")
        _say(ctx, "  !<cmd>     直接跑 shell（输出不进上下文，不入历史）")
        return
    from rich.table import Table
    t = Table(title="QRA console 命令")
    t.add_column("命令", style="bold")
    t.add_column("说明")
    cur_cat = None
    for name, usage, cat, help_ in rows:
        if cat != cur_cat:
            t.add_row(f"[dim]{cat}[/dim]", "", end_section=True)
            cur_cat = cat
        d = _COMMANDS[name]
        label = f"/{name}"
        if d.aliases:
            label += f" [dim]({', '.join('/' + a for a in d.aliases)})[/dim]"
        t.add_row(label + f" [dim]{usage}[/dim]", help_)
    ctx.console.print(t)
    ctx.console.print("  !<命令>  直接跑 shell：同 terminal 工具同门审批，"
                      "输出只上终端（不进上下文/历史），120s 超时")


def _register_p0() -> None:
    from qra.console import handlers
    register(CommandDef(
        "help", "", "会话", "显示全部命令", _cmd_help, aliases=("h", "?")))
    register(CommandDef(
        "resume", "[数字|ID|标题]", "会话",
        "无参列出最近会话并进入待选号模式；有参恢复目标会话",
        handlers.cmd_resume, aliases=("r",)))
    register(CommandDef(
        "sessions", "", "会话",
        "列出最近会话（/resume <数字|ID|标题> 恢复）",
        handlers.cmd_sessions, aliases=("ls",)))
    register(CommandDef(
        "clear", "", "会话",
        "开新会话（旧会话保留在库里，可 /resume 找回）",
        handlers.cmd_clear, aliases=("new",)))
    register(CommandDef(
        "compact", "", "会话",
        "手动压缩上下文（fork 新会话，旧会话保留）",
        handlers.cmd_compact, aliases=("compress",)))
    register(CommandDef(
        "export", "[md|jsonl]", "会话",
        "导出当前会话到 HERMES_HOME/exports/",
        handlers.cmd_export, aliases=("e",)))
    register(CommandDef(
        "model", "[deepseek|opus]", "系统",
        "双路由切换：deepseek 直连 ↔ opus@CC proxy 8789；无参看当前路由",
        handlers.cmd_model, aliases=("m",)))
    register(CommandDef(
        "yolo", "", "系统",
        "切换危险命令自动放行（默认开；关后 agent 危险命令自动拒绝，! 可交互审批）",
        handlers.cmd_yolo, aliases=()))
    register(CommandDef(
        "usage", "", "系统",
        "本会话 token 用量与费用估算", handlers.cmd_usage, aliases=("cost",)))
    register(CommandDef(
        "status", "", "系统",
        "会话 / 模型 / 路由 / YOLO / 最近活动一览",
        handlers.cmd_status, aliases=("st",)))
    register(CommandDef(
        "memory", "", "系统",
        "用 $EDITOR 打开 HERMES_HOME/memories/MEMORY.md",
        handlers.cmd_memory, aliases=("mem",)))
    register(CommandDef(
        "loop", "[prompt]", "系统",
        "自动继续：每轮自动以同 prompt 重跑（间隔 60s，Ctrl+C 退出）",
        handlers.cmd_loop, aliases=()))
    register(CommandDef(
        "fold", "[序号]", "显示",
        "折叠块管理：无参列块表，带序号切换折叠（鼠标点击折叠行等效）",
        handlers.cmd_fold, aliases=("f",)))
    register(CommandDef(
        "mouse", "[on|off]", "显示",
        "鼠标捕获开关：开=点击折叠行展开（原生拖选复制/滚轮失效，iTerm2 按住 Option 可临时拖选）；默认关",
        handlers.cmd_mouse, aliases=()))
    register(CommandDef(
        "agents", "", "显示",
        "本进程子代理快照：状态/角色/模型/耗时（delegate_task 类工具）",
        handlers.cmd_agents, aliases=()))


_register_p0()
