"""palette 单测（审计 D-07）：env 覆盖链 / remap 全表 / 探测协议。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console import palette  # noqa: E402


class PaletteEnvOverrideTests(unittest.TestCase):

    def tearDown(self):
        palette._light = None
        for var in ("QRA_TUI_LIGHT", "HERMES_TUI_LIGHT", "QRA_TUI_THEME"):
            os.environ.pop(var, None)

    def test_default_dark(self):
        self.assertFalse(palette.is_light())

    def test_env_light_true_variants(self):
        for var, val in (("QRA_TUI_LIGHT", "1"), ("HERMES_TUI_LIGHT", "true"),
                         ("QRA_TUI_LIGHT", "yes")):
            with self.subTest(var=var, val=val):
                palette._light = None
                os.environ[var] = val
                self.assertTrue(palette.is_light())

    def test_env_light_false_variants(self):
        os.environ["QRA_TUI_LIGHT"] = "0"
        self.assertFalse(palette.is_light())

    def test_theme_var(self):
        os.environ["QRA_TUI_THEME"] = "light"
        self.assertTrue(palette.is_light())
        os.environ["QRA_TUI_THEME"] = "dark"
        palette._light = None
        self.assertFalse(palette.is_light())

    def test_set_light_overrides(self):
        palette.set_light(True)
        self.assertTrue(palette.is_light())
        palette.set_light(False)
        self.assertFalse(palette.is_light())


class PaletteRemapTests(unittest.TestCase):

    def setUp(self):
        palette.set_light(True)

    def tearDown(self):
        palette._light = None

    def test_remap_table_exact(self):
        """全表照抄 vendor cli.py `_LIGHT_MODE_REMAP`——逐键验证。"""
        for src, dst in {
            "#FFF8DC": "#1A1A1A", "#FFD700": "#9A6B00", "#FFBF00": "#8A5A00",
            "#B8860B": "#5C4500", "#DAA520": "#6B4F00", "#F1E6CF": "#1A1A1A",
            "#c9d1d9": "#24292F", "#EAF7FF": "#0F1B26", "#F5F5F5": "#1A1A1A",
            "#FFF0D4": "#1A1A1A", "#CD7F32": "#8A4F1A", "#FFEFB5": "#3A2A00",
        }.items():
            with self.subTest(src=src):
                self.assertEqual(palette.remap(src), dst)

    def test_remap_unknown_passthrough(self):
        self.assertEqual(palette.remap("#123456"), "#123456")

    def test_remap_case_insensitive(self):
        self.assertEqual(palette.remap("#ffd700"), "#9A6B00")

    def test_remap_dark_identity(self):
        palette.set_light(False)
        self.assertEqual(palette.remap("#FFD700"), "#FFD700")

    def test_helpers(self):
        self.assertEqual(palette.gold(), "#9A6B00")
        self.assertEqual(palette.accent(), "#8A5A00")
        self.assertEqual(palette.dim_gold(), "#5C4500")


class PaletteProbeTests(unittest.TestCase):

    def tearDown(self):
        palette._light = None
        os.environ.pop("QRA_TUI_LIGHT", None)

    def test_probe_env_override_short_circuits(self):
        """env 覆盖优先：探测白跑（非 tty fd 也不会触发任何 IO）。"""
        os.environ["QRA_TUI_LIGHT"] = "1"
        palette.probe_terminal_background(-1, -1)   # 无效 fd：走不到 termios
        self.assertTrue(palette.is_light())

    def test_probe_non_tty_returns_dark(self):
        """非 tty（重定向/测试）→ tcgetattr 失败 → 不探测，暗色。"""
        import tempfile
        with tempfile.TemporaryFile() as f:
            palette.probe_terminal_background(f.fileno(), f.fileno())
        self.assertFalse(palette.is_light())


if __name__ == "__main__":
    unittest.main()
