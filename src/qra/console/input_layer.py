"""输入层（会话级）：cbreak-noecho + 读线程 + 行编辑 + 历史/补全/粘贴保护。

从 main.py 迁出并扩展。prime 机理落地：单一持久读线程 + 自有行编辑，
不依赖 input()/readline。此前"每回合起停 key thread"的架构有交还竞态——
key thread 读走字节后主循环已越过缓冲检查，input() 永远等不到（pty 实证
挂死）。会话级输入层没有 raw/cooked 转换窗口：回合中敲入直接进行缓冲，
回合后主循环从队列取行。

P0 扩展：
- ↑↓ 历史（ConsoleHistory 导航，整行替换 + \r 重绘）
- Tab 补全（draft 以 / 开头 → completer 回调）
- 大块粘贴保护（单 chunk ≥4096 字节且 <200ms → 提交前模态确认）
- ask_modal：主线程的模态问答委托给读线程（不抢 stdin）
- pause/resume：/memory 起编辑器前还原 termios
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


def _char_width(c: str) -> int:
    """终端显示列宽（CJK 全角占 2 列），退格回显用。"""
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def detect_paste(n_bytes: int, span_ms: float,
                 threshold_bytes: int = 4096,
                 threshold_ms: float = 200.0) -> bool:
    """大块粘贴判据（纯函数，单测锁定）：单 chunk 字节数 ≥ 阈值且距上一
    chunk 不足阈值毫秒 → True。人手打字不可能 200ms 内灌入 4KB。"""
    return n_bytes >= threshold_bytes and span_ms < threshold_ms


class InputLayer:
    """交互会话持有一个实例：cbreak-noecho + 读线程 + 行队列。

    行为：
    - Ctrl+T 折叠/展开思考（写 TurnState.dirty 触发渲染重画）
    - Ctrl+C 还原 SIGINT（cbreak 下 ISIG 已关）；Ctrl+Z → SIGTSTP
    - 回车提交整行进队列；退格删一个字符（按显示列宽回显）；^D 空行 = EOF
    - ↑↓ 历史导航；Tab 补全命令名；大块粘贴先确认
    - 回显直达 /dev/tty——回合中 stdout 被重定向进 StringIO，绕开它才可见
    """

    EOF = object()

    def __init__(self, state, history=None, completer=None,
                 prompt: str = "❯ ") -> None:
        self._q: queue.Queue = queue.Queue()
        self._modal_q: queue.Queue = queue.Queue()   # (prompt, result_q)
        self._state = state
        self._history = history      # ConsoleHistory | None
        self._completer = completer  # callable(draft) -> str | None
        self._prompt = prompt        # 纯文本提示（历史替换 \r 重绘用）
        self._fd = sys.stdin.fileno()
        self._tty_out = None
        try:
            self._tty_out = os.open("/dev/tty", os.O_WRONLY)
        except OSError:
            self._tty_out = None
        self._stop = threading.Event()
        self._paused = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="qra-console-input")
        # 行编辑状态：raw 字节（提交用）+ 字符列表（退格按字符删）
        self._raw = bytearray()
        self._chars: list[tuple[str, int]] = []   # (字符, 字节数)
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # CSI 状态机 + 粘贴模式 + 读节奏
        self._esc: bytearray | None = None
        self._paste_mode = False
        self._last_chunk_at = time.monotonic()
        # 粘贴 burst 统计：os.read 每次最多 64 字节，单 chunk 永远够不到
        # 4096 阈值——按 burst（≥200ms 间隔重新起算）累计喂给 detect_paste
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
        return "".join(c for c, _ in self._chars)

    def redraw(self) -> None:
        """回合结束后把已有草稿补回显到新提示符后（清理回合中乱插的回显）。"""
        if self._chars:
            self._echo(self.draft().encode("utf-8", "replace"))

    def set_prompt(self, prompt: str) -> None:
        self._prompt = prompt

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
        """/memory 起编辑器前调用：读线程挂起 + termios 还原（编辑器接管终端）。"""
        self._paused = True
        if self._old_termios is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
            except termios.error:
                pass

    def resume(self) -> None:
        """编辑器退出后调用：重建 cbreak 并恢复读线程。"""
        self._apply_raw()
        self._paused = False

    # ------------------------------------------------------------ 回显/编辑

    def _echo(self, data: bytes) -> None:
        if self._tty_out is not None:
            try:
                os.write(self._tty_out, data)
            except OSError:
                pass

    def _reset_dec(self) -> None:
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _clear_draft(self) -> None:
        self._raw.clear()
        self._chars.clear()
        self._reset_dec()   # 行边界：丢弃跨行残留的半个 UTF-8 序列

    def _submit(self) -> None:
        line = self._raw.decode("utf-8", "replace")
        self._echo(b"\r\n")
        self._q.put(line)
        self._clear_draft()
        if self._history is not None:
            self._history.push(line)

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

    def _replace_draft(self, new: str) -> None:
        """整行替换（↑↓ 历史 / Tab 补全）：\r 回到行首 + 重印提示 + 新草稿 +
        \x1b[K 清到行尾（旧草稿比新草稿长也不留残字）。"""
        self._raw = bytearray(new.encode("utf-8"))
        self._chars = [(c, len(c.encode("utf-8"))) for c in new]
        self._reset_dec()
        out = (b"\r" + (self._prompt or "").encode("utf-8")
               + new.encode("utf-8") + b"\x1b[K")
        self._echo(out)

    def _apply_raw(self) -> None:
        """按当前 termios 重建 cbreak-noecho（首次与 resume 复用）。"""
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
                    self._paste_mode = True
                for b in chunk:
                    if self._handle(bytes([b])):
                        return  # EOF 路径：直接收线程（finally 还原 termios）
        finally:
            if self._old_termios is not None:
                try:
                    termios.tcsetattr(
                        self._fd, termios.TCSADRAIN, self._old_termios)
                except termios.error:
                    pass

    def _handle(self, b: bytes) -> bool:
        """处理单字节；返回 True 表示终止线程（EOF 路径）。

        CSI 状态机：\x1b 开头进缓冲；第二字节必须是引入符（[ O P），否则
        冲刷复位转普通键（防吞 Alt+字母 / 孤立 ESC 后的字符——final 字节
        0x40-0x7E 与可打印字母重叠，不卡引入符会把 ↑ 拆成 "吞 \x1b[ + 打 A"）。
        缓冲 ≤8 字节，final∈0x40-0x7E 分派，0x20-0x3F 为参数/中间字节。
        """
        if self._esc is not None:
            self._esc.append(b[0])   # b 是 bytes（_run 逐字节包一层传入），取整数值
            if len(self._esc) == 2:
                if b[0] not in b"[OP":  # 非 CSI 引入符：Alt+键 / 孤立 ESC
                    self._esc = None
                    return self._handle_plain(b)
                return False
            if 0x40 <= b[0] <= 0x7E:  # CSI final
                seq = bytes(self._esc)
                self._esc = None
                self._dispatch_csi(seq)
                return False
            if len(self._esc) > 8:    # 越界：噪声，丢弃
                self._esc = None
                return False
            if 0x20 <= b[0] < 0x40:   # 参数/中间字节：继续缓冲
                return False
            self._esc = None          # 其余可打印：冲刷后按普通键处理
            return self._handle_plain(b)
        if b == b"\x1b":
            self._esc = bytearray(b"\x1b")
            return False
        return self._handle_plain(b)

    def _dispatch_csi(self, seq: bytes) -> None:
        if seq == b"\x1b[A":  # ↑
            if self._history is not None:
                prev = self._history.up(self.draft())
                if prev is not None:
                    self._replace_draft(prev)
        elif seq == b"\x1b[B":  # ↓
            if self._history is not None:
                nxt = self._history.down(self.draft())
                if nxt is not None:
                    self._replace_draft(nxt)
        # \x1b[C/\x1b[D（←→光标）与其余 CSI：P1 再实现，忽略不误吞

    def _handle_plain(self, b: bytes) -> bool:
        if b == b"\x14":  # Ctrl+T 折叠
            self._state.show_thinking = not self._state.show_thinking
            self._state.dirty = True
        elif b == b"\x03":  # Ctrl+C → SIGINT（ISIG 已关，手动还原）
            os.kill(os.getpid(), signal.SIGINT)
        elif b == b"\x1a":  # Ctrl+Z → SIGTSTP
            os.kill(os.getpid(), signal.SIGTSTP)
        elif b in (b"\r", b"\n"):
            if self._paste_mode:
                self._paste_mode = False
                self._confirm_paste()
            else:
                self._submit()
        elif b in (b"\x7f", b"\x08"):  # 退格
            self._backspace()
        elif b == b"\x04" and not self._chars:  # ^D 空行 = EOF
            self._q.put(self.EOF)
            return True
        elif b == b"\t":
            if not self._paste_mode and self._completer is not None:
                d = self.draft()
                if d.startswith("/"):
                    filled = self._completer(d)
                    if filled and filled != d:
                        self._replace_draft(filled)
                        return False
            self._push_char("\t")
            self._echo(b"\t")
        elif b >= b" ":  # 可见字符
            for ch in self._dec.decode(b):
                self._push_char(ch)
                self._echo(ch.encode("utf-8"))
        return False

    def _confirm_paste(self) -> None:
        """大块粘贴落到换行时的确认：默认拒绝（安全侧）。"""
        n = len(self._raw)
        ans = self._modal_read(
            f"检测到大块粘贴（{n} 字节）。提交？[y/N] ").strip().lower()
        if ans in ("y", "yes"):
            self._submit()
        else:
            self._clear_draft()
            self._echo(b"\r\n")

    def _modal_read(self, prompt: str) -> str:
        """读线程内模态取行：独占输入，返回一行（Ctrl+C/EOF → 空串）。"""
        self._echo(prompt.encode("utf-8", "replace"))
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
                    self._echo(b"\r\n")
                    return raw.decode("utf-8", "replace")
                if b == b"\x03":  # Ctrl+C：取消模态
                    self._echo(b"^C\r\n")
                    return ""
                if b in (b"\x7f", b"\x08"):
                    if chars:
                        ch, n = chars.pop()
                        w = _char_width(ch)
                        self._echo(b"\b" * w + b" " * w + b"\b" * w)
                        del raw[-n:]
                    continue
                if b >= b" " or b == b"\t":
                    for ch in dec.decode(b):
                        chars.append((ch, len(ch.encode("utf-8"))))
                        raw.extend(ch.encode("utf-8"))
                        self._echo(ch.encode("utf-8"))
