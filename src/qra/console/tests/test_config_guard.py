"""config_guard 单元测试（dsh 精华：fail-loud 启动自检 + 配置 schema 硬校验）。

覆盖：合法配置零问题；每类违规逐条检出（结构违规必报，未知插件名不误报）。
guard_config() 的 sys.exit 行为由门禁 pty 层间接覆盖（非法配置启动即退 2）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.config_guard import validate_config  # noqa: E402


def _good() -> dict:
    """与 .hermes/config.yaml 同构的合法配置。"""
    return {
        "model": {"default": "deepseek-v4-pro", "provider": "anthropic"},
        "plugins": {"enabled": ["qra", "qra_verify", "qra_refine",
                                 "qra_memory", "qra_python"]},
        "memory": {"provider": "qra_memory"},
        "approvals": {"timeout": 60},
        "model_overrides": {
            "anthropic": {"opus": {"context_window": 1000000}}},
    }


class ValidateConfigTests(unittest.TestCase):
    def test_good_config_clean(self):
        self.assertEqual(validate_config(_good()), [])

    def test_unknown_plugin_name_not_an_error(self):
        # 用户可自行启用 hermes 插件：结构合法就不判错
        cfg = _good()
        cfg["plugins"]["enabled"].append("spotify")
        self.assertEqual(validate_config(cfg), [])

    def test_model_default_out_of_domain(self):
        cfg = _good()
        cfg["model"]["default"] = "deepseek/opus"
        self.assertTrue(any("model.default" in p for p in validate_config(cfg)))

    def test_model_provider_wrong(self):
        cfg = _good()
        cfg["model"]["provider"] = "openai"
        self.assertTrue(any("model.provider" in p for p in validate_config(cfg)))

    def test_plugins_enabled_not_list(self):
        cfg = _good()
        cfg["plugins"]["enabled"] = "qra"
        self.assertTrue(any("plugins.enabled" in p for p in validate_config(cfg)))

    def test_plugins_enabled_empty_string_entry(self):
        cfg = _good()
        cfg["plugins"]["enabled"] = ["qra", "  "]
        self.assertTrue(any("plugins.enabled" in p for p in validate_config(cfg)))

    def test_memory_provider_wrong(self):
        cfg = _good()
        cfg["memory"]["provider"] = "builtin"
        self.assertTrue(any("memory.provider" in p for p in validate_config(cfg)))

    def test_approvals_timeout_bad(self):
        cfg = _good()
        cfg["approvals"]["timeout"] = -1
        self.assertTrue(any("approvals.timeout" in p for p in validate_config(cfg)))
        cfg["approvals"]["timeout"] = "60"
        self.assertTrue(any("approvals.timeout" in p for p in validate_config(cfg)))

    def test_context_window_bad(self):
        cfg = _good()
        cfg["model_overrides"]["anthropic"]["opus"]["context_window"] = "1M"
        self.assertTrue(any("context_window" in p for p in validate_config(cfg)))

    def test_missing_sections_detected(self):
        cfg = {"model": {"default": "deepseek-v4-pro", "provider": "anthropic"}}
        problems = validate_config(cfg)
        self.assertTrue(any("plugins" in p for p in problems))
        self.assertTrue(any("memory" in p for p in problems))
        self.assertTrue(any("approvals" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
