"""qra_python 持久内核四级验证 + 机理单测（D007 P2）。

四级（D007 规格）：执行 → 跨轮变量 → dill 恢复 → bench 题。
机理：debounce / 超时中断 / 死内核自愈（有/无快照）/ 恢复过滤 /
输出截断 / LRU 驱逐 / 空闲关停 / 全生命周期通用工作流。

env 全在 setUpModule 里收紧（HERMES_HOME=临时目录 + QRA_PY_* 快参数），
import 顺序决定了模块常量必须在 env 设置之后读——所以 setUpModule 先设
env 再 import 插件模块。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

Q = None  # setUpModule 里填充（env 必须先于 import）


def setUpModule():
    global Q
    tmp = tempfile.mkdtemp(prefix="qra_python_test_")
    os.environ["HERMES_HOME"] = tmp
    os.environ["QRA_PY_DEBOUNCE"] = "0.3"
    os.environ["QRA_PY_MIN_INTERVAL"] = "0.5"
    os.environ["QRA_PY_IDLE"] = "2"
    os.environ["QRA_PY_MAXLIVE"] = "1"
    os.environ["QRA_PY_MAXOUT"] = "200"
    os.environ["QRA_PY_MAXCODE"] = "4000"
    os.environ["QRA_PY_TICK"] = "0.3"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import qra_python as _mod

    Q = _mod


def _call(sid: str, code: str) -> dict:
    """走 handler 全链路（与模型调用同路径）：args dict + session_id kwarg。"""
    return json.loads(Q.qra_python({"code": code}, session_id=sid, task_id="t"))


def _direct(sid: str, code: str) -> dict:
    """走内部快路径：确保内核后直接执行（跳过 handler 校验）。"""
    e = Q._ensure_kernel(sid)
    return Q._execute_with_retry(e, code)


class KernelTestCase(unittest.TestCase):
    def tearDown(self):
        Q._reset_all()


class TestLevel1Execute(KernelTestCase):
    def test_basic_execute(self):
        r = _call("l1_a", "x = 6 * 7\nprint('hello')\nx")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], "42")
        self.assertEqual(r["stdout"], "hello\n")
        self.assertIsInstance(r["execution_count"], int)

    def test_stderr_and_display(self):
        r = _call("l1_b", "import sys\nsys.stderr.write('警告\\n')")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["stderr"], "警告\n")


class TestLevel2CrossCall(KernelTestCase):
    def test_variables_persist(self):
        self.assertTrue(_call("l2_a", "a = 41")["ok"])
        r = _call("l2_a", "a + 1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], "42")

    def test_functions_persist(self):
        self.assertTrue(_call("l2_b", "def twice(n): return n * 2")["ok"])
        r = _call("l2_b", "twice(twice(5))")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], "20")

    def test_sessions_isolated(self):
        _call("l2_c", "secret = 'c'")
        r = _call("l2_d", "secret")
        self.assertFalse(r["ok"], r)
        self.assertIn("NameError", r["error"])

    def test_error_then_kernel_alive(self):
        r = _call("l2_e", "raise ValueError('故意的')")
        self.assertFalse(r["ok"], r)
        self.assertIn("ValueError", r["error"])
        r2 = _call("l2_e", "1 + 1")
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["result"], "2")


class TestLevel3SnapshotRestore(KernelTestCase):
    def _snapshot_sid(self, sid: str, code: str):
        e = Q._ensure_kernel(sid)
        r = Q._execute_with_retry(e, code)
        self.assertTrue(r["ok"], r)
        e.dirty = True
        self.assertTrue(Q._snapshot(e), "快照失败")

    def test_snapshot_and_revive(self):
        sid = "l3_a"
        self._snapshot_sid(sid, "portfolio = {'aapl': 100, 'tsla': 50}\n"
                                 "def nav(): return sum(portfolio.values())")
        # 模拟 hermes 重启/会话回收：关停并清掉内存态
        e = Q._KERNELS[sid]
        Q._shutdown(e)
        Q._KERNELS.pop(sid, None)
        r = _call(sid, "portfolio['aapl'] + nav()")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r.get("restored_from_snapshot"), r)
        self.assertEqual(r["result"], "250")  # 100 + 150

    def test_restore_filters_ipython_injected_names(self):
        sid = "l3_b"
        # 旧内核执行过有结果的表达式：Out 里会留下 2——若恢复不过滤，它会泄漏
        self._snapshot_sid(sid, "1 + 1\nmy_var = 'keep me'")
        e = Q._KERNELS[sid]
        Q._shutdown(e)
        Q._KERNELS.pop(sid, None)
        r = _call(sid, "my_var")
        self.assertTrue(r["ok"] and r["result"] == "'keep me'", r)
        # 快照 meta 键不得回注成变量
        r2 = _call(sid, "__qra_meta__")
        self.assertFalse(r2["ok"], r2)
        self.assertIn("NameError", r2["error"])
        # 旧内核的 Out 历史不得泄漏进新内核（新内核 Out 只含自己的结果）
        r3 = _call(sid, "2 in list(Out.values())")
        self.assertTrue(r3["ok"], r3)
        self.assertEqual(r3["result"], "False")

    def test_debounce_auto_snapshot(self):
        # 执行后 dirty；后台线程 idle≥0.3s 且距上次快照≥0.5s 时自动落盘
        sid = "l3_c"
        e = Q._ensure_kernel(sid)
        self.assertTrue(Q._execute_with_retry(e, "auto_saved = 'auto'")["ok"])
        self.assertFalse(e.state_path.exists(), "debounce 窗口内不应立即快照")
        deadline = time.time() + 8
        while time.time() < deadline and not e.state_path.exists():
            time.sleep(0.2)
        self.assertTrue(e.state_path.exists(), "debounce 后应自动落快照")
        # 新格式：{'meta': {'exec_count': ...}, '_payload': {...}}——meta 含 exec_count
        expected = e.exec_count  # 快照时点的执行计数
        code = (
            "import dill\n"
            f"_m = dill.load(open({str(e.state_path)!r}, 'rb'))\n"
            "print(_m['meta']['exec_count'])"
        )
        r = Q._execute_with_retry(e, code)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["stdout"].strip(), str(expected))


class TestLevel4BenchQuestion(KernelTestCase):
    def test_rsi_bench_question(self):
        """bench 题（脚本化模拟）：模型用 qra_python 算 RSI 再回答。

        与真实 bench 题同构：第一笔调用算并留变量，第二笔取数回答。
        期望值用纯 Python 在测试侧独立重算——两侧必须一致。
        """
        sid = "l4_rsi"
        setup = (
            "import random\n"
            "random.seed(42)\n"
            "prices = [100 + sum(random.gauss(0.3, 1.5) for _ in range(i)) "
            "for i in range(1, 31)]\n"
            "def rsi(closes, n=14):\n"
            "    gains, losses = [], []\n"
            "    for i in range(1, len(closes)):\n"
            "        d = closes[i] - closes[i-1]\n"
            "        gains.append(max(d, 0)); losses.append(max(-d, 0))\n"
            "    ag = sum(gains[:n]) / n; al = sum(losses[:n]) / n\n"
            "    for i in range(n, len(gains)):\n"
            "        ag = (ag * (n - 1) + gains[i]) / n\n"
            "        al = (al * (n - 1) + losses[i]) / n\n"
            "    return 100 - 100 / (1 + ag / al) if al else 100.0\n"
            "rsi_14 = rsi(prices)\n"
            "f'{rsi_14:.2f}'"
        )
        r1 = _call(sid, setup)
        self.assertTrue(r1["ok"], r1)
        # 测试侧独立重算
        import random as _r

        _r.seed(42)
        prices = [
            100 + sum(_r.gauss(0.3, 1.5) for _ in range(i))
            for i in range(1, 31)
        ]

        def _rsi(closes, n=14):
            gains, losses = [], []
            for i in range(1, len(closes)):
                d = closes[i] - closes[i - 1]
                gains.append(max(d, 0))
                losses.append(max(-d, 0))
            ag = sum(gains[:n]) / n
            al = sum(losses[:n]) / n
            for i in range(n, len(gains)):
                ag = (ag * (n - 1) + gains[i]) / n
                al = (al * (n - 1) + losses[i]) / n
            return 100 - 100 / (1 + ag / al) if al else 100.0

        expected = f"{_rsi(prices):.2f}"
        # ipykernel 对 str 型 execute_result 的 text/plain 就是 repr（带引号）
        self.assertEqual(r1["result"].strip("'\""), expected, "内核 RSI 与测试侧重算不一致")
        # 第二笔：直接用持久变量回答问题（bench 的答题步）
        r2 = _call(sid, "f'rsi_14={rsi_14:.2f}, n={len(prices)}'")
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["result"].strip("'\""), f"rsi_14={expected}, n=30")

    def test_full_lifecycle_workflow(self):
        """全生命周期验证（非 quant）：写辅助函数 → 复用 → 数据加工。

        用户要求持久内核覆盖 quant 之外的所有工作：这里模拟一个通用
        文档处理工作流，验证「把复用逻辑写成函数留在内核里」的模式。
        """
        sid = "l4_gen"
        r1 = _call(
            sid,
            "def parse_kv(text):\n"
            "    out = {}\n"
            "    for line in text.strip().splitlines():\n"
            "        k, _, v = line.partition('=')\n"
            "        out[k.strip()] = v.strip()\n"
            "    return out\n"
            "config = parse_kv('host = localhost\\nport = 8080\\ndebug = true')",
        )
        self.assertTrue(r1["ok"], r1)
        r2 = _call(sid, "config['port']")
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["result"], "'8080'")
        # 函数还能继续复用在新数据上
        r3 = _call(sid, "parse_kv('name = qra\\nlang = zh')")
        self.assertTrue(r3["ok"], r3)
        self.assertEqual(r3["result"], "{'name': 'qra', 'lang': 'zh'}")


class TestMechanisms(KernelTestCase):
    def test_timeout_interrupt(self):
        e = Q._ensure_kernel("m_timeout")
        t0 = time.time()
        r = Q._execute(e, "while True: pass", timeout=1, record=True)
        self.assertLess(time.time() - t0, 20, "中断应在秒级返回")
        self.assertFalse(r["ok"], r)
        self.assertIn("中断", r["error"])
        r2 = Q._execute_with_retry(e, "1 + 1")
        self.assertTrue(r2["ok"] and r2["result"] == "2", r2)

    def test_dead_kernel_revive_with_snapshot(self):
        sid = "m_dead1"
        e = Q._ensure_kernel(sid)
        Q._execute_with_retry(e, "saved = '复活我'")
        Q._snapshot(e)
        os.kill(e.km.provisioner.process.pid, signal.SIGKILL)
        time.sleep(0.3)
        r = _call(sid, "saved")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], "'复活我'")
        self.assertTrue(r.get("restored_from_snapshot"), r)
        self.assertIn("重启", r.get("note", ""))

    def test_dead_kernel_honest_without_snapshot(self):
        sid = "m_dead2"
        e = Q._ensure_kernel(sid)
        Q._execute_with_retry(e, "gone = 1")
        os.kill(e.km.provisioner.process.pid, signal.SIGKILL)
        time.sleep(0.3)
        r = _call(sid, "gone")
        self.assertFalse(r["ok"], r)
        self.assertIn("无快照", r.get("note", ""))
        self.assertIn("NameError", r["error"])

    def test_output_truncation(self):
        r = _call("m_trunc", "print('x' * 500)")
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["stdout"]), 200)
        self.assertIn("截断", r["note"])

    def test_lru_eviction(self):
        # MAXLIVE=1：确保 B 时 A 被驱逐（快照带走状态），再取 A 时复活
        e_a = Q._ensure_kernel("m_lru_a")
        Q._execute_with_retry(e_a, "lru_var = 'A 的状态'")
        e_a.dirty = True
        _ = Q._ensure_kernel("m_lru_b")  # 触发驱逐 A
        self.assertFalse(Q._kernel_alive(e_a), "A 应已被驱逐关停")
        r = _call("m_lru_a", "lru_var")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], "'A 的状态'")
        self.assertTrue(r.get("restored_from_snapshot"), r)

    def test_idle_shutdown_and_respawn(self):
        # IDLE=2s：空闲后后台线程关停内核；再调用自动重开+快照复活
        sid = "m_idle"
        e = Q._ensure_kernel(sid)
        Q._execute_with_retry(e, "idle_var = '沉睡中'")
        Q._snapshot(e)
        deadline = time.time() + 10
        while time.time() < deadline and Q._kernel_alive(e):
            time.sleep(0.3)
        self.assertFalse(Q._kernel_alive(e), "空闲超时应自动关停")
        r = _call(sid, "idle_var")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], "'沉睡中'")
        self.assertTrue(r.get("restored_from_snapshot"), r)

    def test_handler_validation(self):
        self.assertIn("缺少 code", Q.qra_python({}, session_id="v"))
        self.assertIn("缺少 code", Q.qra_python({"code": "  "}, session_id="v"))
        self.assertIn("必须是对象", Q.qra_python("not a dict", session_id="v"))
        long_code = "x = " + "1" * 4200
        self.assertIn("超长", Q.qra_python({"code": long_code}, session_id="v"))

    def test_workspace_cwd(self):
        r = _call("m_cwd", "import os\nos.getcwd()")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"].strip("'\"").endswith("qra_python/workspace"), r)

    def test_history_audit(self):
        sid = "m_hist"
        e = Q._ensure_kernel(sid)
        Q._execute_with_retry(e, "audit_var = 1")
        self.assertTrue(e.history_path.exists())
        lines = e.history_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["exec"], 1)
        self.assertIn("audit_var", row["code"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
