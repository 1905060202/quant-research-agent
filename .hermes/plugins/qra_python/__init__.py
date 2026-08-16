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
P2.5（2026-08-16）prime 完全体移植：comm 桥（host_request）+ harness CRUD
（内核侧文件店）+ agent_message 收件箱 + qra.run 递归子代理（admission
语义，经 hermes ctx.subagent_lifecycle——hermes 子代理不自报结果，QRA
增加 qra.subagent_result 轮询）。内核侧运行时在 qra_runtime/ 包
（prime-agent-runtime src/rlm 的移植），bootstrap 注入 sys.path 后 import。
C 级不迁移：raw ZMQ 协议（QRA 用 jupyter_client，D007 规定）、forkserver
（macOS 不可用）、mcp_base（P3，QRA 无 MCP 集成需求）、内核沙箱
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
import uuid
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
_skip = {{'rlm', 'asyncio', 'In', 'Out', 'get_ipython', 'exit', 'quit', 'open', 'display', 'dill', 'qra_runtime'}}
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
# 内核 bootstrap（P2.5 prime 完全体）：挂载 qra_runtime 包 + 注入 _qra_save。
# 与 prime 的对齐点：bootstrap 只装「宿主维护的名字」，工作流函数留给模型
# 自建（harness 店里的 skill 就是为此设计的）。恢复顺序契约（prime）：
# qra_runtime 是插件维护的名字——模块对象由 dill 按引用序列化，但在
# _skip 名单里（见 _SNAPSHOT_TPL），快照恢复只覆盖 payload 名单、不删
# bootstrap 注入的绑定，所以复活后无需重注入（_spawn 新内核本来就会
# 重新 bootstrap，restore 在其后执行）。
# _qra_save() 保留：模型算完关键状态后主动请求落盘，绕过 15s 防抖+30s
# 最小间隔。宿主 debounce 线程每 tick 查 workspace 里的请求文件（共享
# 文件系统=零 IPC 的请求通道），见 _debounce_loop。
_BOOTSTRAP_TPL = """\
import builtins as _b
import os as _os
import sys as _sys
_runtime_path = _os.environ.get('QRA_RUNTIME_PATH')
if _runtime_path and _runtime_path not in _sys.path:
    _sys.path.insert(0, _runtime_path)
import qra_runtime  # noqa
import qra_runtime.agent_message as _agent_message  # noqa
def _qra_save():
    _b.open('.qra_save_request', 'w').write('1')
"""

# 内核运行时包的**父目录**（插件目录）——bootstrap 把它插进内核 sys.path，
# import qra_runtime 才能解析（sys.path 条目须是包的外层目录，不是包本身）
QRA_RUNTIME_PATH = str(Path(__file__).resolve().parent)

# qra.find_models 的模型表（QRA 双路由：deepseek 直连 / opus 本地代理）。
# qra.run 的 model kwarg 直通 hermes 子代理 model 参数。
_MODELS = [
    {
        "provider": "deepseek",
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro（直连）",
        "selector": "deepseek",
    },
    {
        "provider": "proxy",
        "id": "opus-4.7",
        "name": "Opus 4.7（本地代理 :8789）",
        "selector": "opus",
    },
]

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

# --- comm 桥（P2.5）：PluginContext + 子代理注册表 ---

_CTX = None  # register() 时存 PluginContext；宿主侧 dispatch 全经它访问 hermes


@dataclass
class _ChildRec:
    """内核侧 qra_child_id ↔ hermes SubagentHandle 的映射记录。"""

    qra_child_id: str
    handle: object  # hermes SubagentHandle（不可变快照，可序列化）
    name: str
    model: str
    session_dir: Path  # QRA 侧子代理目录（inbox 等落这里）
    created_at: float = field(default_factory=time.time)


_CHILDREN: dict[str, dict[str, _ChildRec]] = {}  # sid -> qra_child_id -> rec


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

def _session_dir(sid: str) -> Path:
    """会话目录（harness 文件店 + inbox + 子代理目录的根）。"""
    d = _qra_python_dir() / "sessions" / sid
    for sub in ("harness", "inbox"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def _spawn(sid: str) -> _KernelEntry:
    _ensure_kernelspec()
    session_dir = _session_dir(sid)
    # 每内核 env（prime 同款「fork 后按会话解析」——QRA 无 forkserver，
    # 直接 spawn 时注入）：harness 文件店/收件箱路径、运行时包路径。
    # env 显式并上 os.environ（实测 jupyter_client 传入 env 时 HERMES_HOME
    # 不进内核——harness 全局店曾误落到真实 ~/.hermes，测试抓出来的）。
    # QRA_AGENT_DIR 显式钉死：harness 的 _agent_dir() 优先读它，全局店
    # 固定落在 $HERMES_HOME/qra_python/harness，不依赖 HERMES_HOME 传播。
    km, kc = start_new_kernel(
        kernel_name=KERNELSPEC_NAME,
        cwd=str(_qra_python_dir() / "workspace"),
        env={
            **os.environ,
            "QRA_KERNEL_SID": sid,
            "QRA_SESSION_DIR": str(session_dir),
            "QRA_HARNESS_STATE_DIR": str(session_dir / "harness"),
            "QRA_INBOX_DIR": str(session_dir / "inbox"),
            "QRA_RUNTIME_PATH": QRA_RUNTIME_PATH,
            "QRA_AGENT_DIR": str(_qra_python_dir()),
        },
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
    _bootstrap(entry)
    return entry


def _bootstrap(entry: _KernelEntry) -> None:
    """注入内核运行时（prime 完全体）：qra_runtime 包 + _qra_save 兜底。

    qra_runtime（host_request 桥/harness CRUD/agent_message/qra 递归）在
    _skip 名单里，不随快照走；_qra_save() 是主动落盘请求通道，绕过
    15s 防抖+30s 最小间隔（默认窗口下崩溃最多丢 45s 的活儿，调用它
    收敛到 ≈1 个 tick）。spawn 时注入一次即可，复活路径的新内核也走
    _spawn，天然覆盖（restore 在其后执行，只覆盖 payload 名单）。
    """
    _execute(entry, _BOOTSTRAP_TPL, timeout=30, record=False)


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
    如实告知模型（恢复了什么/失败了什么）。首启不置该标志——全新内核
    报「曾死亡」是撒谎（诚实铁律）。
    """
    with _LOCK:
        was_dead = False
        e = _KERNELS.get(sid)
        if e is not None:
            if _kernel_alive(e):
                e.last_exec = time.monotonic()
                return e
            was_dead = True
            _shutdown(e)
            _KERNELS.pop(sid, None)
        _start_debounce_thread()
        _evict_lru_locked(keep_sid=sid)
        e = _spawn(sid)
        e.restored = _restore_from_snapshot(e)
        e.revived_after_death = was_dead  # 只有真死过才如实告知「曾死亡」
        _KERNELS[sid] = e
        return e


def _restore_from_snapshot(e: _KernelEntry) -> bool:
    """有快照文件就回注 globals()（resume 复活路径），逐名容错。

    恢复顺序契约（prime）：restore 先于一切、逐名容错。QRA 的 bootstrap
    注入层只有 qra_runtime（模块对象在 _skip 名单里，快照不含它；restore
    只覆盖 payload 名单、不删本绑定），所以 restore 后无需重装。
    _spawn 流程保证新内核先 bootstrap 后 restore，顺序天然正确。
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
            # comm 消息（内核 host_request）没有 parent_header——msg_id 过滤
            # 会把它们丢掉。先路由，再走执行结果判定（P2.5 comm 桥）。
            if msg["msg_type"] in ("comm_open", "comm_msg", "comm_close"):
                _handle_comm(entry, msg)
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
                if msg["msg_type"] in ("comm_open", "comm_msg", "comm_close"):
                    _handle_comm(entry, msg)
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


# --- comm 桥（P2.5 prime 完全体）：宿主侧 host_request 分发 ---
#
# 协议（prime host_request 的 QRA 移植）：内核 Comm(target="qra.host.request")
# 发出 comm_open（data={**payload, "type": <请求类型>}，type 永远最后覆盖防
# 劫持）→ 宿主在 _execute 的 iopub drain 里拦截（无 parent_header）→
# dispatch 表分发 → **control channel** 回执 comm_msg（shell busy 时 control
# 线程仍送达；ipykernel ControlThread 恒启动且 comm_msg 原生在
# control_msg_types 里，已对 .venv-v7 ipykernel 6.29 源码核实）→ 内核侧
# future resolve。回执契约：{"status": "ok", ...} 或 {"status": "error",
# "error": "..."}，error 状态内核侧 host_request 抛 RuntimeError。
# 每次 host_request 用新 comm，宿主无需跟踪 comm_id 之外的状态。

_HERMES_STATUS_RUNNING = ("PENDING", "STARTING", "RUNNING", "CANCEL_REQUESTED")
_HERMES_STATUS_COMPLETED = ("SUCCEEDED",)
_HERMES_STATUS_ERROR = ("FAILED", "INTERRUPTED", "CANCELLED", "UNKNOWN")


def _handle_comm(entry: _KernelEntry, msg: dict) -> None:
    """路由一条内核→宿主的 comm 消息（iopub，无 parent_header）。"""
    mt = msg["msg_type"]
    if mt == "comm_close":
        return  # 内核侧已自行结束；每次 host_request 新 comm，无需清理
    if mt not in ("comm_open", "comm_msg"):
        return
    content = msg.get("content") or {}
    comm_id = content.get("comm_id")
    data = content.get("data")
    if not isinstance(comm_id, str) or not comm_id or not isinstance(data, dict):
        return
    reply = _dispatch_host_request(entry, data)
    _send_comm_reply(entry, comm_id, reply)


def _dispatch_host_request(entry: _KernelEntry, data: dict) -> dict:
    """按 data["type"] 分发；handler 任何异常都转 error 回执，绝不抛回 drain 循环。"""
    rtype = data.get("type")
    if not isinstance(rtype, str) or not rtype:
        return {"status": "error", "error": "host_request 缺少 type 字段"}
    handler = _HOST_HANDLERS.get(rtype)
    if handler is None:
        return {"status": "error", "error": f"宿主未注册请求类型 {rtype!r}"}
    try:
        return handler(entry, data)
    except Exception as e:  # 兜底：dispatch 内部异常不让 cell 悬死
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _send_comm_reply(entry: _KernelEntry, comm_id: str, reply: dict) -> None:
    """control channel 回执（prime 同款；发送失败时 cell 会等到自己的超时）。"""
    try:
        msg = entry.kc.session.msg(
            "comm_msg", content={"comm_id": comm_id, "data": reply}
        )
        entry.kc.control_channel.send(msg)
    except Exception:
        pass


def _child_rec(entry: _KernelEntry, qra_child_id: str) -> _ChildRec | None:
    return _CHILDREN.get(entry.sid, {}).get(qra_child_id)


def _hermes_state_to_qra(status: str) -> str:
    """SubagentState（hermes）→ 内核侧 QraSubagent.status。"""
    if status in _HERMES_STATUS_RUNNING:
        return "running"
    if status in _HERMES_STATUS_COMPLETED:
        return "completed"
    return "error"


def _subagent_entry_payload(rec: _ChildRec) -> dict:
    """QraSubagent 载荷（list_subagents/delete_subagent 回执共用）。"""
    state = "UNKNOWN"
    if _CTX is not None:
        try:
            state = str(_CTX.subagent_lifecycle.status(rec.handle).state.value)
        except Exception:
            pass
    return {
        "qra_child_id": rec.qra_child_id,
        "active_session_id": None,
        "session_id": None,
        "session_name": rec.name,
        "session_dir": str(rec.session_dir),
        "status": _hermes_state_to_qra(state),
    }


def _h_ping(entry: _KernelEntry, data: dict) -> dict:
    return {"status": "ok", "sid": entry.sid, "pong": True}


def _h_run(entry: _KernelEntry, data: dict) -> dict:
    """qra.run：经 hermes subagent_lifecycle 派生子代理，admission 即返回。

    约束（hermes 公共契约）：goal ≤16000 字符、role ∈ {leaf, orchestrator}、
    不支持 per-launch timeout/working_directory/blocked_tools。父代理绑定在
    hermes turn 级 ContextVar，tool 线程经 propagate_context_to_thread 继承
    （agent/tool_executor.py:1186 已核实）——所以工具调用内 launch 可见父
    代理。单元测试无父代理时得到干净 error 回执。
    """
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"status": "error", "error": "prompt 必须是非空字符串"}
    if _CTX is None:
        return {"status": "error", "error": "插件未注册（无 PluginContext），无法派生子代理"}
    kwargs = data.get("kwargs") or {}
    if not isinstance(kwargs, dict):
        kwargs = {}
    model = kwargs.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        return {"status": "error", "error": "model kwarg 必须是非空字符串（或省略继承父模型）"}
    from agent.subagent_lifecycle import SubagentLaunchRequest

    qra_child_id = uuid.uuid4().hex
    name = "".join(ch if ch.isalnum() else "_" for ch in prompt.strip()[:60]).strip("_")
    name = name[:80] or "subagent"
    child_dir = _session_dir(entry.sid) / "children" / qra_child_id
    (child_dir / "inbox").mkdir(parents=True, exist_ok=True)
    request = SubagentLaunchRequest(
        goal=prompt[:16000],
        role="leaf",
        model=model or None,  # 直通 hermes 子代理 model；None=继承父模型
        metadata={
            "origin": "qra_python",
            "parent_kernel_sid": entry.sid,
            "name": name,
        },
    )
    handle = _CTX.subagent_lifecycle.launch(request)
    rec = _ChildRec(
        qra_child_id=qra_child_id,
        handle=handle,
        name=name,
        model=str(getattr(handle, "model", None) or model or "inherit"),
        session_dir=child_dir,
    )
    _CHILDREN.setdefault(entry.sid, {})[qra_child_id] = rec
    return {
        "status": "ok",
        "qra_child_id": qra_child_id,
        "name": name,
        "session_dir": str(child_dir),
        "model": rec.model,
    }


def _h_list_subagents(entry: _KernelEntry, data: dict) -> dict:
    return {
        "status": "ok",
        "subagents": [
            _subagent_entry_payload(rec)
            for rec in _CHILDREN.get(entry.sid, {}).values()
        ],
    }


def _h_subagent_result(entry: _KernelEntry, data: dict) -> dict:
    """qra.subagent_result（QRA 增强）：非阻塞轮询子代理最近已知状态。

    hermes 子代理不自报结果（prime 子代理会 agent_message.send 给 parent），
    所以内核侧需要轮询。本 handler 即时返回，绝不阻塞等待完成。
    """
    target = data.get("target")
    if not isinstance(target, str) or not target.strip():
        return {"status": "error", "error": "target 必须是非空字符串"}
    rec = _child_rec(entry, target.strip())
    if rec is None:
        return {
            "status": "error",
            "error": f"未知子代理 {target!r}（用 qra.list_subagents() 找回句柄）",
        }
    state = "UNKNOWN"
    summary = None
    error = None
    if _CTX is not None:
        svc = _CTX.subagent_lifecycle
        try:
            status = svc.status(rec.handle)
            state = str(status.state.value)
        except Exception:
            pass
        try:
            result = svc.result(rec.handle)
            if getattr(result, "ready", False):
                summary = result.summary
                error = result.error_message
                if result.error_classification == "UNKNOWN_HANDLE":
                    error = error or "UNKNOWN_HANDLE"
        except Exception:
            pass
    qra_status = _hermes_state_to_qra(state)
    if qra_status == "error" and error is None:
        error = f"hermes 子代理终态 {state}"
    return {
        "status": "ok",
        "result": {
            "qra_child_id": rec.qra_child_id,
            "status": qra_status,
            "summary": summary,
            "error": error,
        },
    }


def _h_delete_subagent(entry: _KernelEntry, data: dict) -> dict:
    target = data.get("target")
    if not isinstance(target, str) or not target.strip():
        return {"status": "error", "error": "target 必须是非空字符串"}
    rec = _child_rec(entry, target.strip())
    if rec is None:
        return {
            "status": "error",
            "error": f"未知子代理 {target!r}（用 qra.list_subagents() 找回句柄）",
        }
    # best-effort 取消（hermes 可能拒绝/已终态），注册表必然移除——delete
    # 的语义是「停止跟踪」，取消失败不阻塞删除
    if _CTX is not None:
        try:
            _CTX.subagent_lifecycle.cancel(rec.handle, reason="内核请求删除")
        except Exception:
            pass
    _CHILDREN.get(entry.sid, {}).pop(rec.qra_child_id, None)
    return {"status": "ok", "subagent": _subagent_entry_payload(rec)}


def _h_find_models(entry: _KernelEntry, data: dict) -> dict:
    query = data.get("query")
    limit = data.get("limit")
    if not isinstance(query, str):
        query = ""
    if not isinstance(limit, int) or limit < 1:
        limit = 8
    q = query.strip().lower()
    models = [
        m
        for m in _MODELS
        if not q
        or q in m["selector"].lower()
        or q in m["name"].lower()
        or q in m["id"].lower()
    ]
    return {"status": "ok", "models": models[:limit]}


def _inbox_append(path: Path, rec: dict) -> None:
    """收件箱追加（QRA 实现：文件即队列；读取由内核侧自行 glob）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _h_agent_message_send(entry: _KernelEntry, data: dict) -> dict:
    """agent_message.send：写接收方收件箱文件（prime 中由 TS daemon 路由）。

    P1 语义：receiver_role=parent → 本内核父收件箱；=child 需 receiver_name
    = 本内核注册的子代理 qra_child_id → 子代理收件箱；broadcast（target=
    "all"）→ 父 + 全部子代理。conversation 级投递（注入下一轮模型上下文）
    是 P3。
    """
    receipts = []
    if data.get("target") == "all":
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            return {"status": "error", "error": "broadcast 的 message 必须是非空字符串"}
        envelope = {
            "ts": time.time(),
            "from": entry.sid,
            "to": "all",
            "message": message,
        }
        parent_inbox = _session_dir(entry.sid) / "inbox" / "messages.jsonl"
        _inbox_append(parent_inbox, envelope)
        receipts.append({"target": "parent", "deliveryStatus": "queued"})
        for rec in _CHILDREN.get(entry.sid, {}).values():
            _inbox_append(rec.session_dir / "inbox" / "messages.jsonl", envelope)
            receipts.append(
                {"target": f"child:{rec.qra_child_id}", "deliveryStatus": "queued"}
            )
        return {"status": "ok", "receipts": receipts}

    message = data.get("message")
    receiver_role = data.get("receiver_role")
    receiver_name = data.get("receiver_name")
    if not isinstance(message, str):
        return {"status": "error", "error": "message 必须是非空字符串"}
    if receiver_role == "parent":
        inbox = _session_dir(entry.sid) / "inbox" / "messages.jsonl"
        _inbox_append(
            inbox,
            {"ts": time.time(), "from": entry.sid, "to": "parent", "message": message},
        )
        return {
            "status": "ok",
            "receipts": [{"target": "parent", "deliveryStatus": "queued"}],
        }
    if receiver_role == "child":
        if not isinstance(receiver_name, str) or not receiver_name.strip():
            return {"status": "error", "error": "child 消息必须带 receiver_name（qra_child_id）"}
        rec = _child_rec(entry, receiver_name.strip())
        if rec is None:
            return {
                "status": "error",
                "error": f"未知子代理 {receiver_name!r}（用 qra.list_subagents() 找回句柄）",
            }
        _inbox_append(
            rec.session_dir / "inbox" / "messages.jsonl",
            {
                "ts": time.time(),
                "from": entry.sid,
                "to": receiver_name,
                "message": message,
            },
        )
        return {
            "status": "ok",
            "receipts": [
                {"target": f"child:{receiver_name}", "deliveryStatus": "queued"}
            ],
        }
    return {
        "status": "error",
        "error": f"receiver_role 必须是 parent/child（或 target=all 广播），got {receiver_role!r}",
    }


def _h_agent_message_list_agents(entry: _KernelEntry, data: dict) -> dict:
    """agent_message.list_agents：parent/siblings/children 家庭名单。

    QRA 单层树：parent 是宿主 hermes 会话；siblings 恒空（QRA 不跟踪兄弟）；
    children 来自本内核注册表（含已终态、注册表保留到删除或进程退出）。
    """
    children = [
        {
            "qra_child_id": rec.qra_child_id,
            "name": rec.name,
            "session_dir": str(rec.session_dir),
            "status": _subagent_entry_payload(rec)["status"],
        }
        for rec in _CHILDREN.get(entry.sid, {}).values()
    ]
    return {
        "status": "ok",
        "parent": {"session_id": entry.sid},
        "siblings": [],
        "children": children,
    }


# dispatch 表（type → handler；type 由内核侧 host_request 最后覆盖，防劫持）
_HOST_HANDLERS = {
    "qra.ping": _h_ping,
    "qra.run": _h_run,
    "qra.list_subagents": _h_list_subagents,
    "qra.subagent_result": _h_subagent_result,
    "qra.delete_subagent": _h_delete_subagent,
    "qra.find_models": _h_find_models,
    "agent_message.send": _h_agent_message_send,
    "agent_message.list_agents": _h_agent_message_list_agents,
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
    _CHILDREN.clear()


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
    _CHILDREN.clear()
    global _DEBOUNCE_THREAD, _KERNELSPEC_READY, _CTX
    _DEBOUNCE_THREAD = None
    _KERNELSPEC_READY = False
    _CTX = None
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
        "ok/error/stdout/stderr/result。"
        "内核里已预装 qra_runtime 运行时（prime 完全体）："
        "await qra_runtime(\"子任务提示\") 派生子代理（admission 即返回句柄，"
        "结果用 await qra_runtime.subagent_result(handle) 轮询，"
        "qra_runtime.list_subagents() 找回句柄）；qra_runtime.harness 是持久"
        "CRUD 店（create_memory/update_memory/delete_memory/create_skill/"
        "update_skill/delete_skill/create_subagent/update_subagent/"
        "delete_subagent/create_prompt_note/update_prompt_note/"
        "delete_prompt_note/record_refinement/overview，global_=True 跨会话持久）；"
        "qra_runtime.agent_message.send(message, receiver_role='parent') 发消息"
        "（收件箱在 $QRA_INBOX_DIR，可用 glob 读取）；qra_runtime.find_models() "
        "查可用模型；_qra_save() 请求立即快照落盘。",
    ),
]


def register(ctx) -> None:
    """插件入口：被 PluginManager 在 plugins.enabled 命中时调用一次。"""
    global _CTX
    _CTX = ctx  # comm 桥 dispatch 经此访问 subagent_lifecycle（launch/status/cancel）
    # dsh 精华：fail-loud 启动自检——kernelspec 缺失/损坏在启动即暴露，
    # 不留到第一次工具调用才炸（本会话实测：pip install ipykernel 不注册
    # kernelspec、解释器漂移都是真实发生过的故障模式）。register 抛异常
    # hermes 会把插件标记为错误并响亮记录（plugins.py:4697 隔离策略）。
    _ensure_kernelspec()
    for name, toolset, schema, handler, emoji, description in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            description=description,
            emoji=emoji,
        )
