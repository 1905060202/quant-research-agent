"""qra_console 核心单元测试（vendor 同步回归门禁的一部分）。

覆盖 TurnState 折叠状态机与 InputLayer 行编辑核心路径，以及 P0 扩展：
Tab 补全 / ↑↓ 历史 / 孤立 ESC 冲刷 / ask_modal 模态委托。
运行：.venv-v7/bin/python -m unittest discover -s src/qra/console/tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console import commands  # noqa: E402
from qra.console.main import InputLayer, TurnState, _char_width  # noqa: E402
from qra.console.session_state import ConsoleHistory  # noqa: E402


def _wait_draft(il: InputLayer, expect: str, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while il.draft() != expect and time.time() < deadline:
        time.sleep(0.02)


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


class TabCompletionTests(unittest.TestCase):
    def setUp(self):
        self.r, self.w = os.pipe()
        self._fake = os.fdopen(self.r, "rb")
        self._old_stdin = sys.stdin
        self._layers = []
        sys.stdin = self._fake

    def tearDown(self):
        for il in self._layers:
            il.close()
        sys.stdin = self._old_stdin
        self._fake.close()
        os.close(self.w)

    def _layer(self):
        il = InputLayer(TurnState(True), completer=commands.complete)
        il._tty_out = None
        self._layers.append(il)
        il.start()
        return il

    def test_tab_unique_match(self):
        il = self._layer()
        os.write(self.w, b"/res\t")
        _wait_draft(il, "/resume ")   # 唯一匹配 → 补全并加空格
        self.assertEqual(il.draft(), "/resume ")
        il.close()

    def test_tab_lcp_progress(self):
        il = self._layer()
        os.write(self.w, b"/me\t")
        _wait_draft(il, "/mem")       # mem/memory → 最长公共前缀
        self.assertEqual(il.draft(), "/mem")
        il.close()

    def test_tab_no_progress_keeps_literal(self):
        il = self._layer()
        os.write(self.w, b"/m\t")
        deadline = time.time() + 2
        while il.draft() != "/m    " and time.time() < deadline:
            time.sleep(0.02)
        # 无进展：Tab 落为 4 空格（字面 \t 会触发终端制表跳，宽度模型失配）
        self.assertEqual(il.draft(), "/m    ")
        il.close()


class ArrowHistoryTests(unittest.TestCase):
    def setUp(self):
        self.r, self.w = os.pipe()
        self._fake = os.fdopen(self.r, "rb")
        self._old_stdin = sys.stdin
        self._tmp = tempfile.TemporaryDirectory()
        self._hist = ConsoleHistory(path=Path(self._tmp.name) / "h.jsonl")
        self._hist.push("first")
        self._hist.push("second")
        self._layers = []
        sys.stdin = self._fake

    def tearDown(self):
        for il in self._layers:
            il.close()
        sys.stdin = self._old_stdin
        self._fake.close()
        os.close(self.w)
        self._tmp.cleanup()

    def _layer(self):
        il = InputLayer(TurnState(True), history=self._hist)
        il._tty_out = None
        self._layers.append(il)
        il.start()
        return il

    def test_up_replaces_draft_with_recent(self):
        il = self._layer()
        os.write(self.w, b"old\x1b[A")          # 打了草稿再按 ↑：整行换成最近历史
        _wait_draft(il, "second")
        self.assertEqual(il.draft(), "second")
        il.close()

    def test_up_then_down_clears_to_empty(self):
        il = self._layer()
        os.write(self.w, b"\x1b[A")
        _wait_draft(il, "second")
        self.assertEqual(il.draft(), "second")
        os.write(self.w, b"\x1b[B")             # 已在最新 → ↓ 越过末尾成空草稿
        _wait_draft(il, "")
        self.assertEqual(il.draft(), "")
        il.close()

    def test_up_twice_goes_older(self):
        il = self._layer()
        os.write(self.w, b"\x1b[A")
        _wait_draft(il, "second")
        self.assertEqual(il.draft(), "second")
        os.write(self.w, b"\x1b[A")
        _wait_draft(il, "first")
        self.assertEqual(il.draft(), "first")
        il.close()


class EscapeFlushTests(unittest.TestCase):
    """孤立 ESC 冲刷：防吞 Alt+字母 / 孤立 ESC 后的可打印字符。"""

    def setUp(self):
        self.r, self.w = os.pipe()
        self._fake = os.fdopen(self.r, "rb")
        self._old_stdin = sys.stdin
        self._layers = []
        sys.stdin = self._fake

    def tearDown(self):
        for il in self._layers:
            il.close()
        sys.stdin = self._old_stdin
        self._fake.close()
        os.close(self.w)

    def _layer(self):
        il = InputLayer(TurnState(True))
        il._tty_out = None
        self._layers.append(il)
        il.start()
        return il

    def test_orphan_esc_then_letter_types_letter(self):
        il = self._layer()
        os.write(self.w, b"\x1ba")
        _wait_draft(il, "a")                    # ESC 被冲刷，a 照常键入
        self.assertEqual(il.draft(), "a")
        il.close()

    def test_alt_style_key_not_swallowed(self):
        il = self._layer()
        os.write(self.w, b"\x1bb")
        _wait_draft(il, "b")
        self.assertEqual(il.draft(), "b")
        il.close()


class PasteIntegrationTests(unittest.TestCase):
    """burst 级粘贴保护：64 字节读窗口下累计 ≥4096 触发确认模态。"""

    def setUp(self):
        self.r, self.w = os.pipe()
        self._fake = os.fdopen(self.r, "rb")
        self._old_stdin = sys.stdin
        self._layers = []
        sys.stdin = self._fake

    def tearDown(self):
        for il in self._layers:
            il.close()
        sys.stdin = self._old_stdin
        self._fake.close()
        os.close(self.w)

    def _layer(self):
        il = InputLayer(TurnState(True))
        il._tty_out = None
        self._layers.append(il)
        il.start()
        return il

    def test_paste_accepted_submits_line(self):
        il = self._layer()
        os.write(self.w, b"X" * 5000 + b"\n")   # 大块 → 确认模态
        os.write(self.w, b"y\n")                # 批准
        line = il.pop()
        self.assertEqual(len(line), 5000)
        self.assertTrue(line.startswith("X"))
        il.close()

    def test_paste_rejected_clears_draft(self):
        il = self._layer()
        os.write(self.w, b"Y" * 5000 + b"\n")
        os.write(self.w, b"n\n")                # 默认拒绝
        deadline = time.time() + 3
        while il.draft() != "" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(il.draft(), "")        # 草稿被清
        self.assertTrue(il._q.empty())          # 不提交任何行
        il.close()


class MultilinePasteTests(unittest.TestCase):
    """多行草稿（2026-08-17 崩溃修复回归锁）。

    根因：粘贴内容里的 \\n 曾作为普通字符插进 LineBuffer，帧按单行宽度
    模型渲染（cell_len(\\n)=0）→ 行文本含真换行 → 终端错位 → 每键重绘
    加剧 → 终端模拟器崩溃（雅宁实测 01:43 所有 shell 全没）。修复后：
    \\n 保留为多行草稿（帧换行感知），\\r/\\t 规范化。
    """

    def setUp(self):
        self.r, self.w = os.pipe()
        self._fake = os.fdopen(self.r, "rb")
        self._old_stdin = sys.stdin
        self._layers = []
        sys.stdin = self._fake

    def tearDown(self):
        for il in self._layers:
            il.close()
        sys.stdin = self._old_stdin
        self._fake.close()
        os.close(self.w)

    def _layer(self, **kw):
        il = InputLayer(TurnState(True), **kw)
        il._tty_out = None
        self._layers.append(il)
        il.start()
        return il

    def _paste(self, payload: bytes) -> None:
        os.write(self.w, b"\x1b[200~" + payload + b"\x1b[201~")

    def test_paste_multiline_keeps_newline_in_draft(self):
        il = self._layer()
        self._paste("line1\nline2".encode())
        _wait_draft(il, "line1\nline2")
        self.assertEqual(il.draft(), "line1\nline2")

    def test_paste_normalizes_crlf_cr_tab(self):
        il = self._layer()
        self._paste(b"a\r\nb\tc\r")
        _wait_draft(il, "a\nb    c\n")
        self.assertEqual(il.draft(), "a\nb    c\n")

    def test_multiline_submit_sends_whole_draft(self):
        il = self._layer()
        self._paste(b"line1\nline2")
        _wait_draft(il, "line1\nline2")
        os.write(self.w, b"\r")           # Enter 提交整串
        line = il.pop()
        self.assertEqual(line, "line1\nline2")
        _wait_draft(il, "")

    def test_multiline_draft_does_not_open_menu(self):
        il = self._layer(menu_provider=commands.menu_items)
        self._paste("/resume\n继续".encode())
        _wait_draft(il, "/resume\n继续")
        self.assertIsNone(il._menu)       # 多行不弹斜杠菜单

    def test_crlf_paste_then_enter_submits_normalized(self):
        il = self._layer()
        self._paste(b"a\r\nb")
        _wait_draft(il, "a\nb")
        os.write(self.w, b"\r")
        self.assertEqual(il.pop(), "a\nb")
        il.close()


class AskModalTests(unittest.TestCase):
    """ask_modal 委托读线程：主线程不并发抢 stdin，模态内独占取行。"""

    def setUp(self):
        self.r, self.w = os.pipe()
        self._fake = os.fdopen(self.r, "rb")
        self._old_stdin = sys.stdin
        self._layers = []
        sys.stdin = self._fake

    def tearDown(self):
        for il in self._layers:
            il.close()
        sys.stdin = self._old_stdin
        self._fake.close()
        os.close(self.w)

    def _layer(self):
        il = InputLayer(TurnState(True))
        il._tty_out = None
        self._layers.append(il)
        il.start()
        return il

    def test_modal_delegates_to_reader_thread(self):
        il = self._layer()
        result = {}

        def ask():
            result["ans"] = il.ask_modal("批准? ")

        t = threading.Thread(target=ask, daemon=True)   # daemon：失败也不阻塞进程退出
        t.start()
        time.sleep(0.6)         # 等读线程转进模态（select 周期 0.2s）
        os.write(self.w, b"y\n")
        t.join(timeout=15)
        self.assertFalse(t.is_alive(), "ask_modal 未在 15s 内返回")
        self.assertEqual(result["ans"], "y")
        self.assertEqual(il.draft(), "")                # 模态不污染草稿
        il.close()


if __name__ == "__main__":
    unittest.main()
