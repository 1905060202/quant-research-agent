"""追加式流式渲染器（D011 ADR）。

- 已定型内容只 print 一次 → 自然滚动（屏幕自动跟随）、scrollback 完整、
  从结构上消灭 rich Live 全帧重绘的重复输出。
- 唯一原地刷新的是活动尾部：一行 spinner（\\r），内容到达先擦再印。
  spinner 只在「无流式内容」时出现（等首 token / 工具执行中）——文本与
  思考逐 token 流式期间不弹 spinner，避免 \\r 覆盖半行内容。
- CoT 无框：灰暗逐 token 流式追加；块头「✻ 思考」、块尾「✻ 思考 · 用时 Ns」。
  show_thinking=False（Ctrl+T 折叠）时思考块静默累积，闭合时只印一行 recap。
- 文本块流式期按原样追加（markdown 半成品不转换），闭合时区域重印为 Markdown。
- 工具块默认折叠一行 `⏺ 工具 name · 1.2s · ✓ · 结果 234 字 ▸`；
  点击（SGR 鼠标）或 /fold 展开 = 区域重印（行账本 wrap 感知，仅重印被点块
  及其下方，上方已定型历史不动）。
- 流式期（有 open 块）点击/折叠只改账本，finish 时统一重印——
  避免在流式半行上做光标上移重印。
- 单一写入者：全部输出经 TermIO 串行写出（打字崩终端的根因修复）。

线程模型：回合中仅渲染线程调用；提示符阶段主线程调用（RLock 守护）。
行号是会话级绝对行（不随回合清零），点击命中按「屏幕行 → 绝对行」换算。
"""

from __future__ import annotations

import time
from typing import Any

from rich.markdown import Markdown
from rich.text import Text

from qra.console.termio import TermIO

# 折叠块保留的结果上限（字符）：超出截断存储，展开时注明
_RESULT_STORE_MAX = 64_000
# 折叠行里的结果预览上限（字符）
_RESULT_PREVIEW_MAX = 120

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SUBAGENT_TOOLS = ("delegate_task", "spawn_task", "spawn")


class Block:
    """一个已渲染块的行账本条目。kind: thinking / text / tool。"""

    __slots__ = ("kind", "start_row", "end_row", "collapsed", "text",
                 "name", "args", "result", "ok", "duration", "open",
                 "started", "streamed_rows", "text_rendered", "silent")

    def __init__(self, kind: str, start_row: int) -> None:
        self.kind = kind
        self.start_row = start_row
        self.end_row = start_row
        self.collapsed = False
        self.text = ""          # thinking/text 全文
        self.name = ""          # tool
        self.args: Any = None
        self.result = ""
        self.ok = True
        self.duration = 0.0
        self.open = False       # thinking 流式中
        self.started = time.time()
        self.streamed_rows = 0  # 流式期已占行（闭合时修正）
        self.text_rendered = False   # 文本块是否已做 markdown 重印
        self.silent = False     # 折叠态创建：流式静默，闭合时只印 recap 行

    @property
    def rows(self) -> int:
        return max(0, self.end_row - self.start_row + 1)

    @property
    def is_subagent(self) -> bool:
        return self.kind == "tool" and self.name in _SUBAGENT_TOOLS


def _compact_args(args: Any) -> str:
    if not args:
        return ""
    import json
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    if len(s) > 300:
        s = s[:300] + "…"
    return s


def _thinking_recap(text: str) -> str:
    """折叠摘要：推理中最后一个 **加粗标题**（prime 同款算法）。"""
    last = None
    idx = 0
    while True:
        idx = text.find("**", idx)
        if idx == -1:
            break
        end = text.find("**", idx + 2)
        if end != -1:
            last = text[idx + 2:end].strip()
            idx = end + 2
        else:
            break
    return last or "思考中…"


class TurnRenderer:
    """会话级渲染器：跨回合累积行账本（折叠可作用于历史回合的块）。"""

    def __init__(self, tio: TermIO, state) -> None:
        self._tio = tio
        self._state = state          # TurnState（show_thinking）
        self.blocks: list[Block] = []
        self._row = 0                # 会话级绝对行（0 = 首行）
        self._busy = False           # 回合进行中
        self._spinner_on = False
        self._spinner_text = ""
        self._last_content_at = 0.0
        self._running_tool: Block | None = None   # 执行中的工具（spinner 文案源）
        self._pending_fold: int | None = None     # 流式期折叠：finish 时统一重印

    # ------------------------------------------------------------ 基础输出

    def _erase_spinner(self) -> None:
        if self._spinner_on:
            self._tio.write_bytes(b"\r\x1b[K")
            self._spinner_on = False

    def _draw_spinner(self, text: str) -> None:
        self._spinner_on = True
        self._spinner_text = text
        self._tio.write_bytes(("\r" + text + "\x1b[K").encode("utf-8", "replace"))

    def _pre_content(self) -> None:
        """内容落地前：擦 spinner（内容行顶替 spinner 所在行）。"""
        self._erase_spinner()
        self._last_content_at = time.time()

    def _line(self, obj: Any, **kw) -> int:
        """打印一行并记账；返回占屏行数。"""
        self._pre_content()
        rows = self._tio.print(obj, **kw)
        self._row += rows
        return rows

    def _raw_text(self, text: str, style: str | None = None) -> None:
        """流式追加（不换行记账，闭合时重测）。"""
        self._pre_content()
        if style:
            self._tio.print(Text(text, style=style), end="", markup=False,
                            soft_wrap=True)
        else:
            self._tio.print(text, end="", markup=False, soft_wrap=True)

    def _streaming(self) -> bool:
        """当前是否有流式中的块（思考/文本 open）——有则不动光标重印。"""
        for blk in reversed(self.blocks):
            if blk.open:
                return True
        return False

    # ------------------------------------------------------------ 事件入口

    def begin(self) -> None:
        self._busy = True
        self._last_content_at = time.time()
        self._running_tool = None
        self._pending_fold = None

    def reasoning(self, text: str) -> None:
        if not self._busy:
            return
        blk = self.blocks[-1] if self.blocks else None
        if blk is None or blk.kind != "thinking":
            blk = Block("thinking", self._row)
            blk.open = True
            self.blocks.append(blk)
            if self._state.show_thinking:
                # 块头（灰暗，无框——D011：CoT 不框）
                self._line(Text("✻ 思考", style="grey62"))
                blk.start_row = self._row - 1
                blk.end_row = blk.start_row
                blk.streamed_rows = 1
            else:
                blk.collapsed = True
                blk.silent = True    # 静默累积，闭合时只印 recap 行
        if blk.silent:
            blk.text += text        # 折叠态：不上屏
            return
        self._raw_text(text, style="grey62")
        blk.text += text

    def reasoning_close(self, elapsed: float) -> None:
        blk = self.blocks[-1] if self.blocks else None
        if blk is None or blk.kind != "thinking":
            return
        blk.open = False
        blk.duration = elapsed
        if blk.silent:
            blk.silent = False
            if blk.collapsed:
                # 全程折叠的思考：只印一行 recap
                self._line(Text(f"✻ 思考 · {_thinking_recap(blk.text)}"
                                f" · 用时 {elapsed:.0f}s", style="grey62"))
                blk.start_row = self._row - 1
                blk.end_row = self._row - 1
                return
        if blk.collapsed:
            return   # 折叠态由切换重印显示过，闭合不重复
        self._line(Text(f"✻ 思考 · 用时 {elapsed:.0f}s", style="grey62"))
        blk.end_row = self._row - 1

    def text_delta(self, text: str) -> None:
        if not self._busy:
            return
        blk = self.blocks[-1] if self.blocks else None
        if blk is None or blk.kind != "text":
            blk = Block("text", self._row)
            blk.open = True          # 流式窗口：折叠延迟 + spinner 互斥
            self.blocks.append(blk)
        self._raw_text(text)
        blk.text += text

    def text_close(self) -> None:
        """文本块闭合：区域重印为渲染后的 Markdown（流式期是原文）。"""
        blk = self.blocks[-1] if self.blocks else None
        if blk is None or blk.kind != "text":
            return
        blk.open = False
        if blk.text_rendered:
            return
        blk.text_rendered = True
        if not blk.text:
            return
        old_rows = max(1, self._tio.measure_rows(blk.text, end=""))
        self._erase_spinner()
        self._reprint_block(blk)

    def tool_start(self, tool_call_id: str, name: str, args: Any) -> None:
        if not self._busy:
            return
        blk = Block("tool", self._row)
        blk.name = name
        blk.args = args
        blk.started = time.time()
        self.blocks.append(blk)
        self._running_tool = blk

    def tool_complete(self, tool_call_id: str, name: str, args: Any,
                      result: str, ok: bool) -> None:
        if not self._busy:
            return
        blk = None
        for b in reversed(self.blocks):
            if (b.kind == "tool" and b.name == name
                    and not b.result and b.end_row == b.start_row
                    and not b.collapsed):
                blk = b
                break
        if blk is None:
            blk = Block("tool", self._row)
            blk.name = name
            blk.args = args
            self.blocks.append(blk)
        blk.result = (result or "")[:_RESULT_STORE_MAX]
        blk.ok = ok
        blk.duration = max(0.0, time.time() - blk.started)
        blk.collapsed = True
        if self._running_tool is blk:
            self._running_tool = None
        self._print_tool_line(blk)

    def status(self, message: str) -> None:
        if not self._busy:
            return
        self._line(Text(f"  · {message}", style="dim yellow"))

    def emergency_note(self, message: str) -> None:
        """渲染异常兜底：不依赖任何块状态，直接追加一行红字（主循环调用）。"""
        try:
            self._line(Text(f"⚠ {message}", style="bold red"))
        except Exception:
            pass

    def finish(self, usage: dict | None, model: str) -> None:
        self._erase_spinner()
        self._busy = False
        self._running_tool = None
        if self._pending_fold is not None:
            self._reprint_from(self._pending_fold)
            self._pending_fold = None
        if usage and usage.get("input_tokens"):
            self._line(_render_usage(usage, model))

    # ------------------------------------------------------------ 工具折叠行

    def _tool_title(self, blk: Block) -> str:
        return "⎇ 子代理" if blk.is_subagent else "⏺ 工具"

    def _tool_summary(self, blk: Block) -> str:
        n = len(blk.result)
        tail = ""
        if n > 0:
            preview = " ".join(blk.result[: _RESULT_PREVIEW_MAX].split())
            tail = f" · 结果 {n} 字"
            if n > _RESULT_STORE_MAX:
                tail += "（已截断存储）"
        mark = "✓" if blk.ok else "✗"
        return (f"{self._tool_title(blk)} {blk.name} {_compact_args(blk.args)}"
                f" · {blk.duration:.1f}s · {mark}{tail} ▸")

    def _print_tool_line(self, blk: Block) -> None:
        style = "green" if blk.ok else "red"
        rows = self._line(Text(self._tool_summary(blk), style=style))
        blk.start_row = self._row - rows
        blk.end_row = self._row - 1

    # ------------------------------------------------------------ 折叠展开

    def fold_list(self) -> list[tuple[int, str, str, bool]]:
        """[(序号, 图标, 摘要, 是否折叠)]——/fold 展示用。"""
        out = []
        for i, blk in enumerate(self.blocks, 1):
            if blk.kind == "tool":
                out.append((i, "⏺", self._tool_summary(blk), blk.collapsed))
            elif blk.kind == "thinking":
                out.append((i, "✻", f"思考 · {_thinking_recap(blk.text)}",
                            blk.collapsed))
        return out

    def toggle_block(self, idx: int) -> bool:
        """按 1 基序号切换折叠（/fold <n>，提示符阶段）；返回是否发生切换。"""
        if not (1 <= idx <= len(self.blocks)):
            return False
        blk = self.blocks[idx - 1]
        if blk.kind not in ("tool", "thinking"):
            return False
        blk.collapsed = not blk.collapsed
        if self._streaming():
            self._pending_fold = (idx - 1
                                  if self._pending_fold is None
                                  else min(self._pending_fold, idx - 1))
        else:
            self._reprint_from(idx - 1)
        return True

    def click(self, row: int, col: int) -> bool:
        """SGR 鼠标左键：屏幕行 → 绝对行 → 命中块 → 切换。返回是否命中。"""
        abs_row = self._row - self._tio.height + (row - 1)
        if abs_row < 0:
            return False
        for idx, blk in enumerate(self.blocks):
            if blk.start_row <= abs_row <= blk.end_row:
                if blk.kind not in ("tool", "thinking"):
                    return False
                blk.collapsed = not blk.collapsed
                if self._streaming():
                    self._pending_fold = (idx
                                          if self._pending_fold is None
                                          else min(self._pending_fold, idx))
                else:
                    self._reprint_from(idx)
                return True
        return False

    def toggle_thinking(self) -> None:
        """Ctrl+T：翻转 show_thinking，从第一个 thinking 块起区域重印。"""
        self._state.show_thinking = not self._state.show_thinking
        for idx, blk in enumerate(self.blocks):
            if blk.kind == "thinking":
                blk.collapsed = not self._state.show_thinking
                if self._streaming():
                    self._pending_fold = (idx
                                          if self._pending_fold is None
                                          else min(self._pending_fold, idx))
                else:
                    self._reprint_from(idx)
                return

    # ------------------------------------------------------------ 区域重印

    def _render_block_content(self, blk: Block) -> tuple[list[tuple[Any, dict]], int]:
        """返回 ([(obj, kw)...], 总行数)。kw 走 _tio.print 的选项。"""
        lines: list[tuple[Any, dict]] = []
        rows = 0
        if blk.kind == "thinking":
            dur = blk.duration or max(0.0, time.time() - blk.started)
            if blk.collapsed:
                lines.append((Text(f"✻ 思考 · {_thinking_recap(blk.text)}"
                                   f" · 用时 {dur:.0f}s", style="grey62"), {}))
                rows += 1
            else:
                lines.append((Text("✻ 思考", style="grey62"), {}))
                rows += 1
                rows += self._tio.measure_rows(Text(blk.text, style="grey62"),
                                               end="")
                lines.append((Text(blk.text, style="grey62"),
                              {"markup": False, "end": ""}))
                lines.append((Text(f"✻ 思考 · 用时 {dur:.0f}s",
                                   style="grey62"), {}))
                rows += 1
        elif blk.kind == "text":
            rows += self._tio.measure_rows(Markdown(blk.text), end="")
            lines.append((Markdown(blk.text), {}))
        elif blk.kind == "tool":
            if blk.collapsed:
                style = "green" if blk.ok else "red"
                lines.append((Text(self._tool_summary(blk), style=style), {}))
                rows += 1
            else:
                lines.append((Text(self._tool_summary(blk), style="cyan"), {}))
                rows += 1
                body = ("  " + "  ".join(blk.result.splitlines()[:200])
                        or "（无结果）")
                rows += self._tio.measure_rows(Text(body, style="grey74"))
                lines.append((Text(body, style="grey74"), {"markup": False}))
        return lines, rows

    def _reprint_block(self, blk: Block) -> None:
        """只重印单块（text_close 的 markdown 抛光）：上移旧行数、重印、垫齐。"""
        old_rows = max(1, self._tio.measure_rows(blk.text, end=""))
        lines, new_rows = self._render_block_content(blk)
        if old_rows > 1:
            self._move_up(old_rows - 1)
        printed = 0
        for obj, kw in lines:
            printed += self._tio.print(obj, **kw)
        self._pad(printed, new_rows)
        delta = new_rows - old_rows
        blk.end_row = blk.start_row + new_rows - 1
        self._shift_below(blk, delta)

    def _reprint_from(self, idx: int) -> None:
        """区域重印：块 idx 及其下方全部重印，光标复位（垫行守恒）。

        前置：光标在「提示符行位置」（= 绝对行 self._row），即区域恰好是
        光标上方 old_total 行——回合 finish 后 / 提示符阶段均满足。
        """
        self._erase_spinner()
        old_total = sum(b.rows for b in self.blocks[idx:])
        new_total = 0
        rendered: list[tuple[Block, list[tuple[Any, dict]], int]] = []
        for blk in self.blocks[idx:]:
            lines, rows = self._render_block_content(blk)
            rendered.append((blk, lines, rows))
            new_total += rows
        if old_total > 0:
            self._move_up(old_total)
        printed = 0
        for _blk, lines, _rows in rendered:
            for obj, kw in lines:
                printed += self._tio.print(obj, **kw)
        self._pad(printed, new_total)
        # 修账本：行号逐个重排
        row = self._row - old_total
        for blk, _lines, rows in rendered:
            blk.start_row = row
            blk.end_row = row + rows - 1
            row += rows
        self._shift_below(self.blocks[idx + len(rendered) - 1],
                          new_total - old_total)

    def _shift_below(self, last_blk: Block, delta: int) -> None:
        if delta == 0:
            return
        try:
            i = self.blocks.index(last_blk) + 1
        except ValueError:
            return
        for blk in self.blocks[i:]:
            blk.start_row += delta
            blk.end_row += delta
        self._row += delta

    def _move_up(self, n: int) -> None:
        if n > 0:
            self._tio.write_bytes(f"\x1b[{n}A".encode())

    def _pad(self, printed: int, need: int) -> None:
        """行数不足时补空行（\\x1b[K 清残字），保证光标回到原位。"""
        for _ in range(max(0, need - printed)):
            self._tio.write_bytes(b"\r\x1b[K\n")

    # ------------------------------------------------------------ spinner 心跳

    def tick(self) -> None:
        """渲染线程空闲心跳（0.1s 一次）：活动指示器（呼吸反馈）。

        只在「无流式内容」时画 spinner：等首 token / 工具执行中。
        思考与文本逐 token 流式期间内容本身就是反馈，\\r 会覆盖半行。
        """
        if not self._busy:
            return
        now = time.time()
        text = None
        if self._running_tool is not None:
            el = now - self._running_tool.started
            text = (f"{_SPINNER_FRAMES[int(now * 8) % len(_SPINNER_FRAMES)]} "
                    f"{self._tool_title(self._running_tool)} "
                    f"{self._running_tool.name} 执行中…（{el:.0f}s）")
        elif not self._streaming() and now - self._last_content_at >= 0.3:
            text = (f"{_SPINNER_FRAMES[int(now * 8) % len(_SPINNER_FRAMES)]}"
                    f" 思考中…")
        if text is not None and text != self._spinner_text:
            self._draw_spinner(text)


def _render_usage(usage: dict, model: str) -> Text:
    from qra.console.session_state import PRICE_USD, USD_CNY
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cache = usage.get("cache_read_tokens") or 0
    api = usage.get("api_calls") or 0
    usd = (inp * PRICE_USD["input"] + out * PRICE_USD["output"]
           + cache * PRICE_USD["cache_read"]) / 1e6
    t = Text("▸ ", style="bold")
    t.append(f"{model}", style="bold cyan")
    t.append(f" · in {inp:,} / out {out:,} / cache读 {cache:,} · {api} 次调用",
             style="dim")
    t.append(f" · ≈¥{usd * USD_CNY:.3f}", style="green")
    return t
