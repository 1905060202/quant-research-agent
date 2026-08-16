"""InputLayer D011 v2 原始字节测试：光标编辑 / 斜杠菜单 / 括号粘贴 /
SGR 鼠标 / 回合静音回显（2026-08-16 崩溃修复 + 光标 P0）。

与旧 test_inputlayer.py 的差异：这里给 InputLayer 传显式
TermIO(file=StringIO)，断言输出字节（回显协议），旧文件只断言草稿态。
"""

from __future__ import annotations

import io
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console.main import InputLayer, TurnState  # noqa: E402
from qra.console.termio import TermIO  # noqa: E402

# 菜单测试用的极简提供者（不 import commands：解耦注册表）
_MENU = [("help", "显示全部命令"), ("sessions", "列出最近会话"),
         ("resume", "恢复会话"), ("fold", "折叠块管理")]


def _menu_provider(draft: str):
    if not draft.startswith("/") or " " in draft:
        return []
    want = draft[1:].lower()
    return [(n, d) for n, d in _MENU if n.startswith(want)]


def _wait(cond, timeout: float = 2.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)


class _Base(unittest.TestCase):
    def setUp(self):
        self.r, self.w = os.pipe()
        self._fake = os.fdopen(self.r, "rb")
        self._old_stdin = sys.stdin
        self._out = io.StringIO()
        self._tio = TermIO(file=self._out)
        self._layers = []
        sys.stdin = self._fake

    def tearDown(self):
        for il in self._layers:
            il.close()
        sys.stdin = self._old_stdin
        self._fake.close()
        os.close(self.w)

    def _layer(self, **kw):
        kw.setdefault("tio", self._tio)
        il = InputLayer(TurnState(True), **kw)
        self._layers.append(il)
        il.start()
        return il

    def _wait_draft(self, il, expect: str, timeout: float = 2.0):
        _wait(lambda: il.draft() == expect, timeout)
        self.assertEqual(il.draft(), expect)


class CursorEditTests(_Base):
    def test_left_right_move(self):
        il = self._layer()
        os.write(self.w, b"ab\x1b[D")
        self._wait_draft(il, "ab")
        _wait(lambda: il._buf.pos == 1)
        os.write(self.w, b"\x1b[D")
        _wait(lambda: il._buf.pos == 0)
        os.write(self.w, b"\x1b[C\x1b[C")
        _wait(lambda: il._buf.pos == 2)

    def test_mid_insert_and_home_end(self):
        il = self._layer()
        os.write(self.w, b"ac\x1b[D")
        self._wait_draft(il, "ac")
        _wait(lambda: il._buf.pos == 1)
        os.write(self.w, b"b")                    # 行中插入
        self._wait_draft(il, "abc")
        self.assertEqual(il._buf.pos, 2)
        os.write(self.w, b"\x1b[H")               # Home
        _wait(lambda: il._buf.pos == 0)
        os.write(self.w, b"\x1b[F")               # End
        _wait(lambda: il._buf.pos == 3)

    def test_delete_key(self):
        il = self._layer()
        os.write(self.w, b"abc\x1b[D\x1b[D\x1b[3~")   # 光标 a|bc，Delete b
        self._wait_draft(il, "ac")
        self.assertEqual(il._buf.pos, 1)

    def test_ctrl_a_e_k(self):
        il = self._layer()
        os.write(self.w, b"xyz\x01")              # Ctrl+A 行首
        self._wait_draft(il, "xyz")
        _wait(lambda: il._buf.pos == 0)
        os.write(self.w, b"\x05")                 # Ctrl+E 行尾
        _wait(lambda: il._buf.pos == 3)
        os.write(self.w, b"\x01")                 # 回行首
        _wait(lambda: il._buf.pos == 0)
        os.write(self.w, b"\x0b")                 # Ctrl+K 删到行尾
        self._wait_draft(il, "")


class SlashMenuTests(_Base):
    def test_menu_opens_filters_closes(self):
        il = self._layer(menu_provider=_menu_provider)
        os.write(self.w, b"/")
        self._wait_draft(il, "/")
        _wait(lambda: il._menu is not None)       # 面板弹出
        os.write(self.w, b"s")                    # 过滤到 sessions
        self._wait_draft(il, "/s")
        _wait(lambda: il._menu is not None and len(il._menu.items) == 1)
        os.write(self.w, b"\x1b")                 # Esc 关闭
        _wait(lambda: il._menu is None)

    def test_menu_enter_applies_and_submits(self):
        il = self._layer(menu_provider=_menu_provider)
        os.write(self.w, b"/h\n")                 # 选中唯一候选并回车 = 执行
        _wait(lambda: il._q.qsize() == 1)         # 正向等待：提交入队
        self.assertEqual(il._q.get_nowait(), "/help ")
        self.assertIsNone(il._menu)
        self.assertEqual(il.draft(), "")          # 提交后草稿清空

    def test_menu_up_down_tab(self):
        il = self._layer(menu_provider=_menu_provider)
        os.write(self.w, b"/")
        self._wait_draft(il, "/")
        _wait(lambda: il._menu is not None)
        os.write(self.w, b"\x1b[B")               # ↓ 到第二项
        _wait(lambda: il._menu.sel == 1)
        os.write(self.w, b"\x1b[A\x1b[A")         # ↑↑ 停在首项（边界不动）
        _wait(lambda: il._menu.sel == 0)
        os.write(self.w, b"f")                    # 过滤到唯一候选 fold
        self._wait_draft(il, "/f")
        _wait(lambda: il._menu is not None and len(il._menu.items) == 1)
        os.write(self.w, b"\t")                   # Tab 唯一命中 → 补全加空格
        self._wait_draft(il, "/fold ")
        _wait(lambda: il._menu is None)


class BracketPasteTests(_Base):
    def test_bracket_paste_buffers_and_redraws_once(self):
        il = self._layer()
        os.write(self.w, b"\x1b[200~hello\x1b[201~")
        self._wait_draft(il, "hello")
        out = self._out.getvalue()
        # 粘贴期间静音缓冲，关闭符后一次重绘：hello 在输出中只出现一次
        self.assertEqual(out.count("hello"), 1)

    def test_plain_typing_echoes_per_char(self):
        il = self._layer()
        os.write(self.w, b"ab")
        self._wait_draft(il, "ab")
        out = self._out.getvalue()
        self.assertGreaterEqual(out.count("a"), 1)


class BusySilenceTests(_Base):
    def test_busy_typing_silent_until_redraw(self):
        il = self._layer()
        il.set_busy(True)                         # 回合中
        os.write(self.w, b"quiet")
        self._wait_draft(il, "quiet")
        self.assertNotIn("quiet", self._out.getvalue())   # 回显静音
        il.set_busy(False)
        self.assertIn("quiet", self._out.getvalue())     # 结束补画

    def test_sgr_click_goes_to_sink_when_busy(self):
        il = self._layer()
        got = []
        il.set_event_sink(got.append)
        il.set_busy(True)
        os.write(self.w, b"\x1b[<0;5;10M")        # 左键按下：col=5 row=10
        _wait(lambda: bool(got))
        self.assertEqual(got[0], ("click", 10, 5))

    def test_ctrl_t_goes_to_sink_when_busy(self):
        il = self._layer()
        got = []
        il.set_event_sink(got.append)
        il.set_busy(True)
        os.write(self.w, b"\x14")                 # Ctrl+T
        _wait(lambda: bool(got))
        self.assertEqual(got[0], ("toggle_thinking",))

    def test_ctrl_t_sets_dirty_when_idle(self):
        il = self._layer()
        il._state.dirty = False
        os.write(self.w, b"\x14")
        _wait(lambda: il._state.dirty)
        self.assertEqual(il._state.dirty, True)


class MenuGeometryTests(_Base):
    def test_menu_clear_on_submit(self):
        il = self._layer(menu_provider=_menu_provider)
        os.write(self.w, b"/fold\n")
        _wait(lambda: il._q.qsize() == 1)         # 正向等待：提交入队
        self.assertEqual(il._q.get_nowait(), "/fold ")
        self.assertIsNone(il._menu)
        # 提交行回显在输出里（提示符重绘带命令文本）
        self.assertIn("/fold", self._out.getvalue())


if __name__ == "__main__":
    unittest.main()
