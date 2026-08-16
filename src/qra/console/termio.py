"""TermIO：终端单一写入者（D011）。

崩溃根因（2026-08-16 实测 + 代码审计）：
- rich Live 渲染线程与 InputLayer 回显是两个**不同步**的 tty 写入者：
  输入回显字节可能插进 Live 转义序列中间 → 终端模拟器的 CSI 解析状态机
  卡死 → 花屏/假死（「输出时打字终端崩了」）。
- Live 全帧重绘在长 CoT 下向终端灌入巨量 `\\x1b[1A` 重印序列（内容超屏时
  光标上移越界）→ 「大量重复 + 屏幕不跟随下移」。

修复：全部终端输出（渲染帧 / spinner / 输入回显 / 菜单 / 提示符）经 TermIO
一把锁串行写出；渲染本身改为追加式（renderer.py）。本模块同时负责
wrap 感知的行数测量（折叠行账本用）。
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import threading
from typing import Any

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

    def __init__(self, file: Any | None = None) -> None:
        self._lock = threading.RLock()
        self.file = file if file is not None else sys.stdout
        try:
            self._fd = self.file.fileno()
        except (OSError, AttributeError, ValueError, io.UnsupportedOperation):
            self._fd = None

    # ------------------------------------------------------------ 尺寸

    @property
    def width(self) -> int:
        return _term_size()[0]

    @property
    def height(self) -> int:
        return _term_size()[1]

    # ------------------------------------------------------------ 写出

    def write_bytes(self, b: bytes) -> None:
        """原始字节（ANSI 控制序列走这里）。锁内串行，不打断任何转义序列。"""
        with self._lock:
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
        return self.measure_rows(obj, end=end)

    def measure_rows(self, obj: Any, end: str = "\n") -> int:
        """wrap 感知行数测量：同宽度下渲染到 StringIO 数换行（纯文本、无样式码）。"""
        buf = io.StringIO()
        c = Console(file=buf, width=self.width, force_terminal=False,
                    legacy_windows=False)
        c.print(obj, end=end, markup=False, soft_wrap=True, no_wrap=False)
        s = buf.getvalue()
        n = s.count("\n")
        return max(1, n) if s.strip("\n") else n
