"""Frame：固定输入框帧（D011 v4，CC 对齐）。

输出滚动时输入框始终钉在终端底部一层——DECSTBM 滚动区域（内容在
[1..R] 内滚动，帧钉在 [R+1..H] 永不滚动），帧内自上而下分四带：

  提示符带（1..k 行，busy 反显 = CC 式「输入框」）→ 菜单带（0..m 行，
  / 候选面板）→ 活动条带（0/1 行：shell/工具/思考运行中标注+计时）
  → 面板带（0..PANEL_MAX 行：Tab 切入看 shell 输出/本轮工具详情）

帧高随四带动态增减 → 区域同步收缩/扩张。开合纪律（防内容丢失/跳动）：

- 帧变高（菜单/面板/活动条出现）：**不滚内容**——内容保持原位，底部
  行被帧覆盖；记录 (旧区域下界, 覆盖时账本行数) 作恢复判据。
- 帧变矮：扩张区域 → 清释放行 → 若「开合期间账本行数未变」（无新内
  容无滚动，idle 菜单开合的典型场景）按行精确重印被覆盖的内容
  （reprint_abs），零损失；行数变了（busy 面板期间流式内容）则不重印，
  由流式内容自然回填——诚实的降级，绝不印错位内容。

线程纪律：present/各带重绘一律「绝对定位 + 光标 save/restore」——任意
线程（输入线程/渲染线程/shell 线程）随时调用都安全，TermIO 单锁串行
保证字节序列不被穿插；busy 中输入实时回显因此安全（CC 对齐：打字立现）。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from rich.cells import cell_len

PANEL_MAX = 10  # 面板带最大行数（含标题行）


def _slice_disp(s: str, start: int, end: int) -> str:
    """按显示宽度切片（CJK 宽字符感知）；边界上放不下的字符整字跳过。"""
    out: list[str] = []
    pos = 0
    for ch in s:
        w = cell_len(ch)
        if pos >= end:
            break
        if pos + w <= start:
            pos += w
            continue
        if pos >= start and pos + w <= end:
            out.append(ch)
        pos += w
    return "".join(out)


def _pad_disp(s: str, width: int) -> str:
    w = cell_len(s)
    return s + " " * max(0, width - w)


class Frame:
    """固定输入框。绘制全走 TermIO 绝对定位原语 + save/restore。"""

    def __init__(self, tio: Any, prompt: str = "❯ ") -> None:
        self.tio = tio
        self.prompt = prompt
        self.busy = False
        self.draft = ""
        self.cursor = 0  # 草稿内光标（字符位）
        self.menu: tuple[list[str], int] | None = None  # (items, sel)
        self.panel_open = False
        self.panel_scroll = 0
        # 注入（main 接线）：活动条/面板内容来源、offset、被覆盖行恢复
        self.activity_provider: Callable[[], tuple | None] | None = None
        self.panel_provider: Callable[[], tuple[str, list[str]]] | None = None
        self.offset_provider: Callable[[], int] = lambda: 0
        self.restore_cb: Callable[[int, int], None] | None = None
        self.rows_provider: Callable[[], int] | None = None  # 内容账本行数（恢复判据）
        # 内部状态
        self._drawn = False
        self._restore_pending: tuple[int, int] | None = None  # (旧下界, 覆盖时账本行数)
        self._last_activity: tuple | None = None  # 上次绘制的活动 (kind,name,started)
        self._last_activity_text: str | None = None
        self._last_panel_key: tuple | None = None
        self._prompt_rows_drawn = 1
        # 模态（审批/粘贴确认）：问题文字落提示符带，回显走 modal_echo
        self.modal_active = False
        self._modal_pos: tuple[int, int] = (1, 1)  # 回显光标（屏坐标）

    # ------------------------------------------------------------ 布局

    def _prompt_rows(self) -> int:
        w = self.tio.width
        return max(1, (cell_len(self.prompt + self.draft) + w - 1) // w)

    def _menu_rows(self) -> int:
        return len(self.menu[0]) if self.menu else 0

    def _activity_now(self) -> tuple | None:
        if self.activity_provider is None:
            return None
        try:
            return self.activity_provider()
        except Exception:
            return None

    def _panel_content(self) -> tuple[str, list[str]]:
        if self.panel_provider is None:
            return "活动输出", ["（暂无活动输出——运行 ! 命令或等待本轮结束）"]
        try:
            title, lines = self.panel_provider()
            if title is None:
                return "活动输出", ["（暂无活动输出——运行 ! 命令或等待本轮结束）"]
            return title, list(lines)
        except Exception:
            return "活动输出", ["（暂无活动输出——运行 ! 命令或等待本轮结束）"]

    def _zones(self) -> tuple[int, int, int, int]:
        """(prompt, menu, activity, panel) 各带行数。"""
        a = 1 if self._activity_now() is not None else 0
        p = PANEL_MAX if self.panel_open else 0
        return self._prompt_rows(), self._menu_rows(), a, p

    def _offset_now(self) -> int:
        try:
            return max(0, int(self.offset_provider()))
        except Exception:
            return 0

    def _rows_now(self) -> int:
        if self.rows_provider is None:
            return -1
        try:
            return max(0, int(self.rows_provider()))
        except Exception:
            return -1

    # ------------------------------------------------------------ 区域同步

    def _sync_region(self) -> None:
        H = self.tio.height
        zones = self._zones()
        frame_rows = sum(zones)
        R = max(1, H - frame_rows)
        cur = self.tio.region_bottom
        if cur is None:
            cur = H
        if R == cur:
            return
        if R < cur:
            # 帧变高：内容原位不动，底部行被帧覆盖；记恢复判据（仅首次）
            if self._drawn and self._restore_pending is None:
                self._restore_pending = (cur, self._rows_now())
            self.tio.set_region(R)
            return
        # 帧变矮：先扩张区域并清释放行，再按判据精确重印被覆盖内容。
        # 顺序是纪律：restore_cb 的 abs→screen 映射依赖新区域下的
        # offset，erase 必须先于重印否则残字混进重印行。
        pending = self._restore_pending
        self._restore_pending = None
        self.tio.set_region(R)
        for row in range(cur + 1, R + 1):
            self.tio.move(row, 1)
            self.tio.erase_line()
        if (pending is not None and pending[0] == R
                and pending[1] == self._rows_now()
                and self.restore_cb is not None):
            # 被覆盖的绝对行 = [R_new + off .. R_old + off - 1]：屏幕行
            # R_new+1..R_old 映射绝对行 s+off-1，两端代入即得。off 取
            # 扩张后的值（账本行数未变 → 与覆盖时相等）。
            off = self._offset_now()
            a0 = cur + off
            a1 = R + off - 1
            if a0 <= a1:
                try:
                    self.restore_cb(a0, a1)
                except Exception:
                    pass

    # ------------------------------------------------------------ 绘制

    def present(self) -> None:
        """整帧重绘（结构变化用）：区域同步 → 四带绘制 → 光标落位。

        整序列原子（tio.locked）：渲染线程的流式 print 不能插进
        「区域同步 + 四带绘制 + 光标落位」中间，否则 erase 打到错行。
        """
        with self.tio.locked():
            self._sync_region()
            saved = self.tio.cursor_pos
            self._draw_frame()
            if self.busy and not self.modal_active:
                self.tio.move(*saved)  # busy：光标还给内容区（流式继续）
            else:
                self._place_input_cursor()
            self._drawn = True

    def _frame_top(self) -> int:
        R = self.tio.region_bottom
        return (R if R is not None else self.tio.height) + 1

    def _draw_frame(self) -> None:
        R = self.tio.region_bottom
        if R is None:
            return
        top = self._frame_top()
        w = self.tio.width
        row = top
        pr, mr, ar, pr2 = self._zones()
        self._prompt_rows_drawn = pr
        # 提示符带
        full = self.prompt + self.draft
        for i in range(pr):
            seg = _slice_disp(full, i * w, (i + 1) * w)
            self.tio.move(row + i, 1)
            self.tio.erase_line()
            if self.busy:
                self.tio.write_bytes(b"\x1b[7m")
                self.tio.write(_pad_disp(seg, w))
                self.tio.write_bytes(b"\x1b[0m")
            else:
                self.tio.write(seg)
        row += pr
        # 菜单带
        if self.menu:
            items, sel = self.menu
            for i, it in enumerate(items):
                self.tio.move(row + i, 1)
                self.tio.erase_line()
                txt = _pad_disp(_slice_disp(it, 0, w), w)
                if i == sel:
                    self.tio.write_bytes(b"\x1b[7m")
                    self.tio.write(txt)
                    self.tio.write_bytes(b"\x1b[0m")
                else:
                    self.tio.write(txt)
            row += len(items)
        # 活动条带
        if ar:
            self._draw_activity_at(row, w)
            row += 1
        # 面板带
        if pr2:
            self._draw_panel_at(row, w)

    def _activity_text(self, act: tuple, now: float) -> str:
        kind, name, started = act
        el = max(0, now - started)
        if kind == "shell":
            return f"⏵ shell: {name} 运行中…（{el:.0f}s）· Tab 查看"
        if kind == "tool":
            return f"⏺ 工具 {name} 执行中…（{el:.0f}s）· Tab 查看"
        if kind == "subagent":
            return f"⎇ 子代理 {name} 执行中…（{el:.0f}s）· Tab 查看"
        return f"✻ 思考中…（{el:.0f}s）· Tab 查看"

    def _draw_activity_at(self, row: int, w: int) -> None:
        act = self._activity_now()
        if act is None:
            return
        text = self._activity_text(act, time.time())
        self._last_activity = act
        self._last_activity_text = text
        self.tio.move(row, 1)
        self.tio.erase_line()
        self.tio.write_bytes(b"\x1b[2m")
        self.tio.write(_pad_disp(_slice_disp(text, 0, w), w))
        self.tio.write_bytes(b"\x1b[0m")

    def _draw_panel_at(self, row: int, w: int) -> None:
        title, lines = self._panel_content()
        self._last_panel_key = (title, tuple(lines))
        # 标题行
        self.tio.move(row, 1)
        self.tio.erase_line()
        self.tio.write_bytes(b"\x1b[1m")
        self.tio.write(_pad_disp(_slice_disp(f"▸ {title}", 0, w), w))
        self.tio.write_bytes(b"\x1b[0m")
        # 内容行（可滚动）
        body_rows = PANEL_MAX - 1
        n = len(lines)
        self.panel_scroll = max(0, min(self.panel_scroll, max(0, n - body_rows)))
        view = lines[self.panel_scroll:self.panel_scroll + body_rows]
        for i in range(body_rows):
            r = row + 1 + i
            self.tio.move(r, 1)
            self.tio.erase_line()
            if i < len(view):
                self.tio.write_bytes(b"\x1b[2m")
                self.tio.write(_pad_disp(_slice_disp(view[i], 0, w), w))
                self.tio.write_bytes(b"\x1b[0m")

    def _redraw_activity_row(self) -> None:
        """活动条带原地重绘（计时刷新），save/restore（整序列原子）。"""
        with self.tio.locked():
            saved = self.tio.cursor_pos
            row = self._frame_top() + self._prompt_rows_drawn + self._menu_rows()
            self._draw_activity_at(row, self.tio.width)
            self.tio.move(*saved)

    def _redraw_panel_zone(self) -> None:
        """面板带原地重绘（内容刷新/滚动），save/restore（整序列原子）。"""
        with self.tio.locked():
            saved = self.tio.cursor_pos
            row = self._frame_top() + self._prompt_rows_drawn + self._menu_rows()
            act = self._activity_now()
            if act is not None:
                row += 1
            self._draw_panel_at(row, self.tio.width)
            self.tio.move(*saved)

    def _redraw_prompt_zone(self) -> None:
        """提示符带原地重绘（草稿/光标变化），save/restore；idle 落输入光标。"""
        with self.tio.locked():
            saved = self.tio.cursor_pos
            top = self._frame_top()
            pr = self._prompt_rows()
            self._prompt_rows_drawn = pr
            w = self.tio.width
            full = self.prompt + self.draft
            for i in range(pr):
                seg = _slice_disp(full, i * w, (i + 1) * w)
                self.tio.move(top + i, 1)
                self.tio.erase_line()
                if self.busy:
                    self.tio.write_bytes(b"\x1b[7m")
                    self.tio.write(_pad_disp(seg, w))
                    self.tio.write_bytes(b"\x1b[0m")
                else:
                    self.tio.write(seg)
            if self.busy and not self.modal_active:
                self.tio.move(*saved)
            else:
                self._place_input_cursor()

    def _place_input_cursor(self) -> None:
        """光标落草稿光标位（提示符带内换行感知）。"""
        w = self.tio.width
        prefix = self.prompt + self.draft[:self.cursor]
        d = cell_len(prefix)
        top = self._frame_top()
        self.tio.move(top + d // w, d % w + 1)

    # ------------------------------------------------------------ 状态变更

    def input_changed(self, draft: str, cursor: int) -> None:
        """输入线程每键调用：草稿变化 → 提示符带重绘（busy 也实时回显）。"""
        self.draft, self.cursor = draft, cursor
        pr = self._prompt_rows()
        if self.modal_active:
            return
        if pr != self._prompt_rows_drawn:
            self.present()  # 换行数变化 → 帧高变化 → 结构重绘
        else:
            self._redraw_prompt_zone()

    def set_busy(self, busy: bool) -> None:
        if busy == self.busy:
            return
        self.busy = busy
        self.present()

    def menu_changed(self, items: list[str], sel: int) -> None:
        self.menu = (items, sel)
        self.present()

    def menu_closed(self) -> None:
        if self.menu is None:
            return
        self.menu = None
        self.present()

    def toggle_panel(self) -> bool:
        self.panel_open = not self.panel_open
        self.panel_scroll = 0
        if not self.panel_open:
            self._last_panel_key = None
        self.present()
        return self.panel_open

    def panel_scroll_by(self, delta: int) -> None:
        if not self.panel_open:
            return
        self.panel_scroll = max(0, self.panel_scroll + delta)
        self._redraw_panel_zone()

    def tick(self) -> bool:
        """心跳（渲染循环 0.1s / 输入线程 0.2s 兜底）：活动条计时 + 面板刷新。
        返回是否有输出。"""
        act = self._activity_now()
        text = self._activity_text(act, time.time()) if act else None
        structural = (text is not None) != (self._last_activity_text is not None)
        if structural:
            self._last_activity_text = text
            self.present()
            return True
        changed = False
        if text is not None and text != self._last_activity_text:
            self._redraw_activity_row()
            changed = True
        if self.panel_open:
            title, lines = self._panel_content()
            key = (title, tuple(lines))
            if key != self._last_panel_key:
                self._redraw_panel_zone()
                changed = True
        return changed

    # ------------------------------------------------------------ 模态

    def modal_begin(self, text: str) -> None:
        """模态问题落提示符带（审批/粘贴确认）。回显走 modal_echo。"""
        with self.tio.locked():
            self.modal_active = True
            top = self._frame_top()
            pr = self._prompt_rows_drawn
            w = self.tio.width
            self.tio.move(top, 1)
            for i in range(pr):
                self.tio.move(top + i, 1)
                self.tio.erase_line()
            self.tio.write(_pad_disp(_slice_disp(text, 0, w), w))
            self._modal_pos = (top, cell_len(text) + 1)

    def modal_echo(self, text: str) -> None:
        """模态输入回显（逐字符），行内换行感知。"""
        if not self.modal_active:
            return
        with self.tio.locked():
            row, col = self._modal_pos
            w = self.tio.width
            for ch in text:
                if ch == "\r" or ch == "\n":
                    continue
                if ch == "\x7f" or ch == "\b":
                    if col > 1:
                        col -= 1
                    continue
                if cell_len(ch) + col - 1 > w:
                    row += 1
                    col = 1
                self.tio.move(row, col)
                self.tio.write(ch)
                col += cell_len(ch)
            self._modal_pos = (row, col)

    def modal_end(self) -> None:
        """模态结束：清空提示符带，恢复草稿。"""
        self.modal_active = False
        self._redraw_prompt_zone()
