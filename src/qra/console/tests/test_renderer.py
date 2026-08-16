"""TurnRenderer 追加式流式渲染单测（D011）。

TermIO 接 StringIO：捕获全部输出字节。断言重点：
- 追加式：已定型内容只出现一次（消灭 Live 全帧重绘的重复输出）
- 折叠/展开：行账本切换 + 区域重印内容正确
- 静默思考：show_thinking=False 时只印 recap 行
- 流式期折叠延迟到 finish
- 门禁硬标记：✻ 思考 / ⏺ 工具 / ¥ 用量页脚均在
"""

from __future__ import annotations

import io
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console.renderer import Block, TurnRenderer, _thinking_recap  # noqa: E402
from qra.console.termio import TermIO  # noqa: E402


class _State:
    """最小 TurnState 替身（renderer 只读 show_thinking）。"""

    def __init__(self, show_thinking: bool = True) -> None:
        self.show_thinking = show_thinking


def _setup(show_thinking: bool = True):
    buf = io.StringIO()
    tio = TermIO(file=buf)
    state = _State(show_thinking)
    r = TurnRenderer(tio, state)
    return buf, tio, state, r


def _count(buf: io.StringIO, token: str) -> int:
    return buf.getvalue().count(token)


class AppendOnlyTests(unittest.TestCase):
    def test_reasoning_streamed_once(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.reasoning("THINK-A-")
        r.reasoning("THINK-B-")
        r.reasoning_close(1.5)
        out = buf.getvalue()
        self.assertEqual(_count(buf, "THINK-A-"), 1)   # 追加式：首段只一次
        self.assertEqual(_count(buf, "THINK-B-"), 1)
        self.assertIn("✻ 思考", out)                    # 块头
        self.assertIn("用时 2s", out)                   # 块尾（1.5 → 2）
        self.assertEqual(r.blocks[0].kind, "thinking")

    def test_silent_folded_thinking_recap_only(self):
        buf, tio, state, r = _setup(show_thinking=False)
        r.begin()
        r.reasoning("内部推理 **结论摘要** 更多推理")
        r.reasoning_close(3.0)
        out = buf.getvalue()
        self.assertNotIn("内部推理", out)               # 静默：正文不上屏
        self.assertIn("结论摘要", out)                  # recap = 最后加粗标题
        self.assertIn("✻ 思考", out)
        self.assertTrue(r.blocks[0].collapsed)

    def test_text_streamed_then_markdown_reprint(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.text_delta("hello **bold** world")
        r.text_close()
        blk = r.blocks[0]
        # 闭合后行账本与测量一致
        self.assertEqual(blk.end_row - blk.start_row + 1,
                         tio.measure_rows("hello **bold** world", end=""))
        self.assertTrue(blk.text_rendered)


class ToolFoldTests(unittest.TestCase):
    def test_tool_line_format_and_fold(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.tool_start("tc1", "qra_quote", {"symbol": "600519"})
        r.tool_complete("tc1", "qra_quote", {"symbol": "600519"},
                        "查询结果：42.0 元", True)
        out = buf.getvalue()
        self.assertIn("⏺ 工具", out)          # 折叠行
        self.assertIn("qra_quote", out)
        self.assertIn("✓", out)
        self.assertIn("▸", out)
        self.assertNotIn("查询结果", out)     # 折叠态：结果正文不上屏
        blk = r.blocks[0]
        self.assertTrue(blk.collapsed)
        self.assertGreater(blk.duration, 0.0)

    def test_toggle_block_expands_and_collapses(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.tool_start("tc1", "qra_quote", {})
        r.tool_complete("tc1", "qra_quote", {}, "BODY-42", True)
        self.assertTrue(r.toggle_block(1))
        self.assertFalse(r.blocks[0].collapsed)
        self.assertIn("BODY-42", buf.getvalue())     # 展开：正文上屏
        self.assertTrue(r.toggle_block(1))
        self.assertTrue(r.blocks[0].collapsed)
        self.assertEqual(_count(buf, "BODY-42"), 1)  # 收起：不再重复印

    def test_subagent_title(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.tool_start("tc1", "delegate_task", {"role": "researcher"})
        r.tool_complete("tc1", "delegate_task", {"role": "researcher"}, "ok", True)
        self.assertIn("⎇ 子代理", buf.getvalue())

    def test_fold_list(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.reasoning("x **小标题** y")
        r.reasoning_close(1.0)
        r.tool_start("tc1", "qra_quote", {})
        r.tool_complete("tc1", "qra_quote", {}, "r", True)
        fl = r.fold_list()
        self.assertEqual(len(fl), 2)
        self.assertEqual(fl[0][1], "✻")    # thinking 序号 1
        self.assertEqual(fl[1][1], "⏺")    # tool 序号 2
        self.assertTrue(fl[1][3])          # tool 默认折叠
        self.assertFalse(r.toggle_block(99))   # 越界序号：不发生切换


class StreamingDeferTests(unittest.TestCase):
    def test_fold_during_streaming_deferred_to_finish(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.tool_start("tc1", "qra_quote", {})
        r.tool_complete("tc1", "qra_quote", {}, "BODY-7", True)
        r.text_delta("streaming...")          # 文本块流式中
        before = buf.getvalue()
        self.assertTrue(r.toggle_block(1))    # 流式期：只翻账本
        self.assertEqual(buf.getvalue(), before)   # 折叠操作不产生输出
        r.finish(None, "test-model")
        self.assertIn("BODY-7", buf.getvalue())   # finish 统一重印

    def test_spinner_suppressed_during_streaming(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.text_delta("abc")
        n0 = len(buf.getvalue())
        r.tick()
        self.assertEqual(len(buf.getvalue()), n0)   # 流式期无 spinner

    def test_spinner_idle_only(self):
        buf, tio, state, r = _setup()
        r.begin()
        r._last_content_at = time.time() - 1   # 越过 0.3s 空闲门
        r.tick()
        r.tick()
        out = buf.getvalue()
        self.assertIn("\r", out)        # spinner 原地刷新
        self.assertIn("思考中", out)


class GateMarkerTests(unittest.TestCase):
    def test_usage_footer_has_yen(self):
        buf, tio, state, r = _setup()
        r.begin()
        r.text_delta("final")
        r.text_close()
        r.finish({"input_tokens": 1000, "output_tokens": 500,
                  "cache_read_tokens": 0, "api_calls": 1}, "deepseek-v4-pro")
        out = buf.getvalue()
        self.assertIn("¥", out)          # _e2e_helpers.py:149 门禁硬标记

    def test_recap_last_bold(self):
        self.assertEqual(_thinking_recap("a **一** b **二**"), "二")
        self.assertEqual(_thinking_recap("没有标题"), "思考中…")


if __name__ == "__main__":
    unittest.main()
