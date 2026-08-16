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
from unittest import mock

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

    def test_idle_env_parsing(self):
        """QRA_PY_IDLE 解析：空=默认 1800；0/负数=永不关停（旧 bug：0→立刻停）。"""
        with mock.patch.dict(os.environ, {"QRA_PY_IDLE": ""}):
            secs, enabled = Q._idle_cfg()
            self.assertEqual(secs, 1800.0)
            self.assertTrue(enabled)
        with mock.patch.dict(os.environ, {"QRA_PY_IDLE": "0"}):
            secs, enabled = Q._idle_cfg()
            self.assertEqual(secs, 0.0)
            self.assertFalse(enabled)
        with mock.patch.dict(os.environ, {"QRA_PY_IDLE": "-1"}):
            _, enabled = Q._idle_cfg()
            self.assertFalse(enabled)
        with mock.patch.dict(os.environ, {"QRA_PY_IDLE": "300"}):
            secs, enabled = Q._idle_cfg()
            self.assertEqual(secs, 300.0)
            self.assertTrue(enabled)

    def test_idle_disabled_kernel_survives(self):
        """禁用空闲关停（QRA_PY_IDLE≤0 的模块态）：内核越过旧 bug 的
        首 tick 即杀窗口仍存活，状态可继续复用。"""
        sid = "m_noidle"
        Q.IDLE_ENABLED = False
        try:
            e = Q._ensure_kernel(sid)
            Q._execute_with_retry(e, "no_idle = '长寿'")
            time.sleep(1.5)   # 5 个 tick（0.3s）；旧实现 0 哨兵在此早已被杀
            self.assertTrue(Q._kernel_alive(e), "禁用空闲关停后内核应存活")
            r = _call(sid, "no_idle")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"], "'长寿'")
        finally:
            Q.IDLE_ENABLED = True

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


# --- P2.5 prime 完全体：comm 桥 / harness / agent_message / qra 递归 ---

class _FakeHandle:
    model = "deepseek-v4-pro"


class _FakeLifecycle:
    """hermes subagent_lifecycle 的假实现（单元测试无父代理绑定）。"""

    def __init__(self):
        self.launched = []

    def launch(self, request):
        self.launched.append(request)
        return _FakeHandle()

    def status(self, handle):
        return type("S", (), {"state": type("St", (), {"value": "SUCCEEDED"})()})()

    def result(self, handle):
        return type(
            "R",
            (),
            {
                "ready": True,
                "summary": "子代理完成",
                "error_message": None,
                "error_classification": None,
            },
        )()

    def cancel(self, handle, *, reason):
        return None


class _FakeCtx:
    def __init__(self):
        # 实例属性而非类属性：每个测试独立的假 lifecycle（launched 计数不串）
        self.subagent_lifecycle = _FakeLifecycle()


class TestBridgePing(KernelTestCase):
    """host_request 真回环：内核 Comm → 宿主 dispatch → control 回执。"""

    def test_ping_roundtrip(self):
        r = _call("br_ping", "import qra_runtime\nawait qra_runtime.host_request('qra.ping')")
        self.assertTrue(r["ok"], r)
        self.assertIn("'pong': True", r["result"], r)
        self.assertIn("'sid': 'br_ping'", r["result"], r)

    def test_unknown_type_clean_error(self):
        r = _call("br_unk", "import qra_runtime\nawait qra_runtime.host_request('qra.nope')")
        self.assertFalse(r["ok"], r)
        self.assertIn("宿主未注册请求类型", r["error"], r)

    def test_ping_does_not_disturb_normal_exec(self):
        _call("br_mix", "import qra_runtime\nawait qra_runtime.host_request('qra.ping')")
        r = _call("br_mix", "mix_var = 42\nmix_var * 2")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], "84")


class TestBridgeHarness(KernelTestCase):
    """harness CRUD：内核侧文件店（QRA_HARNESS_STATE_DIR），跨重启持久。"""

    def test_harness_crud_roundtrip(self):
        code = (
            "import qra_runtime as q\n"
            "q.harness.create_memory('测试记忆', '量化参数 A=1', path='quant')\n"
            "q.harness.create_skill('回测工具', 'def bt(): ...', "
            "reference={'type': 'python', 'import': 'os', 'callable': 'os.getcwd'}, "
            "arguments={'window': 20})\n"
        )
        r = _call("br_harness", code)
        self.assertTrue(r["ok"], r)
        r = _call("br_harness", "import qra_runtime as q\nq.harness.list()")
        self.assertTrue(r["ok"], r)
        self.assertIn("测试记忆", r["result"], r)
        # 回测工具在截断点之后（测试 MAXOUT=200），改为文件层断言（见下）
        # 文件店确实落盘（会话目录 QRA_HARNESS_STATE_DIR）
        state_file = (
            Q._qra_python_dir() / "sessions" / "br_harness" / "harness" / "harness_state.json"
        )
        self.assertTrue(state_file.exists())
        data = json.loads(state_file.read_text())
        self.assertEqual(len(data["entries"]["memory"]), 1)
        self.assertEqual(len(data["entries"]["skill"]), 1)

    def test_harness_update_delete(self):
        r = _call("br_h2", "import qra_runtime as q\nq.harness.create_memory('旧', 'v1')")
        self.assertTrue(r["ok"], r)
        r = _call("br_h2", "import qra_runtime as q\nq.harness.update_memory('旧', '新', 'v2')")
        self.assertTrue(r["ok"], r)
        r = _call("br_h2", "import qra_runtime as q\nq.harness.get('memory', '旧').content")
        self.assertEqual(r["result"], "'v2'", r)
        r = _call("br_h2", "import qra_runtime as q\nq.harness.delete_memory('旧')")
        self.assertTrue(r["ok"], r)
        r = _call("br_h2", "import qra_runtime as q\nq.harness.list('memory')")
        self.assertEqual(r["result"], "[]", r)

    def test_harness_global_scope(self):
        # global_=True 落全局店（HERMES_HOME/qra_python/harness），跨会话可见
        r = _call("br_hg", "import qra_runtime as q\nq.harness.create_memory('全局', 'g1', global_=True)")
        self.assertTrue(r["ok"], r)
        # 全局店 = 内核侧 _agent_dir()/harness/；插件 spawn 时显式钉死
        # QRA_AGENT_DIR=$HERMES_HOME/qra_python（不依赖 HERMES_HOME 传播——
        # 不钉死时曾实测误落到真实 ~/.hermes，本断言守护该回归）
        gfile = Q._qra_python_dir() / "harness" / "harness_state.json"
        self.assertTrue(gfile.exists())
        r = _call("br_hg2", "import qra_runtime as q\nq.harness.get('memory', '全局', global_=True).content")
        self.assertEqual(r["result"], "'g1'", r)

    def test_skill_requires_python_reference(self):
        r = _call(
            "br_hs",
            "import qra_runtime as q\ntry:\n"
            "    q.harness.create_skill('坏技能', 'x', reference={'type': 'http'})\n"
            "except ValueError as e:\n"
            "    print('REJECTED', e)",
        )
        self.assertTrue(r["ok"], r)
        self.assertIn("REJECTED", r["stdout"], r)

    def test_harness_survives_kernel_revive(self):
        # 文件店跨内核复活：新内核 harness.list 仍见旧条目（快照不涉及它）
        sid = "br_hrevive"
        e = Q._ensure_kernel(sid)
        Q._execute_with_retry(e, "import qra_runtime as q\nq.harness.create_memory('存活', '跨重启')")
        Q._snapshot(e)
        os.kill(e.km.provisioner.process.pid, signal.SIGKILL)
        time.sleep(0.3)
        r = _call(sid, "import qra_runtime as q\nq.harness.get('memory', '存活').content")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], "'跨重启'")


class TestBridgeAgentMessage(KernelTestCase):
    def test_send_to_parent_writes_inbox(self):
        r = _call(
            "br_msg",
            "import qra_runtime.agent_message as am\n"
            "await am.send('来自内核的消息', receiver_role='parent')",
        )
        self.assertTrue(r["ok"], r)
        inbox = Q._qra_python_dir() / "sessions" / "br_msg" / "inbox" / "messages.jsonl"
        self.assertTrue(inbox.exists())
        row = json.loads(inbox.read_text().strip())
        self.assertEqual(row["to"], "parent")
        self.assertEqual(row["message"], "来自内核的消息")

    def test_send_invalid_role_clean_error(self):
        r = _call(
            "br_msg2",
            "import qra_runtime.agent_message as am\n"
            "await am.send('hi', receiver_role='nope')",
        )
        self.assertFalse(r["ok"], r)
        self.assertIn('receiver_role must be', r["error"], r)


class TestBridgeSubagents(KernelTestCase):
    """qra 递归：假 lifecycle 验证 dispatch 全链（真 hermes 在 e2e 冒烟）。"""

    def setUp(self):
        Q._CTX = _FakeCtx()

    def test_run_returns_spawn_handle_at_admission(self):
        r = _call("br_run", "import qra_runtime as q\nh = await q.run('帮我回测一下')\nh.qra_child_id")
        self.assertTrue(r["ok"], r)
        self.assertIn("br_run", Q._CHILDREN)
        self.assertEqual(len(Q._CHILDREN["br_run"]), 1)
        fake = Q._CTX.subagent_lifecycle
        self.assertEqual(len(fake.launched), 1)
        self.assertEqual(fake.launched[0].goal, "帮我回测一下")

    def test_list_subagents_and_result_poll(self):
        _call("br_list", "import qra_runtime as q\nawait q.run('子任务')")
        # 只投影 status 字段，避开 MAXOUT=200 的整对象截断
        r = _call(
            "br_list",
            "import qra_runtime as q\n[s.status for s in await q.list_subagents()]",
        )
        self.assertTrue(r["ok"], r)
        self.assertIn("'completed'", r["result"], r)
        r = _call(
            "br_list",
            "import qra_runtime as q\n"
            "subs = await q.list_subagents()\n"
            "res = await q.subagent_result(subs[0])\n"
            "res.status + '|' + (res.summary or '')",
        )
        self.assertTrue(r["ok"], r)
        self.assertIn("completed|子代理完成", r["result"], r)

    def test_delete_subagent_removes_from_registry(self):
        r = _call("br_del", "import qra_runtime as q\nh = await q.run('将被删除')\nh.qra_child_id")
        self.assertTrue(r["ok"], r)
        cid = r["result"].strip("'\"")
        r = _call("br_del", f"import qra_runtime as q\nawait q.delete_subagent('{cid}')")
        self.assertTrue(r["ok"], r)
        self.assertNotIn(cid, Q._CHILDREN.get("br_del", {}))

    def test_subagent_result_unknown_clean_error(self):
        r = _call("br_unk2", "import qra_runtime as q\nawait q.subagent_result('不存在')")
        self.assertFalse(r["ok"], r)
        self.assertIn("未知子代理", r["error"], r)

    def test_run_without_ctx_clean_error(self):
        Q._CTX = None
        r = _call("br_noctx", "import qra_runtime as q\nawait q.run('无父代理')")
        self.assertFalse(r["ok"], r)
        self.assertIn("插件未注册", r["error"], r)

    def test_find_models(self):
        r = _call("br_models", "import qra_runtime as q\nawait q.find_models('opus')")
        self.assertTrue(r["ok"], r)
        self.assertIn("opus-4.7", r["result"], r)
        self.assertNotIn("deepseek", r["result"].replace("DeepSeek", ""), r)

    def test_agent_message_send_to_child_writes_child_inbox(self):
        r = _call("br_cmsg", "import qra_runtime as q\nh = await q.run('子代理')\nh.qra_child_id")
        self.assertTrue(r["ok"], r)
        cid = r["result"].strip("'\"")
        r = _call(
            "br_cmsg",
            f"import qra_runtime.agent_message as am\nawait am.send('给子代理', receiver_role='child', receiver_name='{cid}')",
        )
        self.assertTrue(r["ok"], r)
        rec = Q._CHILDREN["br_cmsg"][cid]
        inbox = rec.session_dir / "inbox" / "messages.jsonl"
        self.assertTrue(inbox.exists())
        row = json.loads(inbox.read_text().strip())
        self.assertEqual(row["to"], cid)
        self.assertEqual(row["message"], "给子代理")

    def test_broadcast_all_writes_parent_and_children(self):
        _call("br_bcast", "import qra_runtime as q\nawait q.run('子1')")
        r = _call(
            "br_bcast",
            "import qra_runtime.agent_message as am\n"
            "await am.send('all', broadcast_message='广播')",
        )
        self.assertTrue(r["ok"], r)
        parent_inbox = Q._qra_python_dir() / "sessions" / "br_bcast" / "inbox" / "messages.jsonl"
        self.assertTrue(parent_inbox.exists())
        rec = next(iter(Q._CHILDREN["br_bcast"].values()))
        self.assertTrue((rec.session_dir / "inbox" / "messages.jsonl").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
