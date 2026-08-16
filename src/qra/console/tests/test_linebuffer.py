"""LineBuffer 纯行编辑单测（D011 v2：←→ 光标移动 P0）。

与终端解耦的纯 Python 类——不经 pty，直接覆盖。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console.linebuffer import LineBuffer  # noqa: E402


def _buf(text: str = "", pos: int | None = None) -> LineBuffer:
    b = LineBuffer(text)
    if pos is not None:
        b.move_to(pos)
    return b


class LineBufferTests(unittest.TestCase):
    def test_insert_at_cursor(self):
        b = _buf("ac", 1)
        b.insert("b")
        self.assertEqual(b.text, "abc")
        self.assertEqual(b.pos, 2)

    def test_insert_cjk_one_char(self):
        b = LineBuffer()
        b.insert("中")
        b.insert("文")
        self.assertEqual(b.text, "中文")
        self.assertEqual(b.pos, 2)   # 按字符记账，无字节地雷

    def test_left_right_boundaries(self):
        b = _buf("ab", 1)
        b.left()
        self.assertEqual(b.pos, 0)
        b.left()                     # 边界外不动
        self.assertEqual(b.pos, 0)
        b.right(); b.right(); b.right()
        self.assertEqual(b.pos, 2)

    def test_backspace_before_cursor(self):
        b = _buf("abc", 2)
        self.assertEqual(b.backspace(), "b")
        self.assertEqual(b.text, "ac")
        self.assertEqual(b.pos, 1)
        b.backspace(); b.backspace()   # 到底后返回 None 不动
        self.assertEqual(b.text, "c")

    def test_delete_at_cursor(self):
        b = _buf("abc", 1)
        self.assertEqual(b.delete(), "b")
        self.assertEqual(b.text, "ac")
        self.assertEqual(b.pos, 1)
        self.assertEqual(b.delete(), "c")   # 光标还在 pos 1，再删一个
        self.assertEqual(b.text, "a")
        self.assertIsNone(b.delete())       # 行尾删除返回 None 不动
        self.assertEqual(b.text, "a")

    def test_kill_to_end(self):
        b = _buf("abc", 1)
        self.assertEqual(b.kill_to_end(), "bc")
        self.assertEqual(b.text, "a")

    def test_home_end_move_to(self):
        b = _buf("abc", 1)
        b.end()
        self.assertEqual(b.pos, 3)
        b.home()
        self.assertEqual(b.pos, 0)
        b.move_to(2)
        self.assertEqual(b.pos, 2)
        b.move_to(99)                 # 越界钳到行尾
        self.assertEqual(b.pos, 3)

    def test_replace_clear(self):
        b = _buf("abc", 1)
        b.replace("xyz")
        self.assertEqual(b.text, "xyz")
        self.assertEqual(b.pos, 3)    # 整行替换光标到行尾
        b.clear()
        self.assertEqual(b.text, "")
        self.assertEqual(b.pos, 0)


if __name__ == "__main__":
    unittest.main()
