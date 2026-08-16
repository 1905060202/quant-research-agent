"""TermIO 审计修复单测（2026-08-17 审计 F-01/F-03/F-06/F-07）：

- sanitize_display：C0/C1/ESC 剥除、\\t 展开、keep_newlines 语义
- write 净化：帧区文本进裸 os.write 前统一过滤（转义注入面）
- _advance 折行感知：流式 end="" 跨 print 续写的光标模型与
  _Screen 模拟终端差分一致（模型 == 终端，F-03）
"""
from __future__ import annotations

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console.termio import TermIO, sanitize_display  # noqa: E402
from test_frame import _Screen  # noqa: E402  (discover 同目录)
from rich.cells import cell_len  # noqa: E402


class SanitizeTests(unittest.TestCase):
    def test_strips_c0_and_esc(self):
        s = sanitize_display("a\x1b[2Jb\x07c\x0cd\x00e\x7ff")
        # ESC 被剥离后 [2J 只是可打印文本——注入面消失，不是整序列消失
        self.assertEqual(s, "a[2Jbcdef")

    def test_strips_c1(self):
        self.assertEqual(sanitize_display("x\x9by"), "xy")

    def test_tab_expands_to_four_spaces(self):
        self.assertEqual(sanitize_display("a\tb"), "a    b")

    def test_display_newlines_become_spaces(self):
        self.assertEqual(sanitize_display("a\r\nb"), "a  b")
        self.assertEqual(sanitize_display("a\nb"), "a b")

    def test_draft_newlines_normalized(self):
        self.assertEqual(sanitize_display("a\r\nb", keep_newlines=True), "a\nb")
        self.assertEqual(sanitize_display("a\rb", keep_newlines=True), "a\nb")
        self.assertEqual(sanitize_display("a\nb", keep_newlines=True), "a\nb")

    def test_printable_preserved(self):
        self.assertEqual(sanitize_display("中文 ❤️"), "中文 ❤️")


class WriteSanitizeTests(unittest.TestCase):
    def _tio(self) -> TermIO:
        buf = io.StringIO()
        return buf, TermIO(file=buf, width=80, height=24)

    def test_write_strips_control_from_frame_text(self):
        buf, tio = self._tio()
        tio.write("a\x1b[31mRED")
        self.assertNotIn("\x1b", buf.getvalue())   # 转义不发射
        self.assertEqual(buf.getvalue(), "a[31mRED")
        self.assertEqual(tio.cursor_pos, (1, 1 + cell_len("a[31mRED")))

    def test_write_expands_tab_before_width(self):
        buf, tio = self._tio()
        tio.write("a\tb")
        self.assertEqual(buf.getvalue(), "a    b")
        self.assertEqual(tio.cursor_pos, (1, 7))   # \t 宽 0 → 4 空格宽 4


class AdvanceTests(unittest.TestCase):
    """光标模型差分测试：模型坐标必须 == _Screen 模拟终端坐标。

    _Screen 复刻 xterm pending-wrap 语义（打满末列后下一字符折行），
    审计 F-03 的「流式续写不数折行」在此类用例下原实现必挂。
    """

    def _tio(self, width: int = 80, height: int = 24):
        screen = _Screen(width, height)
        tio = TermIO(file=screen, width=width, height=height)
        return screen, tio

    def test_continuation_wrap_across_prints(self):
        """80 列：50 字符 + 40 字符两段 end="" 续写 → 折行到 (2, 11)。

        旧实现 50+40=90 还在「第一行」；真实终端 30 列后自动折行。
        """
        screen, tio = self._tio()
        tio.print("x" * 50, end="")
        self.assertEqual(tio.cursor_pos, (1, 51))
        tio.print("y" * 40, end="")
        self.assertEqual(tio.cursor_pos, (2, 11))
        self.assertEqual((screen.r, screen.c), (2, 11))   # 模型 == 终端

    def test_exact_fill_pending_wrap(self):
        """恰好写满行宽：光标停末列（pending-wrap），下一字符先折再写。"""
        screen, tio = self._tio()
        tio.print("x" * 80, end="")
        self.assertEqual(tio.cursor_pos, (1, 81))   # w+1 = pending-wrap 约定
        tio.print("z", end="")
        self.assertEqual(tio.cursor_pos, (2, 2))
        self.assertEqual((screen.r, screen.c), (2, 2))

    def test_explicit_newline_advance(self):
        screen, tio = self._tio()
        tio.print("ab\ncd")            # print 默认 end="\n"：ab\ncd\n
        self.assertEqual(tio.cursor_pos, (3, 1))
        self.assertEqual((screen.r, screen.c), (3, 1))

    def test_segment_longer_than_width(self):
        """单段 100 字符（软折行流含 \\n）：折行与显式换行都计数。"""
        screen, tio = self._tio()
        tio.print("x" * 100, end="")
        # rich 软折行流：80 字符 \\n 20 字符；终端从 col 1 写满折行
        self.assertEqual(tio.cursor_pos, (2, 21))
        self.assertEqual((screen.r, screen.c), (2, 21))

    def test_region_bottom_clamp(self):
        screen, tio = self._tio()
        tio.set_region(3)
        tio.print("\n\n\n\n")
        self.assertEqual(tio.cursor_pos, (3, 1))
        self.assertEqual((screen.r, screen.c), (3, 1))

    def test_width_one_terminal(self):
        """1 列终端：每字符一行，模型与终端一致（F-07 假下限删除后可用）。

        _Screen 的 pending-wrap 状态（末列待折）对应模型 _c = w+1 约定。
        """
        screen, tio = self._tio(width=1, height=24)
        tio.print("ab", end="")
        self.assertEqual(tio.cursor_pos, (2, 2))   # 每字一行的 pending-wrap
        self.assertEqual((screen.r, screen.c), (2, 1))
        self.assertTrue(screen._pending)           # 终端停在末列待折


if __name__ == "__main__":
    unittest.main()
