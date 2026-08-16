"""vendor_sync 多上游机制单元测试（D009 §7，离线，mock git/网络）。

覆盖：dispatch（默认 hermes / 未知上游 / 未知模式）、essence 语义
（already_latest / report 不落地且标记 needs_regraft / apply 推进钉针）、
managed 嫁接面硬拦截、CLI 参数解析。
真实网络路径（fetch/compare API/门禁）由历史真实同步（vendor_sync_log
#3/#4）与门禁第 6 层验证，此处不重复。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.qra import vendor_sync as vs  # noqa: E402


def _cfg(tmp: Path, name: str = "prime", kind: str = "essence",
         branch: str = "main", repo: str = "org/repo",
         graft: tuple[str, ...] = ("x/rlm/__init__.py",)) -> vs.UpstreamConfig:
    return vs.UpstreamConfig(name=name, vendor=tmp, repo=repo, branch=branch,
                             kind=kind, graft_paths=graft, hint="HINT 初始化")


def _pin(tmp: Path, sha: str) -> None:
    (tmp / "VERSION").write_text(sha + "\n")


class TestDispatch(unittest.TestCase):
    def test_unknown_upstream(self):
        r = vs.sync("report", "bogus")
        self.assertFalse(r["ok"])
        self.assertIn("未知上游", r["error"])
        self.assertIn("hermes", r["error"])

    def test_unknown_mode(self):
        r = vs.sync("bogus", "hermes")
        self.assertFalse(r["ok"])
        self.assertIn("未知模式", r["error"])

    def test_missing_vendor_dir_hint(self):
        # 真实 UPSTREAMS 的 hermes 换成缺失目录的假配置，不触网。
        tmp = Path("/tmp/qra_vs_missing_xyz")
        with mock.patch.dict(vs.UPSTREAMS, {"hermes": _cfg(tmp, "hermes", "managed")},
                             clear=True):
            r = vs.sync("report", "hermes")
        self.assertFalse(r["ok"])
        self.assertIn("HINT", r["error"])

    def test_default_is_hermes_full(self):
        # sync() 无 upstream 参数的默认值——插件与 CLI 的兼容锚点。
        with mock.patch.dict(vs.UPSTREAMS, {"hermes": mock.MagicMock(kind="managed")},
                             clear=True):
            with mock.patch.object(vs, "_sync_managed", return_value={"ok": True}) as m:
                vs.sync()
        m.assert_called_once()


class TestEssence(unittest.TestCase):
    def test_already_latest(self):
        tmp = Path("/tmp/qra_vs_ess_latest")
        tmp.mkdir(exist_ok=True)
        _pin(tmp, "d" * 40)
        cfg = _cfg(tmp)
        with mock.patch.object(vs, "_fetch_upstream", return_value="d" * 40):
            r = vs._sync_essence(cfg, "report")
        self.assertTrue(r["ok"])
        self.assertTrue(r["already_latest"])
        self.assertFalse(r["needs_regraft"])

    def test_report_with_graft_hit_does_not_land(self):
        tmp = Path("/tmp/qra_vs_ess_hit")
        tmp.mkdir(exist_ok=True)
        _pin(tmp, "a" * 40)
        cfg = _cfg(tmp)
        with mock.patch.object(vs, "_fetch_upstream", return_value="b" * 40), \
             mock.patch.object(vs, "_changed_files",
                               return_value=["x/rlm/__init__.py", "README.md"]), \
             mock.patch.object(vs, "_git") as g:
            r = vs._sync_essence(cfg, "report")
        self.assertTrue(r["ok"])
        self.assertTrue(r["needs_regraft"])
        self.assertEqual(r["graft_hits"], ["x/rlm/__init__.py"])
        self.assertNotIn("merged", r)
        g.assert_not_called()
        self.assertEqual((tmp / "VERSION").read_text(), "a" * 40 + "\n")

    def test_apply_advances_pin_and_flags_clean(self):
        tmp = Path("/tmp/qra_vs_ess_apply")
        tmp.mkdir(exist_ok=True)
        _pin(tmp, "a" * 40)
        cfg = _cfg(tmp)
        calls = []

        def fake_git(_cfg, *args, **kw):
            calls.append(args)
            return "NEWSHA" if args[0] == "rev-parse" else "Updating ..."

        with mock.patch.object(vs, "_fetch_upstream", return_value="b" * 40), \
             mock.patch.object(vs, "_changed_files", return_value=["README.md"]), \
             mock.patch.object(vs, "_git", side_effect=fake_git):
            r = vs._sync_essence(cfg, "full")
        self.assertTrue(r["ok"])
        self.assertTrue(r["merged"])
        self.assertFalse(r["needs_regraft"])
        self.assertEqual(r["new_pin"], "NEWSHA")
        self.assertEqual((tmp / "VERSION").read_text(), "NEWSHA\n")
        self.assertIn(("merge", "--ff-only", "upstream/main"), calls)


class TestManaged(unittest.TestCase):
    def test_graft_hit_rejects_without_merge(self):
        tmp = Path("/tmp/qra_vs_mgd_reject")
        tmp.mkdir(exist_ok=True)
        _pin(tmp, "a" * 40)
        cfg = _cfg(tmp, "hermes", "managed", graft=("run_agent.py",))
        with mock.patch.object(vs, "_fetch_upstream", return_value="b" * 40), \
             mock.patch.object(vs, "_changed_files", return_value=["run_agent.py"]), \
             mock.patch.object(vs, "_git") as g:
            r = vs._sync_managed(cfg, "apply")
        self.assertFalse(r["ok"])
        self.assertIn("拒绝", r["error"])
        g.assert_not_called()
        self.assertEqual((tmp / "VERSION").read_text(), "a" * 40 + "\n")

    def test_apply_merges_and_writes_pin(self):
        tmp = Path("/tmp/qra_vs_mgd_apply")
        tmp.mkdir(exist_ok=True)
        _pin(tmp, "a" * 40)
        cfg = _cfg(tmp, "hermes", "managed", graft=("run_agent.py",))
        calls = []

        def fake_git(_cfg, *args, **kw):
            calls.append(args)
            return "Updating ..."

        with mock.patch.object(vs, "_fetch_upstream", return_value="b" * 40), \
             mock.patch.object(vs, "_changed_files", return_value=["README.md"]), \
             mock.patch.object(vs, "_git", side_effect=fake_git):
            r = vs._sync_managed(cfg, "apply")
        self.assertTrue(r["ok"])
        self.assertTrue(r["merged"])
        self.assertEqual((tmp / "VERSION").read_text(), "b" * 40 + "\n")
        self.assertIn(("merge", "--ff-only", "upstream/main"), calls)


class TestCli(unittest.TestCase):
    def _run(self, argv, calls):
        with mock.patch.object(vs, "sync", side_effect=lambda m, u: calls.append((m, u))
                               or {"ok": True, "already_latest": True, "new_pin": "x" * 40,
                                   "old_pin": "x" * 40, "upstream": u, "mode": m}):
            rc = vs.main(argv)
        return rc

    def test_default_hermes_full(self):
        calls = []
        rc = self._run([], calls)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("full", "hermes")])

    def test_upstream_plus_mode(self):
        calls = []
        rc = self._run(["prime", "report"], calls)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("report", "prime")])

    def test_mode_only_stays_hermes(self):
        calls = []
        rc = self._run(["report"], calls)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("report", "hermes")])

    def test_upstream_only_defaults_full(self):
        calls = []
        rc = self._run(["dsh"], calls)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("full", "dsh")])

    def test_unknown_first_arg(self):
        calls = []
        with mock.patch.object(vs, "sync") as s:
            rc = vs.main(["bogus"])
        self.assertEqual(rc, 1)
        s.assert_not_called()

    def test_extra_args_rejected(self):
        with mock.patch.object(vs, "sync") as s:
            rc = vs.main(["hermes", "apply", "extra"])
        self.assertEqual(rc, 1)
        s.assert_not_called()

    def test_help(self):
        with mock.patch.object(vs, "sync") as s:
            rc = vs.main(["--help"])
        self.assertEqual(rc, 0)
        s.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
