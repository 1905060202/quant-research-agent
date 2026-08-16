"""追加式流式渲染器（D011 v4：固定输入框帧版）。

- 已定型内容只 print 一次 → 自然滚动（屏幕自动跟随）、scrollback 完整、
  从结构上消灭 rich Live 全帧重绘的重复输出。
- CoT 无框：灰暗逐 token 流式追加；块头「✻ 思考」、块尾「✻ 思考 · 用时 Ns」。
  show_thinking=False（Ctrl+T 折叠）时思考块静默累积，闭合时只印一行 recap。
- 文本块流式期按原样追加（markdown 半成品不转换），闭合时区域重印为 Markdown。
- 工具块默认折叠一行 `⏺ 工具 name · 1.2s · ✓ · 结果 234 字 ▸`；
  点击（SGR 鼠标）或 /fold 展开 = 区域重印（行账本 wrap 感知，仅重印被点块
  及其下方，上方已定型历史不动）。
- 流式期（有 open 块）点击/折叠只改账本，finish 时统一重印——
  避免在流式半行上做光标上移重印。
- 单一写入者：全部输出经 TermIO 串行写出（打字崩终端的根因修复）。

v4 变化（配合 frame.py 固定输入框）：

- **spinner 退役**：呼吸反馈移入 Frame 活动条带（输入框下方标注行，
  CC 对齐），本模块只以 `activity()` 提供 (kind, name, started) 内容源。
- **content_end 追踪**：每次内容写出后记录光标位；`begin()` 回合开始、
  `append_line()` 命令/摘要输出都先绝对定位到内容尾部再印——帧占据屏底
  后，内容写入点与提示符位置解耦，一切内容输出都从账本上的「内容尾」续。
- **屏幕↔绝对行换算**：`offset() = max(0, _row - R + 1)`（R=滚动区域下界；
  R 越小 offset 越大），屏幕行 s ↔ 绝对行 s + offset - 1。区域重印全部
  绝对定位 + 先擦后印，不再依赖「光标在提示符紧下方」的旧假设（有帧后
  提示符与内容尾之间隔着空白带，旧公式必错）。
- **reprint_abs**：Frame 收缩恢复——把被覆盖的绝对行按行重印回原位
  （行粒度；跨上界的块整块跳过，宁留小洞不印错位）。

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
        self._content_end = (1, 1)   # 内容尾部写入点（屏幕坐标，帧解耦）
        self._last_content_at = 0.0
        self._running_tool: Block | None = None   # 执行中的工具（活动条内容源）
        self._pending_fold: int | None = None     # 流式期折叠：finish 时统一重印
        # raw 行账本：_line/append_line 逐行原样（首行 → (obj, kw)）。
        # 块外的横幅/回显/命令输出/用量页脚不建 Block，帧收缩恢复
        # （reprint_abs）必须能原样重印它们——没有这份账本恢复就丢行。
        self._raw_rows: dict[int, tuple[Any, dict]] = {}

    # ------------------------------------------------------------ 基础输出

    def _region_bottom(self) -> int:
        R = self._tio.region_bottom
        return R if R is not None else self._tio.height

    def offset(self) -> int:
        """屏幕行 s ↔ 绝对行 (s + offset - 1)。滚动越多 offset 越大。"""
        return max(0, self._row - self._region_bottom() + 1)

    def _note_content_end(self) -> None:
        self._content_end = self._tio.cursor_pos

    def _move_to_content_end(self) -> None:
        if not self._tio.is_tty:
            return
        r, c = self._content_end
        R = self._region_bottom()
        self._tio.move(max(1, min(r, R)), c)

    def _sync_cursor(self) -> None:
        """自愈：光标不在内容尾（模态回显等外部绘制动过）→ 先落回再写。"""
        if self._tio.cursor_pos != self._content_end:
            self._move_to_content_end()

    def _line(self, obj: Any, **kw) -> int:
        """打印一行并记账；返回占屏行数。「move→print」整体持锁：帧绘制
        插不进来（与 append_line 同纪律，2026-08-17 审计 F-04）。"""
        with self._tio.locked():
            self._sync_cursor()
            self._last_content_at = time.time()
            rows = self._tio.print(obj, **kw)
            self._raw_rows[self._row] = (obj, dict(kw))
            self._row += rows
            self._note_content_end()
            return rows

    def _raw_text(self, text: str, style: str | None = None) -> None:
        """流式追加（不换行记账，闭合时重测）。同 append_line 整体持锁
        （2026-08-17 审计 F-04：旧版 move→print 两步间可插入帧重绘，
        token 打进输入框区）。"""
        with self._tio.locked():
            self._sync_cursor()
            self._last_content_at = time.time()
            if style:
                self._tio.print(Text(text, style=style), end="", markup=False,
                                soft_wrap=True)
            else:
                self._tio.print(text, end="", markup=False, soft_wrap=True)
            self._note_content_end()

    def append_line(self, obj: Any = "", **kw) -> int:
        """主线程/命令路径的内容输出：光标移到内容尾再印（帧外调用安全）。

        任何不在渲染线程里的内容输出（横幅、/命令结果、! shell 摘要、
        错误行）都走这里——有固定帧后，光标可能停在提示符上，直接印会
        打在帧里，必须先落回内容尾。「move→print」两步整体持锁：帧绘制
        插不进来（否则 erase 错位）。无参 = 空行分隔（旧 tio.print() 语义）。
        """
        with self._tio.locked():
            self._move_to_content_end()
            rows = self._tio.print(obj, **kw)
            self._raw_rows[self._row] = (obj, dict(kw))
            self._row += rows
            self._note_content_end()
            return rows

    def _streaming(self) -> bool:
        """当前是否有流式中的块（思考/文本 open）——有则不动光标重印。"""
        for blk in reversed(self.blocks):
            if blk.open:
                return True
        return False

    def activity(self, now: float | None = None) -> tuple | None:
        """活动条内容源（Frame 注入）：(kind, name, started) 或 None。

        kind: tool / subagent / thinking。只在「工具执行中」或「等待首
        token」时返回——流式期间内容本身就是反馈，不弹活动标注。
        """
        if not self._busy:
            return None
        now = time.time() if now is None else now
        if self._running_tool is not None:
            kind = "subagent" if self._running_tool.is_subagent else "tool"
            return (kind, self._running_tool.name, self._running_tool.started)
        if not self._streaming() and now - self._last_content_at >= 0.3:
            return ("thinking", "", self._last_content_at)
        return None

    # ------------------------------------------------------------ 事件入口

    def begin(self) -> None:
        self._busy = True
        self._last_content_at = time.time()
        self._running_tool = None
        self._pending_fold = None
        # 有帧后光标停在提示符：回合开始先把光标落回内容尾
        self._move_to_content_end()

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
            blk.open = True          # 流式窗口：折叠延迟 + 活动条互斥
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
            self.append_line(Text(f"⚠ {message}", style="bold red"))
        except Exception:
            pass

    def finish(self, usage: dict | None, model: str) -> None:
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
        """SGR 鼠标左键：屏幕行 → 绝对行 → 命中块 → 切换。返回是否命中。

        v4：绝对行 = 屏幕行 + offset - 1（offset 随帧高/滚动变化，
        旧公式假定光标紧贴内容尾，有帧后不成立）。
        """
        if row > self._region_bottom():
            return False   # 帧区（提示符/菜单/面板），不属内容
        abs_row = row + self.offset() - 1
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
        """只重印单块（text_close 的 markdown 抛光）：先擦旧区再印，流续位重算。

        v4：绝对定位重印（旧实现靠「光标在内容尾」的相对上移，有帧后
        光标语义不成立）；重印后光标落回「流续位」= 旧流位与块新尾的
        较大者（markdown 行数可能多于/少于流式原文行数）。
        """
        with self._tio.locked():
            old_rows = max(1, self._tio.measure_rows(blk.text, end=""))
            self._drop_raw(blk.start_row, blk.start_row + old_rows - 1)
            lines, new_rows = self._render_block_content(blk)
            offset = self.offset()
            start_screen = blk.start_row - offset + 1
            saved = self._tio.cursor_pos
            for r in range(start_screen, start_screen + old_rows):
                self._tio.move(r, 1)
                self._tio.erase_line()
            self._tio.move(start_screen, 1)
            for obj, kw in lines:
                self._tio.print(obj, **kw)
            end_screen = start_screen + new_rows - 1
            cont_row = max(saved[0], end_screen + 1)
            self._tio.move(cont_row, 1)
            self._note_content_end()
            delta = new_rows - old_rows
            blk.end_row = blk.start_row + new_rows - 1
            self._shift_below(blk, delta)

    def _reprint_from(self, idx: int) -> None:
        """区域重印：块 idx 及其下方全部重印（先擦后印，绝对定位）。

        v4：起始屏幕行由账本 absolute 行 + offset 换算（旧实现假定光标
        在提示符紧下方、靠 _move_up 相对定位——有帧后不成立）。
        """
        with self._tio.locked():
            old_total = sum(b.rows for b in self.blocks[idx:])
            self._drop_raw(self.blocks[idx].start_row,
                           self.blocks[idx].start_row + old_total - 1)
            new_total = 0
            rendered: list[tuple[Block, list[tuple[Any, dict]], int]] = []
            for blk in self.blocks[idx:]:
                lines, rows = self._render_block_content(blk)
                rendered.append((blk, lines, rows))
                new_total += rows
            offset = self.offset()
            start_screen = self.blocks[idx].start_row - offset + 1
            # 先擦旧区域（残字 + 行数差都清掉，滚动不漂移）
            for r in range(start_screen, start_screen + old_total):
                self._tio.move(r, 1)
                self._tio.erase_line()
            # 重印
            self._tio.move(start_screen, 1)
            for _blk, lines, _rows in rendered:
                for obj, kw in lines:
                    self._tio.print(obj, **kw)
            # 光标落新内容尾（末行 end="" 时补换行；区域滚动时光标自然留底）
            r, c = self._tio.cursor_pos
            if c > 1:
                self._tio.cr()
                self._tio.move_down(1)
            self._note_content_end()
            # 修账本：行号逐个重排
            row = self.blocks[idx].start_row
            for blk, _lines, rows in rendered:
                blk.start_row = row
                blk.end_row = row + rows - 1
                row += rows
            self._shift_below(self.blocks[idx + len(rendered) - 1],
                              new_total - old_total)

    def reprint_abs(self, a0: int, a1: int) -> bool:
        """按绝对行重印 [a0..a1] 的内容（Frame 收缩恢复被覆盖行）。

        行粒度：raw 行账本（_line/append_line 记账，逐行原样）优先，
        块内部行由块渲染补充；跨上界的块整块跳过（宁留小洞不印错位）。
        返回是否真的印了内容。调用方保证区域已扩张回目标下界、且调用
        期间无内容滚动（offset 稳定）。
        """
        if a0 > a1 or a0 > self._row or not self._tio.is_tty:
            return False
        a1 = min(a1, self._row)
        by_row: dict[int, tuple[Any, dict]] = {}
        for a in range(a0, a1 + 1):
            if a in self._raw_rows:
                by_row[a] = self._raw_rows[a]
        for blk in self.blocks:
            if blk.end_row < a0 or blk.start_row > a1:
                continue
            if blk.start_row < a0:
                continue   # 块跨上界：跳过，避免首行错位
            lines, _ = self._render_block_content(blk)
            row = blk.start_row
            for obj, kw in lines:
                rows = self._tio.measure_rows(obj, end=kw.get("end", "\n"))
                if a0 <= row <= a1 and row not in by_row:
                    by_row[row] = (obj, dict(kw))
                row += rows
        if not by_row:
            return False
        with self._tio.locked():
            offset = self.offset()
            saved = self._tio.cursor_pos
            for abs_row in sorted(by_row):
                obj, kw = by_row[abs_row]
                self._tio.move(abs_row - offset + 1, 1)
                self._tio.print(obj, **kw)
            self._tio.move(*saved)
            return True

    def _drop_raw(self, a0: int, a1: int) -> None:
        """丢弃 [a0..a1] 内的 raw 行（区域重印后原样行失效，由块渲染接管）。"""
        for k in [k for k in self._raw_rows if a0 <= k <= a1]:
            del self._raw_rows[k]

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
        if self._raw_rows:
            # raw 行账本同步平移：旧块尾（新 end_row - delta）之下的原样
            # 行跟着内容一起移位，否则帧收缩恢复会印回旧位置。
            bound = last_blk.end_row - delta
            moved = {k + delta: v for k, v in self._raw_rows.items()
                     if k > bound}
            if moved:
                self._raw_rows = {k: v for k, v in self._raw_rows.items()
                                  if k <= bound}
                self._raw_rows.update(moved)
        self._row += delta


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
