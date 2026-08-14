"""qra_console 核心单元测试（vendor 同步回归门禁的一部分）。

覆盖 TurnState 折叠状态机与 InputLayer 行编辑核心路径。
运行：.venv-v7/bin/python -m unittest discover -s src/qra/console/tests -v
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console.main import InputLayer, TurnState, _char_width  # noqa: E402


class TurnStateTests(unittest.TestCase):
    def test_show_thinking_inverts_fold(self):
        self.assertFalse(TurnState(True).show_thinking)   # 折叠开 → 隐藏
        self.assertTrue(TurnState(False).show_thinking)   # 折叠关 → 展示

    def test_ctrl_t_toggle(self):
        t = TurnState(True)
        t.show_thinking = True
        self.assertTrue(t.show_thinking)


class CharWidthTests(unittest.TestCase):
    def test_cjk_width_two(self):
        self.assertEqual(_char_width("中"), 2)

    def test_ascii_width_one(self):
        self.assertEqual(_char_width("a"), 1)


class InputLayerTests(unittest.TestCase):
    """管道模拟 stdin（非 tty → InputLayer 自动降级裸 select+read）。"""

    def setUp(self):
        self.r, self.w = os.pipe()
        self._fake = os.fdopen(self.r, "rb")
        self._old_stdin = sys.stdin
        self._layers = []
        sys.stdin = self._fake

    def tearDown(self):
        # 先停输入线程再关 fd：线程持有 fd 引用，fd 复用会串读下一用例的字节
        for il in self._layers:
            il.close()
        sys.stdin = self._old_stdin
        self._fake.close()
        os.close(self.w)

    def _layer(self, state=None):
        il = InputLayer(state or TurnState(True))
        il._tty_out = None  # 单测无终端：关 /dev/tty 回显
        self._layers.append(il)
        il.start()
        return il

    def test_ascii_submit(self):
        il = self._layer()
        os.write(self.w, b"hello\n")
        self.assertEqual(il.pop(), "hello")
        il.close()

    def test_eof_on_ctrl_d_empty_line(self):
        il = self._layer()
        os.write(self.w, b"\x04")
        with self.assertRaises(EOFError):
            il.pop()
        il.close()

    def test_cjk_incremental_decode(self):
        il = self._layer()
        os.write(self.w, "茅台".encode())
        deadline = time.time() + 2
        while il.draft() != "茅台" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(il.draft(), "茅台")
        il.close()

    def test_charwise_backspace(self):
        il = self._layer()
        os.write(self.w, "茅台".encode())
        deadline = time.time() + 2
        while il.draft() != "茅台" and time.time() < deadline:
            time.sleep(0.02)
        il._backspace()
        self.assertEqual(il.draft(), "茅")   # 按字符退格，非按字节
        il._backspace()
        self.assertEqual(il.draft(), "")     # 退到底
        il.close()

    def test_type_after_backspace_then_submit(self):
        il = self._layer()
        os.write(self.w, "茅台".encode())
        deadline = time.time() + 2
        while il.draft() != "茅台" and time.time() < deadline:
            time.sleep(0.02)
        il._backspace(); il._backspace()
        os.write(self.w, "台\n".encode())
        self.assertEqual(il.pop(), "台")
        il.close()


if __name__ == "__main__":
    unittest.main()
