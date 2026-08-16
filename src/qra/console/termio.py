"""TermIO：终端单一写入者 + 光标模型（D011 v4）。

崩溃根因（2026-08-16 实测 + 代码审计）：
- rich Live 渲染线程与 InputLayer 回显是两个**不同步**的 tty 写入者：
  输入回显字节可能插进 Live 转义序列中间 → 终端模拟器的 CSI 解析状态机
  卡死 → 花屏/假死（「输出时打字终端崩了」）。
- Live 全帧重绘在长 CoT 下向终端灌入巨量 `\\x1b[1A` 重印序列（内容超屏时
  光标上移越界）→ 「大量重复 + 屏幕不跟随下移」。

修复：全部终端输出（渲染帧 / spinner / 输入回显 / 菜单 / 提示符）经 TermIO
一把锁串行写出；渲染本身改为追加式（renderer.py）。本模块同时负责
wrap 感知的行数测量（折叠行账本用）。

v4 新增（固定输入框帧，D011 v4）：
- **光标追踪**：TermIO 维护屏幕光标 (row, col) 模型——每次写出（print/write
  原语）都按 wrap 感知的方式推进坐标，转义原语显式更新。任意线程（渲染
  线程/输入线程/shell 线程）经锁串行后坐标模型始终一致——「两个不同步
  写入者」从机制上根除，busy 中输入实时回显因此安全。
- **DECSTBM 滚动区域**：`set_region` 把内容滚动锁在 [1..R] 行内，R 之下
  是 Frame 固定输入框（提示符/菜单/活动条/面板），永不滚动。
- **绝对定位原语**：move/move_up/move_down/move_col/cr/erase_line 全部
  追踪。约定：**移动光标的字节只能走原语**；write_bytes 只用于 SGR 样式
  码等不移动光标的字节（违反约定 = 光标模型漂移）。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from rich.cells import cell_len
from rich.console import Console

# 显示净化：剥「会动终端」的控制字节。保留 \t（展空格）、\r/\n（按用途
# 转换），其余 C0（ESC/响铃/退格/清屏/DEL）、C1 全部剥除——cell_len 对它们
# 宽 0 而终端会执行（ESC 引 CSI、\x0c 清屏、\x07 响铃、\x1b[2J 一键清屏）。
# 2026-08-17 审计 F-01/F-06：粘贴带 ANSI 色的日志/教程、! ls --color 输出
# 原样进帧区 = 转义注入（帧区 tio.write 是裸 os.write，无 rich 转义保护）。
_C0_STRIP_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize_display(text: str, keep_newlines: bool = False) -> str:
    """终端显示净化（帧区写入与草稿插入共用）。

    keep_newlines=False（显示路径）：剥 C0/C1，\\t→4 空格，\\r/\\n→空格——
    帧行文本写出去不许触发回车/换行。keep_newlines=True（草稿路径）：
    \\r(\\n)→\\n（多行草稿合法），其余同。
    """
    text = _C0_STRIP_RE.sub("", text)
    text = text.replace("\t", "    ")
    if keep_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = text.replace("\r", " ").replace("\n", " ")
    return text


def _term_size() -> tuple[int, int]:
    try:
        sz = shutil.get_terminal_size(fallback=(80, 24))
        # 只挡 0/负（假终端），不设 ≥20 假下限：真实窄终端（iTerm2 分屏
        # <20 列）按假宽度排版会整体错位（2026-08-17 审计 F-07）
        return max(1, sz.columns), max(1, sz.lines)
    except (OSError, ValueError):
        return 80, 24


class TermIO:
    """串行终端输出。构造时刻绑定真实 stdout 的文件对象（回合内
    redirect_stdout 不影响它——main.run_turn 同款约定）。"""

    def __init__(self, file: Any | None = None,
                 width: int | None = None, height: int | None = None) -> None:
        self._lock = threading.RLock()
        self.file = file if file is not None else sys.stdout
        # 尺寸注入（测试/嵌入场景）：None = 每次现取真实终端；窄屏测试
        # 显式注入（env 路由不可靠）
        self._w, self._h = width, height
        try:
            self._fd = self.file.fileno()
        except (OSError, AttributeError, ValueError, io.UnsupportedOperation):
            self._fd = None
        # 光标模型（屏幕坐标，1 基）。初始化时假设左上角。
        self._r = 1
        self._c = 1
        self._region_bottom: int | None = None  # DECSTBM 下界；None=全屏

    # ------------------------------------------------------------ 尺寸

    @property
    def width(self) -> int:
        return self._w if self._w is not None else _term_size()[0]

    @property
    def height(self) -> int:
        return self._h if self._h is not None else _term_size()[1]

    # ------------------------------------------------------------ 光标模型

    @property
    def cursor_pos(self) -> tuple[int, int]:
        """屏幕光标坐标 (row, col)，1 基。"""
        return self._r, self._c

    @property
    def region_bottom(self) -> int | None:
        """DECSTBM 下界行号；None=全屏（无滚动区域）。"""
        return self._region_bottom

    @property
    def is_tty(self) -> bool:
        """真实终端？StringIO 测试路径 / 管道重定向下为 False。"""
        try:
            return self.file.isatty()
        except (AttributeError, ValueError):
            return False

    # ------------------------------------------------------------ 写出

    def write_bytes(self, b: bytes) -> None:
        """原始字节。约定：只用于 SGR 样式码等**不移动光标**的字节；
        光标移动一律走原语（否则光标模型漂移）。"""
        with self._lock:
            self._emit(b)

    def _emit(self, b: bytes) -> None:
        if self._fd is not None:
            try:
                os.write(self._fd, b)
                return
            except OSError:
                pass
        try:
            self.file.write(b.decode("utf-8", "replace"))
            self.file.flush()
        except (OSError, ValueError, AttributeError):
            pass

    def print(self, obj: Any = "", *, end: str = "\n", style: str | None = None,
              markup: bool = False, soft_wrap: bool = True) -> int:
        """打印一行/一个 renderable；返回占屏行数（wrap 感知）。

        width 每次现取（窗口 resize 后不漂移）；force_terminal=True 保证
        样式码输出到真实终端（file 为 StringIO 的测试路径下同样一致）。
        打印后按同宽度渲染缓冲推进光标模型（滚动区域底部自动钳制）。
        """
        with self._lock:
            w = self.width
            c = Console(file=self.file, width=w, force_terminal=True,
                        legacy_windows=False)
            c.print(obj, end=end, style=style, markup=markup,
                    soft_wrap=soft_wrap, no_wrap=False)
            try:
                self.file.flush()
            except (OSError, ValueError, AttributeError):
                pass
            self._advance(self._measure_buf(obj, end=end))
        return self.measure_rows(obj, end=end)

    def write(self, text: str) -> None:
        """帧区文本原语：在光标处写一段短文本（不换行），推进列坐标。
        只用于 Frame 区域（行宽内截断好的文本），不做 wrap 处理。

        写入前统一净化（2026-08-17 审计 F-01/F-06）：帧区是裸 os.write，
        rich 的转义保护覆盖不到——ESC/C0 若随面板/菜单/草稿文本进来，
        会被原样发射（\\x1b[2J 清屏、SGR 泄漏、\\r 跳行覆写）。净化后
        再算宽度：\\t→空格等换算进 cell_len，模型与终端一致。
        """
        text = sanitize_display(text)
        with self._lock:
            self._emit(text.encode("utf-8", "replace"))
            self._c += cell_len(text)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """整序列原子互斥：多原语绘制（帧四带 / 区域重印）包一层再画。

        单原语各自持锁只保证单次调用不被穿插；帧绘制是「move → erase →
        write → move 复位」的多步序列，必须整体互斥，否则渲染线程的流式
        print 可能插进序列中间（erase 打到错行、内容与帧互踩）。RLock
        可重入：序列内部的原语/print 调用不受影响。
        """
        with self._lock:
            yield

    # ------------------------------------------------------------ 原语

    def set_region(self, bottom: int | None) -> None:
        """设置/清除 DECSTBM 滚动区域下界。收缩时若光标落在区域外，
        钳制到区域底部（流式内容从此处继续）。"""
        with self._lock:
            if bottom is None:
                self._emit(b"\x1b[r")
            else:
                bottom = max(1, int(bottom))
                self._emit(f"\x1b[1;{bottom}r".encode())
            self._region_bottom = bottom
            if bottom is not None and self._r > bottom:
                self._r, self._c = bottom, 1

    def move(self, row: int, col: int = 1) -> None:
        """绝对定位。row 允许超出区域下界（Frame 区）。"""
        with self._lock:
            self._emit(f"\x1b[{max(1, int(row))};{max(1, int(col))}H".encode())
            self._r, self._c = max(1, int(row)), max(1, int(col))

    def move_up(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._emit(f"\x1b[{n}A".encode())
            self._r = max(1, self._r - n)

    def move_down(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._emit(f"\x1b[{n}B".encode())
            self._r = self._r + n

    def move_col(self, col: int) -> None:
        with self._lock:
            self._emit(f"\x1b[{max(1, int(col))}G".encode())
            self._c = max(1, int(col))

    def cr(self) -> None:
        with self._lock:
            self._emit(b"\r")
            self._c = 1

    def erase_line(self) -> None:
        """擦除当前行（\x1b[2K），光标不动。"""
        with self._lock:
            self._emit(b"\x1b[2K")

    def erase_below(self) -> None:
        """从光标擦到屏底（\x1b[J），光标不动。"""
        with self._lock:
            self._emit(b"\x1b[J")

    # ------------------------------------------------------------ 内部

    def _measure_buf(self, obj: Any, end: str = "\n") -> str:
        """同宽度渲染到 StringIO 的纯文本缓冲（无样式码），供测量与追踪。"""
        buf = io.StringIO()
        c = Console(file=buf, width=self.width, force_terminal=False,
                    legacy_windows=False)
        c.print(obj, end=end, markup=False, soft_wrap=True, no_wrap=False)
        return buf.getvalue()

    def measure_rows(self, obj: Any, end: str = "\n") -> int:
        """wrap 感知行数测量：同宽度下渲染到 StringIO 数换行（纯文本、无样式码）。"""
        s = self._measure_buf(obj, end=end)
        n = s.count("\n")
        return max(1, n) if s.strip("\n") else n

    def _advance(self, buf: str) -> None:
        """按渲染缓冲推进光标模型：显式换行 + 终端自动折行感知。

        buf 是 col 1 起算的渲染流（_measure_buf 产物，软折行按 col 1
        边界插 \\n）。真实终端从当前光标写起：第一行只到宽度边界就自动
        折行——流式「end=\"\" 两段续写」时旧实现直接把列相加（50+40=90）
        → 模型认为还在第一行，真实终端早已折行 → 坐标永久分叉且
        _sync_cursor 因「模型==错位坐标」不自愈（2026-08-17 审计 F-03）。

        约定：恰好写满行宽时 _c = w+1（末列 pending-wrap，下一字符触发
        折行）；跨段续写从实际列重放折行。
        """
        if not buf:
            return
        w = self.width
        for i, seg in enumerate(buf.split("\n")):
            if i > 0:                     # 显式 \\n：换行即消 pending-wrap
                self._r += 1
                self._c = 1
            L = cell_len(seg)
            if not L:
                continue
            if self._c > w:               # 上段恰好写满：先折再写
                self._r += 1
                self._c = 1
            rem = w - self._c + 1         # 本行剩余列数
            if L > rem:
                rest = L - rem
                rows, extra = divmod(rest, w)
                if extra:
                    self._r += rows + 1
                    self._c = extra + 1
                else:
                    self._r += rows
                    self._c = w + 1       # 恰好写满末行
            elif self._c + L - 1 == w:
                self._c = w + 1           # 写满本行：pending-wrap
            else:
                self._c += L
        if self._region_bottom is not None and self._r > self._region_bottom:
            # 区域底部写满换行 → 区域滚动，光标留底
            self._r = self._region_bottom
            self._c = 1
