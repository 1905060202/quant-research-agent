"""detect_paste 大块粘贴判据单元测试（阈值边界锁定）。

判据：单 chunk 字节数 ≥ threshold_bytes 且距上一 chunk < threshold_ms。
人手打字不可能 200ms 内灌入 4KB；阈值边界是安全设计的一部分，
改判定逻辑必须同步改这里。

运行：.venv-v7/bin/python -m unittest discover -s src/qra/console/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console.input_layer import detect_paste  # noqa: E402


class DetectPasteTests(unittest.TestCase):
    def test_default_threshold_hit(self):
        self.assertTrue(detect_paste(4096, 199.9))

    def test_bytes_at_threshold_and_fast(self):
        # 恰 4096 字节且 <200ms → 命中
        self.assertTrue(detect_paste(4096, 0.0))
        self.assertTrue(detect_paste(5000, 100.0))

    def test_span_exactly_threshold_not_hit(self):
        # 200.0ms 不满足 <200ms：宁可少判一次粘贴，不可误拦正常输入
        self.assertFalse(detect_paste(4096, 200.0))

    def test_bytes_below_threshold_not_hit(self):
        self.assertFalse(detect_paste(4095, 199.9))

    def test_small_fast_chunk_not_hit(self):
        # 快速小 chunk（普通连续键入）不是粘贴
        self.assertFalse(detect_paste(64, 10.0))

    def test_custom_thresholds(self):
        fn = lambda n, ms: detect_paste(n, ms, threshold_bytes=10, threshold_ms=100)
        self.assertTrue(fn(10, 99.9))
        self.assertFalse(fn(9, 99.9))
        self.assertFalse(fn(10, 100.0))


if __name__ == "__main__":
    unittest.main()
