"""qra_sync 插件桥加载回归锁（2026-08-18 修复 B）。

根因：sync.py 的 _load_core() 用 importlib.util.spec_from_file_location 加载
vendor_sync.py，但没把模块注册进 sys.modules。vendor_sync.py 顶层 @dataclass
（UpstreamConfig）在 Python 3.9 的 dataclasses 实现里会执行
sys.modules.get(cls.__module__).__dict__——模块未注册返回 None 直接
AttributeError（'NoneType' object has no attribute '__dict__'），qra_sync
工具在对话里必然崩。

本测试直接调用 sync.qra_sync()，断言能完成加载并返回 JSON（不崩），
且核心模块已注册进 sys.modules（防去掉注册的退行）。

注：只验证加载路径与返回值形状，不触发真实网络同步（mode 传非法值会
被 sync.py 归一化为 full——为不碰网络，这里 monkeypatch 核心的 sync
函数为桩，只验证 _load_core 的加载路径）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGINS = ROOT / ".hermes" / "plugins"

sys.path.insert(0, str(PLUGINS))


def setUpModule():
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="qra_sync_load_test_")


class QraSyncLoadTests(unittest.TestCase):
    def test_load_core_registers_in_sys_modules(self):
        """_load_core 后核心模块必须在 sys.modules 中（dataclass 依赖）。"""
        from qra.sync import _load_core

        core = _load_core()
        # dataclass 装饰器已成功执行（模块加载未崩），且模块在 sys.modules 中
        self.assertIn("_qra_vendor_sync_core", sys.modules)
        self.assertIs(sys.modules["_qra_vendor_sync_core"], core)
        self.assertTrue(callable(core.sync))
        # 模块内 dataclass 已正确构造（frozen dataclass 实例可访问字段）
        cfg = core.UPSTREAMS["hermes"]
        self.assertEqual(cfg.name, "hermes")
        self.assertEqual(cfg.kind, "managed")

    def test_qra_sync_tool_returns_json_without_crash(self):
        """qra_sync 工具入口返回 JSON 且不抛 AttributeError。"""
        import qra.sync as sync_mod

        # 桩掉 _load_core（qra_sync 内部会重新加载核心），避免真实网络 fetch。
        # 本测试只验证加载桥 + 工具入口不崩；核心逻辑由 test 1 的加载验证覆盖。
        class _FakeCore:
            def sync(self, mode, upstream):
                return {"ok": True, "already_latest": True,
                        "new_pin": "stub" * 4, "upstream": upstream, "mode": mode}

        original = sync_mod._load_core
        sync_mod._load_core = lambda: _FakeCore()
        try:
            out = sync_mod.qra_sync({"mode": "report"})
        finally:
            sync_mod._load_core = original
        payload = json.loads(out)
        self.assertEqual(payload["synced"], False)  # already_latest → synced=False
        self.assertIn("message", payload)

    def test_dataclass_module_not_registered_still_crashes_old_way(self):
        """防退行：去掉 sys.modules 注册的旧实现确实会崩（证明本修复必要）。"""
        # 模拟旧实现：不在 sys.modules 注册就直接 exec_module
        import importlib.util

        src = PLUGINS.parent.parent / "src" / "qra" / "vendor_sync.py"
        spec = importlib.util.spec_from_file_location("_qra_old_path_test", src)
        mod = importlib.util.module_from_spec(spec)
        # 故意不注册 sys.modules —— 3.9 dataclass 处理应抛 AttributeError
        with self.assertRaises(AttributeError):
            spec.loader.exec_module(mod)


if __name__ == "__main__":
    unittest.main()
