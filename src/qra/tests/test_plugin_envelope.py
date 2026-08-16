"""插件工具 schema 信封回归锁（2026-08-17 修复 A）。

vendor 约定（对照 bundled spotify / execute_code）：ctx.register_tool 的
schema 必须是完整 function 信封 {name, description, parameters}——
registry.get_definitions 原样合并进工具面。裸 JSON schema 会让 deferred 面
（tool_search / tool_describe 桥）返回空描述 + 空 schema，模型只见裸名字、
永远不主动调用（自诊断文档《内核路由失效与修复指引》断层一）。

本测试以假 ctx 收集六个 QRA 工具的注册参数，断言信封形状，防退行。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGINS = ROOT / ".hermes" / "plugins"

sys.path.insert(0, str(PLUGINS))


def setUpModule():
    # qra_verify.register 有 _connect() 启动自检（写 $HERMES_HOME/qra_verify.db），
    # 用临时目录隔离，别碰真实账本。
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="qra_envelope_test_")


def _collect(plugin_pkg: str):
    mod = __import__(plugin_pkg)
    calls: list[dict] = []

    class _FakeCtx:
        def register_tool(self, **kw):
            calls.append(kw)

        def register_hook(self, *a, **kw):
            pass

    mod.register(_FakeCtx())
    return calls


class PluginEnvelopeTests(unittest.TestCase):
    def _assert_envelope(self, kw: dict):
        name = kw["name"]
        schema = kw["schema"]
        self.assertIsInstance(schema, dict, name)
        self.assertEqual(schema.get("name"), name)
        self.assertIsInstance(schema.get("description"), str, name)
        self.assertTrue(schema.get("description"), f"{name} 描述为空")
        params = schema.get("parameters")
        self.assertIsInstance(params, dict, name)
        self.assertEqual(params.get("type"), "object", name)
        self.assertIsInstance(params.get("properties"), dict, name)
        self.assertTrue(params.get("properties"), f"{name} 参数表为空")

    def test_qra_plugin_four_tools(self):
        calls = _collect("qra")
        names = {c["name"] for c in calls}
        self.assertEqual(names, {"qra_quote", "qra_signal", "qra_kb_fts", "qra_sync"})
        for kw in calls:
            self._assert_envelope(kw)

    def test_qra_verify_envelope(self):
        calls = _collect("qra_verify")
        self.assertEqual({c["name"] for c in calls}, {"qra_verify"})
        self._assert_envelope(calls[0])

    def test_qra_python_envelope(self):
        calls = _collect("qra_python")
        self.assertEqual({c["name"] for c in calls}, {"qra_python"})
        kw = calls[0]
        self._assert_envelope(kw)
        # 验收标准里的关键字样：模型必须能从描述知道这是持久内核
        self.assertIn("持久内核", kw["schema"]["description"])
        self.assertIn("code", kw["schema"]["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
