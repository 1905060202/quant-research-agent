"""QRA 内核运行时（prime-agent-runtime src/rlm/__init__.py 的移植，v0.7.2@83a0f9f9）。

prime 的 rlm 是「内核侧 shim」：Python 技能在 IPython 内核里 await 这些函数，
TypeScript 宿主按 type 分发并回执。QRA 移植把宿主换成 hermes 插件
（.hermes/plugins/qra_python），机制一字不改：

- host_request：Jupyter Comm（target "qra.host.request"）→ 宿主 comm_msg 收到
  dispatch → 宿主在 **control channel** 上回执（shell 忙时 control 仍可达，
  见 _install_control_comm_handlers）。payload 的 "type" 键永远最后覆盖，
  防止 payload 劫持路由。
- qra.run()：admission 语义——子代理被宿主接纳即返回句柄，**永远不是**子代
  理的答案。结果经 qra.subagent_result() 轮询（QRA 增强，见下）或
  qra_runtime.agent_message 收件箱取得。
- harness：qra_runtime.harness 的每访问解析代理（见 harness.py）。
- mcp_base 不做移植（P3，QRA 无 MCP 集成需求）。

QRA 与 prime 的唯一语义差异：hermes 子代理不自报结果（prime 子代理会
agent_message.send 给 parent）。所以 QRA 增加 subagent_result()——非阻塞
轮询宿主注册表，返回该子代理的最近已知状态。父代理的用法是在 agent loop
的步骤之间轮询，不要在单个 cell 里长时间阻塞等待（cell 有 60s 上限）。

引用形态：本模块可调用——``await qra("子任务提示")`` 等价于
``await qra.run("子任务提示")``；``import qra_runtime`` 后整个模块也可调用。
"""
from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .harness import HarnessEntry, HarnessScope, HarnessState, RefinementEvent, get_harness_state

try:
    from ipykernel.comm import Comm
except Exception:  # pragma: no cover - depends on ipykernel version
    Comm = None  # type: ignore[assignment]

try:
    from IPython import get_ipython
except Exception:  # pragma: no cover - only available in kernels
    get_ipython = None  # type: ignore[assignment]

HOST_COMM_TARGET = "qra.host.request"


@dataclass(frozen=True)
class QraSpawnHandle:
    qra_child_id: str
    name: str
    session_dir: Path
    model: str


@dataclass(frozen=True)
class QraModel:
    provider: str
    id: str
    name: str
    selector: str


@dataclass(frozen=True)
class QraSubagent:
    qra_child_id: str
    active_session_id: str | None
    session_id: str | None
    session_name: str
    session_dir: Path
    status: str


@dataclass(frozen=True)
class QraSubagentResult:
    """qra.subagent_result() 的最近已知状态（QRA 增强：hermes 子代理不自报）。"""

    qra_child_id: str
    status: Literal["running", "completed", "error"]
    summary: str | None
    error: str | None


def _install_control_comm_handlers() -> None:
    """Let comm replies arrive on the control channel during an execute_request."""
    if get_ipython is None:
        return
    shell = get_ipython()
    kernel = getattr(shell, "kernel", None)
    comm_manager = getattr(kernel, "comm_manager", None)
    control_handlers = getattr(kernel, "control_handlers", None)
    if comm_manager is None or not isinstance(control_handlers, dict):
        return
    control_handlers.setdefault("comm_msg", comm_manager.comm_msg)
    control_handlers.setdefault("comm_close", comm_manager.comm_close)


def _spawn_handle_from_payload(payload: Any) -> QraSpawnHandle:
    if not isinstance(payload, dict):
        raise RuntimeError("qra.run returned an invalid spawn handle")
    child_id = payload.get("qra_child_id")
    name = payload.get("name")
    session_dir = payload.get("session_dir")
    model = payload.get("model")
    if not all(isinstance(value, str) and value for value in (child_id, name, session_dir, model)):
        raise RuntimeError("qra.run returned an invalid spawn handle")
    return QraSpawnHandle(
        qra_child_id=child_id,
        name=name,
        session_dir=Path(session_dir),
        model=model,
    )


async def host_request(request_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a typed request to the QRA host plugin and await its reply.

    This is the kernel side of the generic host bridge: kernel-side code calls
    ``await host_request("<type>", {...})`` and the host (hermes qra_python
    plugin) dispatches on the type. Raises RuntimeError when the host reports
    an error or when no handler for the type is registered in this session.
    """
    if not isinstance(request_type, str) or not request_type:
        raise TypeError("request_type must be a non-empty str")
    if payload is not None and not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict or None, got {type(payload).__name__}")
    if Comm is None:
        raise RuntimeError("Jupyter comm support is unavailable in this kernel")
    _install_control_comm_handlers()

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    comm = Comm(target_name=HOST_COMM_TARGET, primary=False)

    def _on_msg(msg: dict[str, Any]) -> None:
        content = msg.get("content", {})
        reply = content.get("data", {}) if isinstance(content, dict) else {}
        if not isinstance(reply, dict):
            return

        status = reply.get("status")
        if status == "ok":
            def _resolve_result() -> None:
                if not future.done():
                    future.set_result({k: v for k, v in reply.items() if k != "status"})
                    comm.close()

            loop.call_soon_threadsafe(_resolve_result)
            return
        if status == "error":
            message = reply.get("error") or f"host request {request_type} failed"
            def _resolve_error() -> None:
                if not future.done():
                    future.set_exception(RuntimeError(str(message)))
                    comm.close()

            loop.call_soon_threadsafe(_resolve_error)
            return

        unexpected = f"host request {request_type} returned unexpected status: {status!r}"
        def _resolve_unexpected() -> None:
            if not future.done():
                future.set_exception(RuntimeError(unexpected))
                comm.close()

        loop.call_soon_threadsafe(_resolve_unexpected)

    comm.on_msg(_on_msg)
    # request_type goes last so a payload "type" key cannot reroute the request.
    comm.open(data={**(payload or {}), "type": request_type})
    return await future


async def run(prompt: str, **kwargs: Any) -> QraSpawnHandle:
    """Spawn a recursive QRA child agent and return once its task is admitted.

    Admission 语义：宿主（hermes）接收启动请求即返回句柄，不等子代理完成。
    结果经 ``await qra.subagent_result(handle)`` 轮询。
    ``model`` 选择子代理模型（QRA 主机模型的 selector 语法，如
    ``"provider/model"`` 或 ``"deepseek"``）。
    """
    if not isinstance(prompt, str):
        raise TypeError(f"prompt must be str, got {type(prompt).__name__}")
    payload = await host_request("qra.run", {"prompt": prompt, "kwargs": kwargs})
    return _spawn_handle_from_payload(payload)


def _model_from_payload(payload: Any) -> QraModel:
    if not isinstance(payload, dict):
        raise RuntimeError("qra.find_models returned an invalid model entry")
    provider = payload.get("provider")
    model_id = payload.get("id")
    name = payload.get("name")
    selector = payload.get("selector")
    if not all(isinstance(value, str) and value for value in (provider, model_id, name, selector)):
        raise RuntimeError("qra.find_models returned an invalid model entry")
    return QraModel(provider=provider, id=model_id, name=name, selector=selector)


async def find_models(query: str = "", limit: int = 8) -> list[QraModel]:
    """Search a bounded list of models backed by active user credentials."""
    if not isinstance(query, str):
        raise TypeError(f"query must be str, got {type(query).__name__}")
    if not isinstance(limit, int):
        raise TypeError(f"limit must be int, got {type(limit).__name__}")
    payload = await host_request("qra.find_models", {"query": query, "limit": limit})
    models = payload.get("models")
    if not isinstance(models, list):
        raise RuntimeError("qra.find_models returned an invalid models list")
    return [_model_from_payload(model) for model in models]


def _subagent_from_payload(payload: Any, operation: str = "qra.list_subagents") -> QraSubagent:
    if not isinstance(payload, dict):
        raise RuntimeError(f"{operation} returned an invalid subagent entry")
    child_id = payload.get("qra_child_id")
    active_session_id = payload.get("active_session_id")
    session_id = payload.get("session_id")
    session_name = payload.get("session_name")
    session_dir = payload.get("session_dir")
    status = payload.get("status")
    if not isinstance(child_id, str) or not child_id:
        raise RuntimeError(f"{operation} entry is missing qra_child_id")
    if active_session_id is not None and not isinstance(active_session_id, str):
        raise RuntimeError(f"{operation} entry has invalid active_session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise RuntimeError(f"{operation} entry has invalid session_id")
    if not isinstance(session_name, str) or not session_name:
        raise RuntimeError(f"{operation} entry is missing session_name")
    if not isinstance(session_dir, str) or not session_dir:
        raise RuntimeError(f"{operation} entry is missing session_dir")
    if status not in {"running", "completed", "error"}:
        raise RuntimeError(f"{operation} entry has invalid status")
    return QraSubagent(
        qra_child_id=child_id,
        active_session_id=active_session_id,
        session_id=session_id,
        session_name=session_name,
        session_dir=Path(session_dir),
        status=status,
    )


async def list_subagents() -> list[QraSubagent]:
    """List direct QRA children retained by the current parent session."""
    payload = await host_request("qra.list_subagents")
    entries = payload.get("subagents")
    if not isinstance(entries, list):
        raise RuntimeError("qra.list_subagents returned an invalid subagents registry")
    return [_subagent_from_payload(entry) for entry in entries]


async def delete_subagent(target: str | QraSubagent) -> QraSubagent:
    """Delete one running or retained direct child from the current parent session."""
    if isinstance(target, QraSubagent):
        selector = target.qra_child_id
    elif isinstance(target, str):
        selector = target.strip()
        if not selector:
            raise ValueError("target must not be empty")
    else:
        raise TypeError(f"target must be str or QraSubagent, got {type(target).__name__}")
    payload = await host_request("qra.delete_subagent", {"target": selector})
    return _subagent_from_payload(payload.get("subagent"), "qra.delete_subagent")


def _result_from_payload(payload: Any) -> QraSubagentResult:
    if not isinstance(payload, dict):
        raise RuntimeError("qra.subagent_result returned an invalid result entry")
    child_id = payload.get("qra_child_id")
    status = payload.get("status")
    if not isinstance(child_id, str) or not child_id:
        raise RuntimeError("qra.subagent_result entry is missing qra_child_id")
    if status not in {"running", "completed", "error"}:
        raise RuntimeError("qra.subagent_result entry has invalid status")
    summary = payload.get("summary")
    error = payload.get("error")
    if summary is not None and not isinstance(summary, str):
        raise RuntimeError("qra.subagent_result entry has invalid summary")
    if error is not None and not isinstance(error, str):
        raise RuntimeError("qra.subagent_result entry has invalid error")
    return QraSubagentResult(qra_child_id=child_id, status=status, summary=summary, error=error)


async def subagent_result(target: str | QraSubagent) -> QraSubagentResult:
    """Poll one direct child's latest known status (QRA extension, non-blocking).

    hermes 子代理不像 prime 那样向 parent 自报结果，所以 QRA 增加本函数：
    每次调用即时返回该子代理的最近已知状态，不阻塞等待完成。父代理应在
    agent loop 的步骤之间轮询；不要在单个 cell 里长时间阻塞（cell 有 60s
    上限，宿主也不会为等待结果占用执行通道）。
    """
    if isinstance(target, QraSubagent):
        selector = target.qra_child_id
    elif isinstance(target, str):
        selector = target.strip()
        if not selector:
            raise ValueError("target must not be empty")
    else:
        raise TypeError(f"target must be str or QraSubagent, got {type(target).__name__}")
    payload = await host_request("qra.subagent_result", {"target": selector})
    return _result_from_payload(payload.get("result"))


class _HarnessProxy:
    """Resolve the harness state against the current environment on every access.

    QRA 内核对每个会话 kernel 由宿主在 spawn 时注入
    QRA_HARNESS_STATE_DIR/QRA_SESSION_DIR 等 env；本代理每次访问重新解析，
    因此快照恢复后 env 变化也能拿到正确状态。解析永不抛（内核命名空间里
    的失败会直接杀死内核）：local 未配置时读是空视图、写报指导性错误；
    其他解析失败降级为共享内存店。
    """

    _fallback: HarnessState | None = None
    _unpersisted: HarnessState | None = None

    def _resolve(self) -> HarnessState:
        try:
            return get_harness_state()
        except RuntimeError as exc:
            if "Local harness state requires" in str(exc):
                if _HarnessProxy._unpersisted is None:
                    _HarnessProxy._unpersisted = HarnessState(
                        in_memory=True,
                        local_write_error=(
                            f"{exc} This session has no persistent local harness store; "
                            "pass global_=True to persist across sessions."
                        ),
                    )
                return _HarnessProxy._unpersisted
            return self._degraded()
        except Exception:  # pragma: no cover - harness access must never raise
            return self._degraded()

    @staticmethod
    def _degraded() -> HarnessState:
        if _HarnessProxy._fallback is None:
            _HarnessProxy._fallback = HarnessState(in_memory=True)
        return _HarnessProxy._fallback

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        return repr(self._resolve())


_harness_state = _HarnessProxy()


class _QraCallable:
    harness = _harness_state
    get_harness_state = staticmethod(get_harness_state)

    async def run(self, prompt: str, **kwargs: Any) -> QraSpawnHandle:
        return await run(prompt, **kwargs)

    async def find_models(self, query: str = "", limit: int = 8) -> list[QraModel]:
        return await find_models(query, limit)

    async def list_subagents(self) -> list[QraSubagent]:
        return await list_subagents()

    async def delete_subagent(self, target: str | QraSubagent) -> QraSubagent:
        return await delete_subagent(target)

    async def subagent_result(self, target: str | QraSubagent) -> QraSubagentResult:
        return await subagent_result(target)

    async def __call__(self, prompt: str, **kwargs: Any) -> QraSpawnHandle:
        return await run(prompt, **kwargs)


qra = _QraCallable()
harness = _harness_state


class _CallableModule(types.ModuleType):
    async def __call__(self, prompt: str, **kwargs: Any) -> QraSpawnHandle:
        return await run(prompt, **kwargs)


sys.modules[__name__].__class__ = _CallableModule

__all__ = [
    "HarnessEntry",
    "HarnessScope",
    "HarnessState",
    "QraModel",
    "QraSpawnHandle",
    "QraSubagent",
    "QraSubagentResult",
    "RefinementEvent",
    "delete_subagent",
    "find_models",
    "get_harness_state",
    "harness",
    "host_request",
    "list_subagents",
    "qra",
    "run",
    "subagent_result",
]
