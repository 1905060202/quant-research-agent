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
import shutil
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from rich.cells import cell_len
from rich.console import Console


def _term_size() -> tuple[int, int]:
    try:
        sz = shutil.get_terminal_size(fallback=(80, 24))
        return max(20, sz.columns), max(10, sz.lines)
    except (OSError, ValueError):
        return 80, 24


class TermIO:
    """串行终端输出。构造时刻绑定真实 stdout 的文件对象（回合内
    redirect_stdout 不影响它——main.run_turn 同款约定）。"""

    def __init__(self, file: Any | None = None,
                 width: int | None = None, height: int | None = None) -> None:
        self._lock = threading.RLock()
        self.file = file if file is not None else sys.stdout
        # 尺寸注入（测试/嵌入场景）：None = 每次现取真实终端
        # （_term_size 有 ≥20 钳制，窄屏测试无法走 env，须显式注入）
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
        只用于 Frame 区域（行宽内截断好的文本），不做 wrap 处理。"""
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
        """按渲染缓冲推进光标模型；区域底部换行触发滚动 → 钳制。"""
        if not buf:
            return
        segs = buf.split("\n")
        if buf.endswith("\n"):
            self._r += len(segs) - 1
            self._c = 1
        else:
            self._r += len(segs) - 1
            self._c += cell_len(segs[-1])
        if self._region_bottom is not None and self._r > self._region_bottom:
            # 区域底部写满换行 → 区域滚动，光标留底
            self._r = self._region_bottom
            self._c = 1
