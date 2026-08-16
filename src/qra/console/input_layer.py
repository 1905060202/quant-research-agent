"""输入层（会话级）：cbreak-noecho + 读线程 + LineBuffer 行编辑 + 斜杠菜单
+ SGR 鼠标 + 括号粘贴（D011 v2）。

从 main.py 迁出并扩展。prime 机理落地：单一持久读线程 + 自有行编辑，
不依赖 input()/readline。此前"每回合起停 key thread"的架构有交还竞态——
key thread 读走字节后主循环已越过缓冲检查，input() 永远等不到（pty 实证
挂死）。会话级输入层没有 raw/cooked 转换窗口：回合中敲入直接进行缓冲，
回合后主循环从队列取行。

P0 修复（2026-08-16 雅宁实测）：
- 「输出时打字终端崩」：回显改走 TermIO 单一写入者（与渲染器同一把锁），
  字节流不可能再插进渲染转义序列中间；回合中（busy）回显静音、按键照常
  进 LineBuffer，回合结束 redraw() 一次补画提示符与草稿。
- 「←→ 光标无法移动」：LineBuffer 行编辑模型 + ←→/Home/End/Delete/
  行中插入删除 + SGR 鼠标点击定位光标。

交互功能：
- ↑↓ 历史（ConsoleHistory 导航，整行替换重绘）；菜单打开时 ↑↓ 导航菜单
- Tab：斜杠菜单打开时补最长公共前缀；否则 completer 回调（命令名补全）
- 斜杠菜单：draft 以 `/` 开头且无空格 → menu_provider 提供候选，
  提示符下方反显面板，随输入过滤；Enter 选中补全、Esc 关闭
- SGR 鼠标：左键点提示符定位光标、点菜单行选中、回合中点击直达渲染器
- 括号粘贴（2004h）：粘贴内容一次性进缓冲、只重绘一次；大块（≥4096 字节）
  提交前模态确认（detect_paste 突发判据保留作无括号粘贴终端的回退）
- ask_modal：主线程的模态问答委托给读线程（不抢 stdin）
- pause/resume：/memory 起编辑器前还原 termios + 关鼠标/粘贴模式
"""

from __future__ import annotations

import codecs
import os
import queue
import select
import signal
import sys
import termios
import threading
import time
import unicodedata
from typing import Any

from qra.console.linebuffer import LineBuffer
from qra.console.termio import TermIO

_MENU_MAX_ITEMS = 12          # 面板最多显示条数（v1 无面板内滚动，诚实边界）
_PASTE_CONFIRM_BYTES = 4096   # 大块粘贴确认阈值（与 detect_paste 同源）


def _char_width(c: str) -> int:
    """终端显示列宽（CJK 全角占 2 列），光标定位/截断用。"""
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def _disp_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def _trunc(s: str, w: int) -> str:
    """按显示列宽截断并垫到整宽（菜单行反显铺满）。"""
    out: list[str] = []
    width = 0
    for ch in s:
        cw = _char_width(ch)
        if width + cw > w:
            break
        out.append(ch)
        width += cw
    return "".join(out) + " " * (w - width)


def _pos_at_display_col(text: str, col: int) -> int:
    """显示列 → 字符下标（SGR 点击定位）。返回第一个起点 ≥ col 的字符。"""
    width = 0
    for i, ch in enumerate(text):
        cw = _char_width(ch)
        if width + cw > col:
            return i
        width += cw
    return len(text)


def detect_paste(n_bytes: int, span_ms: float,
                 threshold_bytes: int = 4096,
                 threshold_ms: float = 200.0) -> bool:
    """大块粘贴判据（纯函数，单测锁定）：单 chunk 字节数 ≥ 阈值且距上一
    chunk 不足阈值毫秒 → True。人手打字不可能 200ms 内灌入 4KB。"""
    return n_bytes >= threshold_bytes and span_ms < threshold_ms


class _Menu:
    __slots__ = ("items", "sel", "rows")

    def __init__(self, items: list[tuple[str, str]], sel: int = 0) -> None:
        self.items = items          # [(name, desc)]
        self.sel = sel
        self.rows = len(items)


class InputLayer:
    """交互会话持有一个实例：cbreak-noecho + 读线程 + 行队列。

    行为：
    - Ctrl+T 折叠/展开思考（回合中经事件直达渲染器，空闲时置 dirty）
    - Ctrl+C 还原 SIGINT（cbreak 下 ISIG 已关）；Ctrl+Z → SIGTSTP
    - 回车提交整行进队列；^D 空行 = EOF
    - ←→/Home/End/Ctrl+A/Ctrl+E 移动光标；Backspace/Delete/行中插入
    - ↑↓ 历史导航；Tab 补全；大块粘贴先确认
    - 所有回显经 TermIO 单一写入者（锁内串行，与渲染器不交错）
    - 回合中（busy）回显静音，按键进 LineBuffer 缓冲，回合结束补画
    """

    EOF = object()

    def __init__(self, state, history=None, completer=None,
                 prompt: str = "❯ ", tio: TermIO | None = None,
                 menu_provider=None) -> None:
        self._q: queue.Queue = queue.Queue()
        self._modal_q: queue.Queue = queue.Queue()   # (prompt, result_q)
        self._state = state
        self._history = history      # ConsoleHistory | None
        self._completer = completer  # callable(draft) -> str | None
        self._prompt = prompt        # 提示符（纯文本，行首重绘用）
        self._tio = tio if tio is not None else TermIO()   # 几何（宽/高）+ 回显通道
        self._tty_out = self._tio   # 回显通道：单测置 None 关回显（旧契约，D011 保留）
        self._menu_provider = menu_provider  # callable(draft) -> [(name, desc)]
        self._fd = sys.stdin.fileno()
        self._stop = threading.Event()
        self._paused = False
        self._busy = False           # 回合进行中：回显静音
        self._event_sink = None      # 回合中事件直达渲染器：callable(tuple)
        self._rows_before_prompt = 0  # 提示符首行上方已打印的行数（点击定位）
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="qra-console-input")
        # 行编辑：LineBuffer（光标模型）+ 增量解码器
        self._buf = LineBuffer()
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # 光标跟踪：相对提示符首行的 (行, 列)——所有重绘移动都据此发 CUU/CUD
        self._cur_row = 0
        self._cur_col = 0
        self._prompt_rows_prev = 0   # 上一帧提示符占屏行数（缩行清残字）
        # 菜单（斜杠面板）
        self._menu: _Menu | None = None
        self._mouse_on = False       # SGR 鼠标捕获状态（默认关：保原生拖选复制）
        # CSI 状态机 + 括号粘贴 + 粘贴突发回退
        self._esc: bytearray | None = None
        self._bracket = False        # 括号粘贴区间内（200~ … 201~）
        self._paste_buf = bytearray()
        self._burst_pending = False  # 突发判据命中，回车时确认
        self._last_chunk_at = time.monotonic()
        self._burst_bytes = 0
        self._burst_start = 0.0
        # termios 基线（stdin 非 tty 时为 None，单测/降级路径）
        try:
            self._old_termios = termios.tcgetattr(self._fd)
        except termios.error:
            self._old_termios = None

    # ------------------------------------------------------------ 公共

    def start(self) -> None:
        self._thread.start()

    def pop(self) -> str:
        item = self._q.get()
        if item is self.EOF:
            raise EOFError
        return item

    def draft(self) -> str:
        return self._buf.text

    def _backspace(self) -> None:
        """按字符退格（旧单测入口保留；LineBuffer 按字符记账，无字节地雷）。"""
        self._buf.backspace()
        self._after_edit()

    def redraw(self) -> None:
        """回合结束后把提示符 + 草稿一次补画（回合中静音回显的落点）。"""
        self._present()

    def set_prompt(self, prompt: str) -> None:
        self._prompt = prompt

    def set_busy(self, busy: bool) -> None:
        """回合开始 busy=True（关菜单、静音）；结束 busy=False（补画提示符）。"""
        if busy:
            if self._menu is not None:
                self._menu_close()
            self._busy = True
        else:
            self._busy = False
            self._present()

    def set_event_sink(self, sink) -> None:
        """回合中事件（鼠标点击 / Ctrl+T）直达渲染器队列：callable(tuple)。"""
        self._event_sink = sink

    def set_content_rows(self, n: int) -> None:
        """提示符首行上方已打印的行数（renderer 会话级行号），点击定位用。"""
        self._rows_before_prompt = n

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def ask_modal(self, prompt: str) -> str:
        """主线程调用：模态取行委托给读线程（主线程不得与读线程并发抢 stdin）。"""
        if threading.current_thread() is self._thread:
            return self._modal_read(prompt)
        rq: queue.Queue = queue.Queue()
        self._modal_q.put((prompt, rq))
        try:
            return rq.get(timeout=300)
        except queue.Empty:
            return ""

    def pause(self) -> None:
        """/memory 起编辑器前调用：读线程挂起 + termios 还原 + 关鼠标/粘贴。"""
        self._paused = True
        self._w(b"\x1b[?1000l\x1b[?1006l\x1b[?2004l")
        if self._old_termios is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
            except termios.error:
                pass

    def resume(self) -> None:
        """编辑器退出后调用：重建 cbreak 并恢复读线程（还原鼠标捕获状态）。"""
        was_mouse = self._mouse_on
        self._apply_raw()
        if was_mouse:
            self.set_mouse(True)
        self._paused = False

    # ------------------------------------------------------------ 终端低层

    def _w(self, data: bytes) -> None:
        if self._tty_out is not None:
            self._tty_out.write_bytes(data)

    def _goto(self, row: int, col: int) -> None:
        """移到（相对提示符首行的）row 行 col 列；跟踪模型据此发 CUU/CUD/\\r/CUF。"""
        dr = self._cur_row - row
        if dr > 0:
            self._w(f"\x1b[{dr}A".encode())
        elif dr < 0:
            self._w(f"\x1b[{-dr}B".encode())
        self._w(b"\r")
        if col > 0:
            self._w(f"\x1b[{col}C".encode())
        self._cur_row, self._cur_col = row, col

    def _layout(self) -> tuple[int, int, int, int, int]:
        """(宽, 文本末行, 文本末列, 光标行, 光标列)——全部相对提示符首行。"""
        w = self._tio.width
        pw = _disp_width(self._prompt)
        text = self._buf.text
        end = pw + _disp_width(text)
        rows_end, col_end = divmod(end, w)
        pp = pw + _disp_width(text[: self._buf.pos])
        rows_pos, col_pos = divmod(pp, w)
        return w, rows_end, col_end, rows_pos, col_pos

    def _present(self) -> None:
        """整帧补画：提示符 + 草稿 + 光标定位（回合结束 / 首次显示）。"""
        self._cur_row, self._cur_col = 0, 0      # 渲染器已把真实光标带到新行首
        self._prompt_rows_prev = 0
        w, rows_end, col_end, rows_pos, col_pos = self._layout()
        self._w((self._prompt + self._buf.text).encode("utf-8", "replace"))
        self._w(b"\x1b[K")
        self._prompt_rows_prev = rows_end + 1
        self._goto(rows_pos, col_pos)

    def _redraw_line(self) -> None:
        """每键重绘：整行重印 + 清上一帧多出的行 + 光标定位（wrap 感知）。"""
        w, rows_end, col_end, rows_pos, col_pos = self._layout()
        rows = rows_end + 1
        self._goto(0, 0)
        self._w((self._prompt + self._buf.text).encode("utf-8", "replace"))
        self._w(b"\x1b[K")
        extra = self._prompt_rows_prev - rows   # 文本变短：清残留行
        if extra > 0:
            for i in range(extra):
                self._goto(rows + i, 0)
                self._w(b"\x1b[2K")
        self._prompt_rows_prev = rows
        self._goto(rows_pos, col_pos)

    def _after_edit(self) -> None:
        """任意编辑后的统一收口：重绘 + 菜单过滤（回合中静音跳过）。"""
        if self._busy:
            return
        self._redraw_line()
        t = self._buf.text
        if t.startswith("/") and " " not in t:
            self._menu_filter()
        elif self._menu is not None:
            self._menu_close()

    def _insert_text(self, text: str) -> None:
        for ch in text:
            self._buf.insert(ch)
        self._after_edit()

    # ------------------------------------------------------------ 斜杠菜单

    def _menu_filter(self) -> None:
        old_k = self._menu.rows if self._menu is not None else 0
        items = self._menu_provider(self._buf.text) if self._menu_provider else []
        if not items:
            self._menu_close()
            return
        if len(items) > _MENU_MAX_ITEMS:
            items = items[: _MENU_MAX_ITEMS]
        sel = min(self._menu.sel if self._menu is not None else 0,
                  len(items) - 1)
        self._menu = _Menu(items, sel)
        self._menu_redraw(old_k)

    def _menu_redraw(self, old_k: int) -> None:
        """面板重绘：从提示符下方第一行起，清 max(旧,新) 行、印新项、光标回位。"""
        w, rows_end, col_end, rows_pos, col_pos = self._layout()
        m = self._menu
        total = max(m.rows, old_k)
        self._goto(rows_end + 1, 0)
        for i in range(total):
            self._w(b"\x1b[2K")
            if i < m.rows:
                name, desc = m.items[i]
                line = _trunc(f"{name}  {desc}", w)
                if i == m.sel:
                    self._w(b"\x1b[7m" + line.encode("utf-8", "replace")
                            + b"\x1b[0m")
                else:
                    self._w(line.encode("utf-8", "replace"))
            if i < total - 1:
                self._goto(rows_end + 2 + i, 0)
        self._goto(rows_pos, col_pos)

    def _menu_close(self) -> None:
        if self._menu is None:
            return
        k = self._menu.rows
        self._menu = None
        self._clear_menu_rows(k)

    def _clear_menu_rows(self, k: int) -> None:
        w, rows_end, col_end, rows_pos, col_pos = self._layout()
        self._goto(rows_end + 1, 0)
        for i in range(k):
            self._w(b"\x1b[2K")
            if i < k - 1:
                self._goto(rows_end + 2 + i, 0)
        self._goto(rows_pos, col_pos)

    def _menu_move(self, d: int) -> None:
        m = self._menu
        m.sel = max(0, min(m.rows - 1, m.sel + d))
        self._menu_redraw(m.rows)

    def _menu_apply(self) -> None:
        """Enter：选中项补全进草稿并加空格（保留前导 /）。

        D011 定稿：菜单选中即执行——调用方在 apply 后立即 _submit()。
        Tab 只补全不执行（留给用户补参数）。
        """
        name = self._menu.items[self._menu.sel][0]
        k = self._menu.rows
        self._menu = None
        self._clear_menu_rows(k)
        self._buf.replace("/" + name + " ")
        self._after_edit()

    def _menu_tab(self) -> None:
        """Tab：补最长公共前缀；唯一命中补全并加空格（保留前导 /）。

        前缀比较用裸名（草稿去前导 /），替换时再拼回 /。
        """
        names = [n for n, _ in self._menu.items]
        prefix = os.path.commonprefix(names)
        bare = self._buf.text[1:] if self._buf.text.startswith("/") else ""
        if len(names) == 1:
            # 唯一命中：一次 Tab 补全并加空格（与 commands.complete 同约定）
            self._buf.replace("/" + names[0] + " ")
        elif len(prefix) > len(bare):
            self._buf.replace("/" + prefix)
        self._after_edit()

    # ------------------------------------------------------------ 鼠标点击

    def _click(self, cy: int, cx: int) -> None:
        """SGR 左键（提示符阶段）：提示符行定位光标 / 菜单行选中。"""
        t = self._tio.height
        k = self._menu.rows if self._menu is not None else 0
        w, rows_end, col_end, rows_pos, col_pos = self._layout()
        rows = rows_end + 1
        if self._rows_before_prompt + rows + k > t:   # 已滚动：提示符压屏底
            p_first = t - k - rows + 1
        else:
            p_first = self._rows_before_prompt + 1
        if self._menu is not None and p_first + rows <= cy <= p_first + rows + k - 1:
            idx = cy - (p_first + rows)
            if idx != self._menu.sel:
                self._menu.sel = idx
                self._menu_redraw(k)
            return
        if p_first <= cy <= p_first + rows - 1:
            row_in = cy - p_first
            if row_in == 0:
                col_in = cx - 1 - _disp_width(self._prompt)
            else:
                col_in = row_in * w + (cx - 1)
            self._buf.move_to(_pos_at_display_col(self._buf.text, max(0, col_in)))
            self._redraw_line()

    # ------------------------------------------------------------ 提交

    def _submit(self) -> None:
        line = self._buf.text
        if not self._busy:
            self._w(b"\r\n")
        self._q.put(line)
        self._buf.clear()
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        if self._history is not None:
            self._history.push(line)

    def _confirm_paste(self) -> None:
        """大块粘贴落到换行时的确认：默认拒绝（安全侧）。"""
        n = len(self._buf.text.encode("utf-8", "replace"))
        ans = self._modal_read(
            f"检测到大块粘贴（{n} 字节）。提交？[y/N] ").strip().lower()
        if ans in ("y", "yes"):
            self._submit()
        else:
            self._buf.clear()
            self._w(b"\r\n")

    def _apply_raw(self) -> None:
        """按当前 termios 重建 cbreak-noecho（首次与 resume 复用）。

        只开括号粘贴 ?2004h；鼠标捕获 ?1000h 默认不开——button-event
        捕获常开会吞掉终端原生拖选复制与滚轮（2026-08-16 雅宁实测：
        无法复制输出/无法滑动窗口），需要点击折叠时 /mouse 显式开。
        """
        try:
            tio = termios.tcgetattr(self._fd)
        except termios.error:
            return
        tio[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        tio[6][termios.VMIN] = 0
        tio[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, tio)
        except termios.error:
            pass
        self._w(b"\x1b[?2004h")

    def set_mouse(self, enabled: bool) -> bool:
        """/mouse 切换：开 ?1000h+?1006h（点击折叠行生效），关恢复原生选择。

        开启时终端原生拖选/滚轮失效（SGR 事件接管）；iTerm2 按住 Option
        拖选仍可原生复制。返回新状态。
        """
        self._mouse_on = enabled
        if enabled:
            self._w(b"\x1b[?1000h\x1b[?1006h")
        else:
            self._w(b"\x1b[?1000l\x1b[?1006l")
        return enabled

    # ------------------------------------------------------------ 读线程

    def _run(self) -> None:
        # stdin 非 tty（单测/降级）时 _old_termios=None，裸 select+read 照常工作
        if self._old_termios is not None:
            self._apply_raw()
        try:
            while not self._stop.is_set():
                if self._paused:
                    time.sleep(0.05)
                    continue
                # 模态请求优先（主线程 ask_modal 的委托）
                try:
                    prompt, rq = self._modal_q.get_nowait()
                except queue.Empty:
                    pass
                else:
                    rq.put(self._modal_read(prompt))
                    continue
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
                now = time.monotonic()
                if now - self._last_chunk_at >= 0.2:  # 间隔≥200ms：burst 重新起算
                    self._burst_bytes = 0
                    self._burst_start = now
                self._last_chunk_at = now
                self._burst_bytes += len(chunk)
                if detect_paste(self._burst_bytes, now - self._burst_start):
                    self._burst_pending = True   # 回车时确认（无括号粘贴终端回退）
                for b in chunk:
                    if self._handle(bytes([b])):
                        return  # EOF 路径：直接收线程（finally 还原 termios）
        finally:
            self._w(b"\x1b[?1000l\x1b[?1006l\x1b[?2004l")
            if self._old_termios is not None:
                try:
                    termios.tcsetattr(
                        self._fd, termios.TCSADRAIN, self._old_termios)
                except termios.error:
                    pass

    def _handle(self, b: bytes) -> bool:
        """处理单字节；返回 True 表示终止线程（EOF 路径）。

        CSI 状态机：\\x1b 开头进缓冲；第二字节必须是引入符（[ O P <，< 为
        SGR 鼠标），否则冲刷复位——Esc 当独立键（关菜单），第二字节按普通
        键处理（防吞 Alt+字母 / 孤立 ESC 后的字符）。缓冲 ≤16 字节，
        final∈0x40-0x7E 分派（含 ~ 的 200~/201~），0x20-0x3F 为参数/中间字节。
        括号粘贴区间内：字节全进 _paste_buf，只在 201~ 时一次解码插入。
        """
        # CSI 状态机优先于粘贴区：粘贴结束序列 \x1b[201~ 也是 CSI，必须
        # 先解析（否则区间字节把 [201~ 全吞进 paste_buf，永远不 flush）
        if self._esc is not None:
            self._esc.append(b[0])           # b 是 bytes（_run 逐字节包一层传入）
            if len(self._esc) == 2:
                if b[0] not in b"[OP<":      # 非 CSI 引入符：Esc 键 + 普通键
                    self._esc = None
                    if self._bracket:        # 粘贴内容里的孤立 ESC：当内容存
                        self._paste_buf.extend(b"\x1b" + b)
                        return False
                    self._handle_plain(b"\x1b")
                    return self._handle_plain(b)
                return False
            if 0x40 <= b[0] <= 0x7E:         # CSI final
                seq = bytes(self._esc)
                self._esc = None
                self._dispatch_csi(seq)
                return False
            if len(self._esc) > 16:          # 越界：噪声，丢弃
                self._esc = None
                return False
            if 0x20 <= b[0] < 0x40:          # 参数/中间字节：继续缓冲
                return False
            self._esc = None                 # 其余可打印：冲刷后按普通键处理
            if self._bracket:
                self._paste_buf.extend(b"\x1b" + b)
                return False
            return self._handle_plain(b)
        if self._bracket:
            if b == b"\x1b":                 # 可能是 201~ 结束序列的起点
                self._esc = bytearray(b"\x1b")
                return False
            self._paste_buf.extend(b)
            return False
        if b == b"\x1b":
            self._esc = bytearray(b"\x1b")
            return False
        return self._handle_plain(b)

    def _dispatch_csi(self, seq: bytes) -> None:
        if self._bracket:                     # 粘贴内容里带的转义序列：当内容存
            if seq == b"\x1b[201~":
                self._bracket = False
                text = bytes(self._paste_buf).decode("utf-8", "replace")
                self._paste_buf.clear()
                if len(text.encode("utf-8", "replace")) >= _PASTE_CONFIRM_BYTES:
                    self._insert_text(text)
                    self._confirm_paste()
                else:
                    self._insert_text(text)
            else:
                self._paste_buf.extend(seq)
            return
        if seq == b"\x1b[200~":               # 括号粘贴开始
            self._bracket = True
            self._paste_buf.clear()
        elif seq == b"\x1b[A":                # ↑
            if self._menu is not None:
                self._menu_move(-1)
            elif self._history is not None:
                prev = self._history.up(self._buf.text)
                if prev is not None:
                    self._buf.replace(prev)
                    self._after_edit()
        elif seq == b"\x1b[B":                # ↓
            if self._menu is not None:
                self._menu_move(1)
            elif self._history is not None:
                nxt = self._history.down(self._buf.text)
                if nxt is not None:
                    self._buf.replace(nxt)
                    self._after_edit()
        elif seq == b"\x1b[C":                # →
            self._buf.right()
            self._after_edit()
        elif seq == b"\x1b[D":                # ←
            self._buf.left()
            self._after_edit()
        elif seq in (b"\x1b[H", b"\x1b[1~", b"\x1bOH"):   # Home
            self._buf.home()
            self._after_edit()
        elif seq in (b"\x1b[F", b"\x1b[4~", b"\x1bOF"):   # End
            self._buf.end()
            self._after_edit()
        elif seq == b"\x1b[3~":               # Delete
            self._buf.delete()
            self._after_edit()
        elif seq.startswith(b"\x1b[<") and seq.endswith(b"M"):
            # SGR 鼠标按下：\x1b[<Cb;Cx;CyM
            try:
                parts = seq[3:-1].decode("ascii").split(";")
                cb, cx, cy = int(parts[0]), int(parts[1]), int(parts[2])
            except (ValueError, IndexError):
                return
            if cb == 0:                       # 左键
                if self._busy and self._event_sink is not None:
                    self._event_sink(("click", cy, cx))
                elif not self._busy:
                    self._click(cy, cx)
        # 其余 CSI：忽略，不误吞

    def _handle_plain(self, b: bytes) -> bool:
        if b == b"\x14":  # Ctrl+T 折叠
            if self._busy and self._event_sink is not None:
                self._event_sink(("toggle_thinking",))
            else:
                self._state.dirty = True   # 主循环调 renderer.toggle_thinking 并重印
        elif b == b"\x03":  # Ctrl+C → SIGINT（ISIG 已关，手动还原）
            os.kill(os.getpid(), signal.SIGINT)
        elif b == b"\x1a":  # Ctrl+Z → SIGTSTP
            os.kill(os.getpid(), signal.SIGTSTP)
        elif b in (b"\r", b"\n"):
            if self._menu is not None:
                self._menu_apply()
                self._submit()   # 菜单选中即执行（Tab 只补全不执行）
            elif self._burst_pending:
                self._burst_pending = False
                self._confirm_paste()
            else:
                self._submit()
        elif b in (b"\x7f", b"\x08"):  # 退格
            self._buf.backspace()
            self._after_edit()
        elif b == b"\x04" and not self._buf.text:  # ^D 空行 = EOF
            self._q.put(self.EOF)
            return True
        elif b == b"\x0b":  # Ctrl+K 删到行尾
            self._buf.kill_to_end()
            self._after_edit()
        elif b == b"\x01":  # Ctrl+A 行首
            self._buf.home()
            self._after_edit()
        elif b == b"\x05":  # Ctrl+E 行尾
            self._buf.end()
            self._after_edit()
        elif b == b"\t":
            if self._menu is not None:
                self._menu_tab()
            elif self._completer is not None:
                d = self._buf.text
                if d.startswith("/"):
                    filled = self._completer(d)
                    if filled and filled != d:
                        self._buf.replace(filled)
                        self._after_edit()
                        return False
                self._buf.insert("\t")
                self._after_edit()
        elif b == b"\x1b":  # Esc 键（经冲刷路径）：关菜单
            if self._menu is not None:
                self._menu_close()
        elif b >= b" ":  # 可见字符
            text = self._dec.decode(b)
            for ch in text:
                self._buf.insert(ch)
            self._after_edit()
        return False

    def _modal_read(self, prompt: str) -> str:
        """读线程内模态取行：独占输入，返回一行（Ctrl+C/EOF → 空串）。"""
        self._w(prompt.encode("utf-8", "replace"))
        raw = bytearray()
        chars: list[tuple[str, int]] = []
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 64)
            except OSError:
                continue
            if not chunk:
                return ""
            for raw_b in chunk:
                b = bytes([raw_b])   # 与 _handle 同约定：字节比较用 bytes
                if b in (b"\r", b"\n"):
                    self._w(b"\r\n")
                    return raw.decode("utf-8", "replace")
                if b == b"\x03":  # Ctrl+C：取消模态
                    self._w(b"^C\r\n")
                    return ""
                if b in (b"\x7f", b"\x08"):
                    if chars:
                        ch, n = chars.pop()
                        w = _char_width(ch)
                        self._w(b"\b" * w + b" " * w + b"\b" * w)
                        del raw[-n:]
                    continue
                if b >= b" " or b == b"\t":
                    for ch in dec.decode(b):
                        chars.append((ch, len(ch.encode("utf-8"))))
                        raw.extend(ch.encode("utf-8"))
                        self._w(ch.encode("utf-8"))
