"""ConsoleHistory ↑↓ 输入历史单元测试。

覆盖：push 顺序与去重、/ 与 ! 行跳过、cap 1000 环边界、jsonl 文件往返
（含脏行容忍）、up/down 导航边界。

运行：.venv-v7/bin/python -m unittest discover -s src/qra/console/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console.session_state import HISTORY_CAP, ConsoleHistory  # noqa: E402


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / "console_history.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def _hist(self):
        return ConsoleHistory(path=self._path)

    def test_push_then_up_returns_recent(self):
        h = self._hist()
        h.push("hello")
        h.push("world")
        self.assertEqual(h.up(""), "world")
        self.assertEqual(h.up(""), "hello")

    def test_up_stays_at_oldest(self):
        h = self._hist()
        h.push("a")
        h.push("b")
        h.up(""); h.up("")
        self.assertEqual(h.up(""), "a")   # 到最旧后原地不动

    def test_down_returns_next_then_empty(self):
        h = self._hist()
        h.push("a")
        h.push("b")
        self.assertEqual(h.up(""), "b")
        self.assertEqual(h.down(""), "")          # 已在最新 → 越到末尾空草稿
        self.assertEqual(h.down(""), "")          # 越界后保持空
        h.reset_cursor()
        self.assertEqual(h.up(""), "b")           # reset 后重新从最新开始

    def test_consecutive_duplicate_skipped(self):
        h = self._hist()
        h.push("same")
        h.push("same")
        self.assertEqual(h.up(""), "same")
        self.assertEqual(h.up(""), "same")        # 只有一条：再 ↑ 仍是最旧
        with open(self._path, encoding="utf-8") as f:
            self.assertEqual(sum(1 for _ in f), 1)

    def test_slash_and_bang_lines_skipped(self):
        h = self._hist()
        h.push("before")
        h.push("/resume 1")
        h.push("! ls -la")
        h.push("")
        self.assertEqual(h.up(""), "before")      # 命令行与空行不记历史

    def test_strip_whitespace(self):
        h = self._hist()
        h.push("  padded  ")
        self.assertEqual(h.up(""), "padded")

    def test_jsonl_roundtrip(self):
        h = self._hist()
        h.push("第一句")
        h.push("第二句")
        h2 = ConsoleHistory(path=self._path)
        self.assertEqual(h2.up(""), "第二句")
        self.assertEqual(h2.up(""), "第一句")

    def test_dirty_line_tolerated_as_raw(self):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write("not json { at all\n")
            f.write(json.dumps({"line": "ok"}, ensure_ascii=False) + "\n")
        h = self._hist()
        self.assertEqual(h.up(""), "ok")
        self.assertEqual(h.up(""), "not json { at all")   # 脏行整行当历史

    def test_cap_boundary_drops_oldest(self):
        h = self._hist()
        for i in range(HISTORY_CAP + 1):
            h.push(f"line{i}")
        self.assertEqual(h.up(""), f"line{HISTORY_CAP}")  # 最新
        # 最旧的 line0 被丢弃：从最新往回走到头应是 line1
        cur = h.up("")
        while True:
            nxt = h.up("")
            if nxt == cur:
                break
            cur = nxt
        self.assertEqual(cur, "line1")


if __name__ == "__main__":
    unittest.main()
