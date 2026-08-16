"""QRA Python 持久内核工具（D007 P2）——全生命周期计算底座。

给模型一个会话级 Jupyter 内核：每轮调用 qra_python 执行一段 Python，
变量和函数在内核进程里跨调用存活。quant 场景用它算指标、跑回测；
通用场景用它写可复用函数、做数据处理、跑实验——内核是「活的数值状态」，
文件系统是「跨会话记忆」，两者分工（prime 实证：DeepSeek V4 Pro 26 小时
1229 次工具调用全部走单一内核，零重启）。

设计依据：D007 ADR + prime-agent 源码深挖（github.com/PrimeIntellect-ai/
prime-agent，packages/coding-agent/src/core/kernel/）。A 级直接迁移的机制：
逐变量 dill 快照（单变量失败跳过不炸全量）+ 256MiB 上限 + tmp/os.replace
原子替换 + marker-line 结果协议 + 防遮蔽 builtins 别名（_b.open 等）+
恢复顺序契约（restore 先于一切、逐名容错）+ 恢复名单注入模型上下文 +
busy-interrupt 500ms 重发×5s 宽限 + dispose 前最终 flush（5s 上限）。
B 级改造迁移：死内核 poll 检测 + 重启重试（prime 无自动重启，QRA 无人
值守长任务是刚需，这是增强）+ 空闲关停/LRU（prime 单会话无池化需求）。
C 级不迁移：raw ZMQ 协议（QRA 用 jupyter_client，D007 规定）、forkserver
（macOS 不可用）、comm 桥/rlm 递归子代理（P3 再评估）、内核沙箱
（prime 自己都没做，QRA 已有 terminal/execute_code，风险记录于 D007）。

安全边界（诚实声明）：内核运行在宿主用户权限下，工作目录隔离在
``$HERMES_HOME/qra_python/workspace/``，不是沙箱——与 prime 一致
（prime README 明示 "not a security sandbox"）。
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import zmq  # jupyter_client 的传递依赖，用于 SNDTIMEO 兜底

# --- 依赖声明（宿主进程只需要 jupyter_client；dill 在内核进程内用） ---
try:
    from jupyter_client.kernelspec import KernelSpecManager, NoSuchKernel
    from jupyter_client.manager import start_new_kernel
except ImportError:  # 理论不可达（.venv-v7 已装），防御性提示
    raise ImportError(
        "qra_python 插件需要 jupyter_client：uv pip install jupyter_client "
        "ipykernel dill --python .venv-v7/bin/python"
    ) from None

# --- 配置（env 可覆盖，测试用 QRA_PY_* 压缩等待时间） ---
EXEC_TIMEOUT_S = float(os.environ.get("QRA_PY_TIMEOUT", "60") or 60)
MAX_OUTPUT_CHARS = int(os.environ.get("QRA_PY_MAXOUT", "4000") or 4000)
MAX_CODE_CHARS = int(os.environ.get("QRA_PY_MAXCODE", "16000") or 16000)
SNAPSHOT_DEBOUNCE_S = float(os.environ.get("QRA_PY_DEBOUNCE", "15") or 15)
SNAPSHOT_MIN_INTERVAL_S = float(os.environ.get("QRA_PY_MIN_INTERVAL", "30") or 30)
IDLE_SHUTDOWN_S = float(os.environ.get("QRA_PY_IDLE", "1800") or 1800)
DEBOUNCE_TICK_S = float(os.environ.get("QRA_PY_TICK", "5") or 5)
MAX_LIVE_KERNELS = int(os.environ.get("QRA_PY_MAXLIVE", "2") or 2)
MAX_SNAPSHOT_BYTES = int(os.environ.get("QRA_PY_MAXSNAP", str(256 << 20)) or 256 << 20)
# prime 同款 busy 参数：中断后每 500ms 重发、5s 宽限耗尽即判死（走重启路径）
INTERRUPT_DRAIN_S = 5.0
REINTERRUPT_INTERVAL_S = 0.5

KERNELSPEC_NAME = "qra_python"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")

# 快照模板（prime 语义：逐变量独立 dumps，单变量失败跳过不炸全量）。
# 防遮蔽：所有 builtin 走 import builtins as _b，用户把 open/print/len
# 遮蔽了也不破坏快照路径。快照时过滤（垃圾名从不进 payload），
# restore 侧不再依赖过滤。结果走 marker-line 协议（stdout 最后一行的
# __QRA_KERNEL_STATE__+JSON），比 execute_result 更抗用户 print 干扰。
_SNAPSHOT_TPL = """\
import builtins as _b
import dill, json, time
_skip = {{'rlm', 'asyncio', 'In', 'Out', 'get_ipython', 'exit', 'quit', 'open', 'display', 'dill'}}
_payload = {{}}
_skipped = []
_saved = 0
for _name, _val in _b.list(_b.globals().items()):
    if _name.startswith('_') or _name in _skip:
        continue
    try:
        _blob = dill.dumps(_val)
    except Exception:
        _skipped.append(_name)
        continue
    _saved += _b.len(_blob)
    if _saved > {max_bytes}:
        _skipped.append(_name)
        _saved -= _b.len(_blob)
        continue
    _payload[_name] = _blob
_meta = {{'saved_at': time.time(), 'exec_count': {exec_count}}}
_ok = True
try:
    with _b.open({tmp!r}, 'wb') as _f:
        dill.dump({{'_payload': _payload, 'meta': _meta}}, _f)
except Exception:
    _ok = False
_b.print('__QRA_KERNEL_STATE__' + json.dumps(
    {{'ok': _ok, 'saved': _b.len(_payload), 'bytes': _saved,
     'skipped': _skipped, 'saved_at': _meta['saved_at']}}))
"""
# 恢复模板（prime 语义：逐名 dill.loads 容错，失败进 failed 名单继续；
# 空/损坏文件 → 空恢复不抛）。同样走 marker-line 协议。
_RESTORE_TPL = """\
import builtins as _b
import dill, json
_data = None
try:
    _data = dill.load(_b.open({path!r}, 'rb'))
except Exception:
    _data = None
_restored = []
_failed = []
if _b.isinstance(_data, _b.dict) and '_payload' in _data:
    for _name, _blob in _data.get('_payload', {{}}).items():
        if _name.startswith('_'):
            continue
        try:
            _b.globals()[_name] = dill.loads(_blob)
            _restored.append(_name)
        except Exception:
            _failed.append(_name)
_b.print('__QRA_KERNEL_RESTORE__' + json.dumps(
    {{'ok': _b.isinstance(_data, _b.dict) and '_payload' in _data,
     'restored': _restored, 'failed': _failed}}))
"""
# 内核自检：确认 ipykernel 认的是装了 dill 的解释器（kernelspec 指向错误
# python 时快照会静默失败——启动即暴露，不等到要恢复的那一刻）。
_PROBE_TPL = """\
try:
    import dill, ipykernel  # noqa
    _qra_probe_ok = True
except Exception as _e:  # noqa
    _qra_probe_ok = repr(_e)
"""

_MARKER_RE = re.compile(r"__QRA_KERNEL_(?:STATE|RESTORE)__(\{.*\})")


class _KernelDead(Exception):
    """内核进程死亡/通道断裂/中断后仍 busy，上层负责重启+快照恢复+重试。"""


def _hermes_home() -> Path:
    """HERMES_HOME 解析：env → ~/.hermes（照 qra_verify 模式）。"""
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(env) if env else Path.home() / ".hermes"


def _qra_python_dir() -> Path:
    d = _hermes_home() / "qra_python"
    for sub in ("kernel_state", "kernel_history", "workspace"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _clip(s: str, cap: int) -> tuple[str, int]:
    """截断输出，返回 (文本, 被截掉的字符数)。"""
    if len(s) <= cap:
        return s, 0
    return s[:cap], len(s) - cap


def _parse_marker(stdout: str, name: str) -> dict | None:
    """解析 marker-line 协议（取最后一次出现，防用户 print 撞车）。"""
    hits = re.findall(rf"__QRA_KERNEL_{name}__(\{{.*\}})", stdout)
    if not hits:
        return None
    try:
        return json.loads(hits[-1])
    except json.JSONDecodeError:
        return None


@dataclass
class _KernelEntry:
    sid: str
    km: object
    kc: object
    last_exec: float = field(default_factory=time.monotonic)
    last_snapshot: float = field(default_factory=time.monotonic)
    dirty: bool = False
    exec_count: int = 0
    restored: bool = False  # 一次性标志：下次执行结果里告诉模型状态复活过
    revived_after_death: bool = False  # 一次性标志：死内核被重建过（含无快照情形）
    restore_failed: list = field(default_factory=list)  # 恢复失败名单（告诉模型重建）

    @property
    def state_path(self) -> Path:
        return _qra_python_dir() / "kernel_state" / f"{self.sid}.dill"

    @property
    def manifest_path(self) -> Path:
        return _qra_python_dir() / "kernel_state" / f"{self.sid}.json"

    @property
    def history_path(self) -> Path:
        return _qra_python_dir() / "kernel_history" / f"{self.sid}.jsonl"


_LOCK = threading.RLock()
_KERNELS: dict[str, _KernelEntry] = {}
_DEBOUNCE_STOP = threading.Event()
_DEBOUNCE_THREAD: threading.Thread | None = None
_KERNELSPEC_READY = False


# --- kernelspec：锁定内核解释器 = sys.executable（装了 ipykernel+dill 的那个） ---

def _ensure_kernelspec() -> None:
    """懒安装 qra_python kernelspec（幂等；解释器漂移时重装）。

    直接 pip install ipykernel 不会注册 kernelspec；不注册则 start_new_kernel
    找不到 python3 内核。用独立名字避免与用户本机 Jupyter 配置冲突。
    """
    global _KERNELSPEC_READY
    if _KERNELSPEC_READY:
        return
    ksm = KernelSpecManager()
    argv_wanted = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    need_install = True
    try:
        spec = ksm.get_kernel_spec(KERNELSPEC_NAME)
        if spec.argv == argv_wanted:
            need_install = False
    except NoSuchKernel:
        pass
    if need_install:
        from jupyter_client.kernelspec import install_kernel_spec

        spec_dir = _qra_python_dir() / "kernelspec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "kernel.json").write_text(
            json.dumps(
                {
                    "argv": argv_wanted,
                    "display_name": "QRA Python（持久内核，venv-v7）",
                    "language": "python",
                    "env": {"NO_COLOR": "1", "PYTHONUNBUFFERED": "1"},
                },
                ensure_ascii=False,
            )
        )
        install_kernel_spec(
            str(spec_dir), kernel_name=KERNELSPEC_NAME, user=True, replace=True
        )
    _KERNELSPEC_READY = True


# --- 内核生命周期 ---

def _spawn(sid: str) -> _KernelEntry:
    _ensure_kernelspec()
    km, kc = start_new_kernel(
        kernel_name=KERNELSPEC_NAME,
        cwd=str(_qra_python_dir() / "workspace"),
    )
    # 发送侧超时兜底：进程死在 poll 与 send 之间的竞态窗口内时，
    # send 不再无限阻塞而是 10s 后抛错→_KernelDead→重启路径。
    # 只设 SNDTIMEO 不设 RCVTIMEO：接收线程空闲超时会自毁，不能动。
    for ch in (kc.shell_channel, kc.control_channel, kc.iopub_channel):
        try:
            ch.socket.setsockopt(zmq.SNDTIMEO, 10000)
        except Exception:
            pass
    entry = _KernelEntry(sid=sid, km=km, kc=kc)
    # 启动即自检：解释器不对（缺 dill）此刻暴露，别留到要恢复快照时
    probe = _execute(entry, _PROBE_TPL, timeout=30, record=False)
    if not probe["ok"]:
        _shutdown(entry)
        raise RuntimeError(f"qra_python 内核自检失败：{probe['error'][:200]}")
    return entry


def _shutdown(entry: _KernelEntry) -> None:
    """关停内核。now=False 走 shutdown_request 让 ipykernel 自行退出。"""
    try:
        entry.km.shutdown_kernel(now=False)
    except Exception:
        try:
            entry.km.shutdown_kernel(now=True)
        except Exception:
            pass
    try:
        entry.kc.stop_channels()
    except Exception:
        pass


def _kernel_alive(entry: _KernelEntry) -> bool:
    try:
        return bool(entry.km.is_alive())
    except Exception:
        return False


def _start_debounce_thread() -> None:
    global _DEBOUNCE_THREAD
    if _DEBOUNCE_THREAD is not None and _DEBOUNCE_THREAD.is_alive():
        return
    _DEBOUNCE_THREAD = threading.Thread(
        target=_debounce_loop, name="qra_python_debounce", daemon=True
    )
    _DEBOUNCE_THREAD.start()


def _debounce_loop() -> None:
    """快照 debounce + 空闲关停（tick 5s 默认）。

    快照条件三连：dirty（执行过新代码）且内核空闲≥DEBOUNCE 且距上次
    快照≥MIN_INTERVAL——每笔执行都序列化大状态会把内核拖死。
    参数与 prime 不同（prime 1500ms 无最小间隔）：QRA 内核常驻大
    DataFrames/回测状态，快照成本高，15s+30s 是刻意的权衡（代价是
    崩溃丢失窗口 ≤45s，dispose 前最终 flush 兜底收敛到 ≈0）。
    """
    while not _DEBOUNCE_STOP.wait(DEBOUNCE_TICK_S):
        with _LOCK:
            now = time.monotonic()
            for sid in list(_KERNELS):
                e = _KERNELS.get(sid)
                if e is None:
                    continue
                try:
                    if (
                        e.dirty
                        and now - e.last_exec >= SNAPSHOT_DEBOUNCE_S
                        and now - e.last_snapshot >= SNAPSHOT_MIN_INTERVAL_S
                    ):
                        _snapshot(e)
                        e.dirty = False
                        e.last_snapshot = time.monotonic()
                    if now - e.last_exec >= IDLE_SHUTDOWN_S:
                        if e.dirty:
                            _snapshot(e)
                        _shutdown(e)
                        _KERNELS.pop(sid, None)
                except Exception:  # 后台线程任何异常都不能带崩插件
                    pass


def _evict_lru_locked(keep_sid: str) -> None:
    """在锁内驱逐最久未用的内核（超 MAX_LIVE_KERNELS 时）。"""
    while len(_KERNELS) >= MAX_LIVE_KERNELS:
        candidates = {s: e for s, e in _KERNELS.items() if s != keep_sid}
        if not candidates:
            return
        lru_sid = min(candidates, key=lambda s: candidates[s].last_exec)
        lru = candidates[lru_sid]
        if lru.dirty:
            _snapshot(lru)
        _shutdown(lru)
        _KERNELS.pop(lru_sid, None)


def _ensure_kernel(sid: str) -> _KernelEntry:
    """取（或复活）sid 的内核：活着→复用；死了→重启+快照恢复。

    懒启动：首次调用才 spawn。复活时置一次性标志，下一次执行结果里
    如实告知模型（恢复了什么/失败了什么）。
    """
    with _LOCK:
        e = _KERNELS.get(sid)
        if e is not None:
            if _kernel_alive(e):
                e.last_exec = time.monotonic()
                return e
            _shutdown(e)
            _KERNELS.pop(sid, None)
        _start_debounce_thread()
        _evict_lru_locked(keep_sid=sid)
        e = _spawn(sid)
        e.restored = _restore_from_snapshot(e)
        e.revived_after_death = True  # 死内核重建：下一次执行结果里如实告知
        _KERNELS[sid] = e
        return e


def _restore_from_snapshot(e: _KernelEntry) -> bool:
    """有快照文件就回注 globals()（resume 复活路径），逐名容错。

    恢复顺序契约（prime）：restore 先于一切；QRA 无 bootstrap 注入层，
    所以本插件维护的名字（无）不参与——将来加内核内辅助函数时须加进
    _skip 名单并在 restore 后重装（prime 对 rlm/skills 的做法）。
    """
    if not e.state_path.exists():
        return False
    res = _execute(
        e, _RESTORE_TPL.format(path=str(e.state_path)), timeout=30, record=False
    )
    if not res["ok"]:
        return False
    marker = _parse_marker(res["stdout"], "RESTORE")
    if marker is None:
        return False
    e.restore_failed = list(marker.get("failed") or [])
    return bool(marker.get("ok") and marker.get("restored"))


def _snapshot(e: _KernelEntry, timeout: float | None = None) -> bool:
    """dill 快照落盘：逐变量 dump → tmp 写 → 大小检查 → 原子替换 → manifest。

    返回 False 只意味着这次快照失败（不抛）：dispose 兜底和 debounce
    都容忍失败，旧快照（如果有）继续兜底。
    """
    tmp = e.state_path.with_suffix(".tmp")
    code = _SNAPSHOT_TPL.format(
        tmp=str(tmp), max_bytes=MAX_SNAPSHOT_BYTES, exec_count=e.exec_count
    )
    t = timeout if timeout is not None else min(60, EXEC_TIMEOUT_S)
    res = _execute(e, code, timeout=t, record=False)
    if not res["ok"]:
        return False
    marker = _parse_marker(res["stdout"], "STATE")
    if marker is None or not marker.get("ok"):
        return False
    try:
        if tmp.exists() and tmp.stat().st_size <= MAX_SNAPSHOT_BYTES:
            os.replace(tmp, e.state_path)
        else:
            tmp.unlink(missing_ok=True)
            return False
    except OSError:
        return False
    _write_manifest(e, marker)
    return True


def _write_manifest(e: _KernelEntry, marker: dict) -> None:
    """伴生 JSON manifest（prime 同款）：调试/审计快照内容。失败不致命。"""
    try:
        e.manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sid": e.sid,
                    "saved": marker.get("saved"),
                    "skipped": marker.get("skipped", []),
                    "bytes": marker.get("bytes"),
                    "saved_at": marker.get("saved_at"),
                    "exec_count": e.exec_count,
                },
                ensure_ascii=False,
            )
        )
    except OSError:
        pass


# --- 执行核心 ---

def _execute(entry: _KernelEntry, code: str, timeout: float, record: bool) -> dict:
    """阻塞执行一段代码，收集 stdout/stderr/result/error 直到 idle。

    锁在此处统一获取（RLock 可重入）：任何调用方都不可能让 debounce 线程
    在「执行中」看到空闲窗口而关停内核——测试曾因直调不带锁触发该竞态。
    record=False 用于快照/恢复/自检这类内部代码（不进审计历史、不计执行数）。
    """
    with _LOCK:
        return _execute_locked(entry, code, timeout, record)


def _execute_locked(entry: _KernelEntry, code: str, timeout: float, record: bool) -> dict:
    """_execute 的锁内实现（BlockingKernelClient 非线程安全，全局串行化）。"""
    if not _kernel_alive(entry):
        # zmq send 对已死进程会无限阻塞（实测挂死 3 分钟），poll 前置拦截
        raise _KernelDead("内核进程已死亡")
    t0 = time.monotonic()
    try:
        # allow_stdin=False：input() 直接抛 StdinNotImplementedError，
        # 不悬等一条永远不会来的 stdin 应答（prime 同款）
        msg_id = entry.kc.execute(code, allow_stdin=False)
    except Exception as e:
        raise _KernelDead(f"execute 发送失败: {e}") from e

    outs: list[str] = []
    errs: list[str] = []
    results: list[str] = []
    displays: list[str] = []
    error: str = ""
    execution_count: int | None = None
    interrupted = False

    def _collect(msg: dict) -> bool:
        """处理一条 iopub 消息；返回 True=执行结束（idle）。"""
        nonlocal execution_count, error
        mt = msg["msg_type"]
        c = msg["content"]
        if mt == "stream":
            (outs if c.get("name") == "stdout" else errs).append(c.get("text", ""))
        elif mt == "execute_result":
            execution_count = c.get("execution_count")
            results.append(c.get("data", {}).get("text/plain", ""))
        elif mt == "display_data":
            displays.append(c.get("data", {}).get("text/plain", ""))
        elif mt == "error":
            error = _strip_ansi("\n".join(c.get("traceback", []) or []))
        elif mt == "status" and c.get("execution_state") == "idle":
            return True
        return False

    deadline = time.monotonic() + timeout
    last_alive_check = time.monotonic()
    try:
        # 阶段一：正常执行（idle 或超时出循环）
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                msg = entry.kc.get_iopub_msg(timeout=min(remain, 5))
            except queue.Empty:
                # 静默期间每 5s 探一次进程：执行中死亡（OOM 等）不用傻等超时
                if time.monotonic() - last_alive_check >= 5:
                    if not _kernel_alive(entry):
                        raise _KernelDead("内核进程在执行中死亡")
                    last_alive_check = time.monotonic()
                continue
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            if _collect(msg):
                break
        # 阶段二：超时 → interrupt + 500ms 重发 ×5s 宽限（prime busy 参数）
        if time.monotonic() >= deadline and not interrupted:
            interrupted = True
            try:
                entry.km.interrupt_kernel()
            except Exception:
                raise _KernelDead("内核中断信号发送失败")
            drain_deadline = time.monotonic() + INTERRUPT_DRAIN_S
            next_resend = time.monotonic() + REINTERRUPT_INTERVAL_S
            while True:
                remain = drain_deadline - time.monotonic()
                if remain <= 0:
                    raise _KernelDead("中断后内核仍 busy（5s 宽限耗尽）")
                try:
                    msg = entry.kc.get_iopub_msg(
                        timeout=min(remain, REINTERRUPT_INTERVAL_S)
                    )
                except queue.Empty:
                    if time.monotonic() >= next_resend:
                        try:
                            entry.km.interrupt_kernel()
                        except Exception:
                            raise _KernelDead("内核中断重发失败")
                        next_resend = time.monotonic() + REINTERRUPT_INTERVAL_S
                    continue
                if msg["parent_header"].get("msg_id") != msg_id:
                    continue
                if _collect(msg):
                    break
    except Exception as e:
        if isinstance(e, _KernelDead):
            raise
        raise _KernelDead(f"iopub 通道异常: {e}") from e

    # 权威判定：iopub 已完整消费，error 消息见过=执行失败。
    # reply 只做兜底——status 取自 reply 但要防 shell 队列错位读到旧 reply
    #（首次冒烟抓到的异常：raise 被报成 ok，traceback 全丢）。
    status = "ok"
    try:
        reply = entry.kc.get_shell_msg(timeout=10)
        if reply["parent_header"].get("msg_id") == msg_id:
            status = reply["content"].get("status", "error")
            if status == "error" and not error:
                tb = reply["content"].get("traceback") or []
                error = _strip_ansi("\n".join(tb)) or (
                    f"{reply['content'].get('ename', '?')}: "
                    f"{reply['content'].get('evalue', '?')}"
                )
    except Exception:
        pass  # reply 缺失不致命：iopub 证据已足够判定

    duration = time.monotonic() - t0
    ok = status == "ok" and not interrupted and not error
    if interrupted:
        # 超时消息打头，traceback 跟上——模型能看到代码卡在哪个位置
        loc = f"\n中断位置：\n{error}" if error else ""
        error = f"执行超过 {timeout}s，已被 interrupt_kernel 中断{loc}"

    out_text, out_clipped = _clip("".join(outs), MAX_OUTPUT_CHARS)
    err_text, err_clipped = _clip("".join(errs), MAX_OUTPUT_CHARS)
    res_text, res_clipped = _clip(
        "".join(results) or "".join(displays), MAX_OUTPUT_CHARS
    )
    clipped = out_clipped + err_clipped + res_clipped

    result: dict = {
        "ok": ok,
        "error": error if not ok else "",
        "stdout": _strip_ansi(out_text),
        "stderr": _strip_ansi(err_text),
        "result": _strip_ansi(res_text),
        "execution_count": execution_count,
        "duration_s": round(duration, 2),
    }
    if clipped:
        result["note"] = f"输出共截断 {clipped} 字符（上限 {MAX_OUTPUT_CHARS}/流）"

    if record:
        entry.exec_count += 1
        entry.dirty = True
        entry.last_exec = time.monotonic()
        # 复活信号统一在此消费（_ensure_kernel 与 retry 两条重建路径殊途同归）
        revived_note = ""
        if entry.revived_after_death:
            if entry.restored:
                revived_note = "内核曾死亡，已自动重启（已从快照恢复状态）"
                if entry.restore_failed:
                    names = ", ".join(entry.restore_failed[:5])
                    revived_note += f"；{len(entry.restore_failed)} 个变量恢复失败需重建：{names}"
            else:
                revived_note = "内核曾死亡，已自动重启（无快照，从空状态重跑）"
        if entry.restored:
            result["restored_from_snapshot"] = True
        if revived_note:
            result["note"] = (
                (result.get("note", "") + " ").strip() + revived_note
            ).strip()
        entry.restored = False  # 一次性：只告诉模型这一次
        entry.revived_after_death = False
        entry.restore_failed = []
        _append_history(entry, code, ok, duration)
    return result


def _append_history(entry: _KernelEntry, code: str, ok: bool, duration: float) -> None:
    """审计 jsonl（P3 JSONL 双轨的前置数据）。失败也不该影响主流程。"""
    try:
        with entry.history_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "exec": entry.exec_count,
                        "status": "ok" if ok else "error",
                        "duration_s": round(duration, 2),
                        "code": code,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def _execute_with_retry(e: _KernelEntry, code: str) -> dict:
    """执行 + 死内核重启重试一次（重启后自动从快照复活状态）。

    复活说明由 _ensure_kernel 置标志、_execute 消费（单一路径不重复）。
    只重试一次：忙碌死循环这类代码重跑也大概率再死，两次后响亮报错。
    """
    with _LOCK:
        try:
            return _execute(e, code, EXEC_TIMEOUT_S, record=True)
        except _KernelDead:
            pass
        # 内核死了：关停 → 重建 → 快照恢复 → 重试同一段代码
        _shutdown(e)
        _KERNELS.pop(e.sid, None)
        e2 = _ensure_kernel(e.sid)
        try:
            return _execute(e2, code, EXEC_TIMEOUT_S, record=True)
        except _KernelDead:
            return {
                "ok": False,
                "error": "内核进程死亡，重启后重试仍失败（可能被系统 OOM 或代码杀掉了内核）",
            }


# --- 工具 handler ---

def qra_python(args: dict, **_kw) -> str:
    """QRA Python 持久内核工具 handler（契约同 qra_quote：args dict + **_kw）。

    模型填 {"code": "..."}；session_id 从框架注入的 kwarg 取——内核按会话
    隔离，/resume 后第一次调用自动从 dill 快照复活变量。
    """
    if not isinstance(args, dict):
        return json.dumps(
            {"error": "参数必须是对象：{\"code\": \"...\"}"}, ensure_ascii=False
        )
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return json.dumps(
            {"error": "缺少 code 参数：要执行的 Python 代码字符串"}, ensure_ascii=False
        )
    if len(code) > MAX_CODE_CHARS:
        return json.dumps(
            {"error": f"code 超长（{len(code)}>{MAX_CODE_CHARS} 字符），请拆分成多次调用"},
            ensure_ascii=False,
        )
    raw_sid = str(_kw.get("session_id") or "default")
    sid = _SID_SAFE_RE.sub("_", raw_sid)[:120]
    try:
        e = _ensure_kernel(sid)
        result = _execute_with_retry(e, code)
    except Exception as e:  # 兜底：任何异常都不该让 agent 崩（quote.py 同款契约）
        return json.dumps(
            {"error": f"qra_python 内核异常：{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )
    return json.dumps(result, ensure_ascii=False)


# --- 清理 ---

def _shutdown_all() -> None:
    """atexit：dirty 内核落快照（5s 上限，prime dispose 同款兜底）后关停。

    尽力而为，绝不让退出卡住；超时的快照由 debounce 期间已落盘的副本兜底。
    """
    _DEBOUNCE_STOP.set()
    with _LOCK:
        for sid in list(_KERNELS):
            e = _KERNELS.get(sid)
            if e is None:
                continue
            try:
                if e.dirty:
                    _snapshot(e, timeout=5)
            except Exception:
                pass
            try:
                _shutdown(e)
            except Exception:
                pass
            _KERNELS.pop(sid, None)


def _reset_all() -> None:
    """测试专用：清空单例状态（关停所有内核、复位线程与标志）。"""
    _DEBOUNCE_STOP.set()
    with _LOCK:
        for e in list(_KERNELS.values()):
            try:
                _shutdown(e)
            except Exception:
                pass
        _KERNELS.clear()
    global _DEBOUNCE_THREAD, _KERNELSPEC_READY
    _DEBOUNCE_THREAD = None
    _KERNELSPEC_READY = False
    _DEBOUNCE_STOP.clear()


atexit.register(_shutdown_all)


PYTHON_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "要执行的 Python 代码。会话级持久内核：定义的变量/函数在后续"
                "qra_python 调用里仍然存在，可直接引用。适合算指标、跑回测、"
                "数据处理、把复用逻辑写成函数留着——任何需要连续计算的活。"
            ),
        }
    },
    "required": ["code"],
}

# (name, toolset, schema, handler, emoji, description)
_TOOLS = [
    (
        "qra_python",
        "qra",
        PYTHON_SCHEMA,
        qra_python,
        "🐍",
        "QRA Python 持久内核：在会话级 Jupyter 内核执行 Python 代码，变量跨调用存活。"
        "全生命周期计算底座：算指标/回测/数据处理/实验都行，把复用逻辑写成函数"
        "留着，下一轮调用直接引用之前定义的变量。工作目录 "
        "$HERMES_HOME/qra_python/workspace；状态自动 dill 快照，/resume 恢复会话"
        "后自动复活（复活名单会如实告知）。超时 60s 会被中断。返回 JSON："
        "ok/error/stdout/stderr/result。",
    ),
]


def register(ctx) -> None:
    """插件入口：被 PluginManager 在 plugins.enabled 命中时调用一次。"""
    for name, toolset, schema, handler, emoji, description in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            description=description,
            emoji=emoji,
        )
