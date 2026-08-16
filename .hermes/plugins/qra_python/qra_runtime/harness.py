"""QRA 内核 harness 状态（prime-agent-runtime src/rlm/harness.py 的移植，v0.7.2@83a0f9f9）。

prime 的 harness 是「内核侧自管理的持久 CRUD 店」：prompt 笔记 / memory /
skill / subagent 规格 / refinement 事件，默认落会话 harness 目录，global_=True
落全局目录。执行仍归宿主（hermes）与 qra.run 递归桥。

移植裁定（D007 P2.5）：
- 文件名 env 改名（RLM_* → QRA_*），机制一字不改：mtime 防覆写（宿主或他
  进程改文件后内核再 save 不覆盖）、损坏文件当空处理、local 不可写时写
  操作响亮报错而读照常、in_memory 兜底永不抛、global 前缀路由。
- 内核内文件读写，不经过 host_request——所以宿主不参与，性能与解耦都最优。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

HarnessKind = Literal["prompt", "memory", "skill", "subagent"]
HarnessScope = Literal["local", "global"]

_DEFAULT_FILE_NAME = "harness_state.json"
_DEFAULT_HARNESS_DIR_NAME = "harness"
_KINDS: tuple[HarnessKind, ...] = ("prompt", "memory", "skill", "subagent")
_state_cache: dict[tuple[Path, HarnessScope], "HarnessState"] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(raw: str, fallback: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in raw.strip())
    normalized = "_".join(part for part in normalized.split("_") if part)
    return (normalized or fallback)[:80]


def _agent_dir() -> Path:
    raw = (
        os.environ.get("QRA_AGENT_DIR")
        or os.environ.get("HERMES_HOME")
        or str(Path.home() / ".hermes")
    )
    return Path(raw).expanduser().resolve()


def _resolve_global_flag(global_: bool = False, extra: dict[str, Any] | None = None) -> bool:
    extra = dict(extra or {})
    if "global" in extra:
        value = extra.pop("global")
        if not isinstance(value, bool):
            raise TypeError(f"global must be a bool, got {type(value).__name__}")
        global_ = value
    if extra:
        unexpected = next(iter(extra))
        raise TypeError(f"unexpected keyword argument {unexpected!r}")
    return bool(global_)


def _strip_scope_prefix(id: str | None, global_: bool) -> tuple[str | None, bool]:
    # overview() 显示 [local:id]/[global:id]；接受这些 id 原样。
    # global: 前缀路由到全局店，除非调用方已用 global_ 强制过作用域。
    if isinstance(id, str):
        scope, sep, rest = id.partition(":")
        if sep and rest and scope in ("local", "global"):
            return rest, global_ or scope == "global"
    return id, global_


def _env_dir(name: str) -> str | None:
    # 置空 env 与未设置同义（裸 "" 会跳过会话目录回退、把 local 写进全局目录）
    value = (os.environ.get(name) or "").strip()
    return value or None


def _state_file(state_dir: str | Path | None = None, *, global_: bool = False) -> Path:
    root: str | Path | None = state_dir
    if root is None:
        root = _env_dir("QRA_GLOBAL_HARNESS_STATE_DIR") if global_ else _env_dir("QRA_HARNESS_STATE_DIR")
    if root is None and not global_ and (session_dir := _env_dir("QRA_SESSION_DIR")):
        root = Path(session_dir) / _DEFAULT_HARNESS_DIR_NAME
    if root is None and not global_:
        raise RuntimeError(
            "Local harness state requires QRA_HARNESS_STATE_DIR or QRA_SESSION_DIR. "
            "Use get_harness_state(global_=True) for global state."
        )
    if root:
        return Path(root).expanduser().resolve() / _DEFAULT_FILE_NAME
    return _agent_dir() / _DEFAULT_HARNESS_DIR_NAME / _DEFAULT_FILE_NAME


@dataclass
class HarnessEntry:
    """一条可复用的 prompt / memory / skill / subagent 记录。"""

    id: str
    kind: HarnessKind
    title: str
    content: str
    path: str = "general"
    scope: HarnessScope = "local"
    reference: dict[str, Any] = field(default_factory=dict)
    arguments: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "agent"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    version: int = 1


@dataclass
class RefinementEvent:
    """一次记录的在线 harness 精修过程。"""

    id: str
    trigger: str
    changes: list[str]
    evidence: str = ""
    outcome: str = ""
    created_at: str = field(default_factory=_now)


_ENTRY_FIELDS = {field.name for field in fields(HarnessEntry)}
_REFINEMENT_FIELDS = {field.name for field in fields(RefinementEvent)}


def _validate_python_skill_reference(reference: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(reference, dict):
        raise ValueError("skill entries require a Python reference")
    normalized = dict(reference)
    if normalized.get("type") != "python":
        raise ValueError("skill reference.type must be 'python'")
    if not any(isinstance(normalized.get(key), str) and normalized[key] for key in ("import", "python_import")):
        raise ValueError("skill reference requires a Python import")
    if not any(isinstance(normalized.get(key), str) and normalized[key] for key in ("callable", "call_pattern")):
        raise ValueError("skill reference requires a callable or call_pattern")
    return normalized


class HarnessState:
    """reset 免疫的 harness 精修状态 CRUD 店。"""

    def __init__(
        self,
        file_path: str | Path | None = None,
        *,
        in_memory: bool = False,
        scope: HarnessScope = "local",
        local_write_error: str | None = None,
    ):
        # in_memory 模式永不解析/触碰路径：是路径解析失败的兜底，构造不能再抛那个错
        if in_memory:
            self.file_path: Path | None = None
        else:
            self.file_path = (
                Path(file_path).expanduser().resolve()
                if file_path
                else _state_file(global_=(scope == "global"))
            )
        self.scope: HarnessScope = scope
        # 设置后 local 写操作抛错而非静默蒸发进易失店；读与 global_=True 委托照常
        self._local_write_error = local_write_error
        self.entries: dict[HarnessKind, dict[str, HarnessEntry]] = {kind: {} for kind in _KINDS}
        self.refinements: list[RefinementEvent] = []
        self._global_target_state_dir: Path | None = None
        # 上次 load/save 时的文件 mtime——检测进程外写入（如宿主侧精修命令）
        self._loaded_mtime: int | None = None
        self.load()

    def _ensure_local_writable(self) -> None:
        if self._local_write_error is not None:
            raise RuntimeError(self._local_write_error)

    def _disk_mtime(self) -> int | None:
        if self.file_path is None:
            return None
        try:
            return self.file_path.stat().st_mtime_ns
        except OSError:
            return None

    def _sync_from_disk(self) -> None:
        """他进程重写状态文件后重载（宿主精修命令与内核长活店并存）。"""
        if self._disk_mtime() != self._loaded_mtime:
            self.load()

    def load(self) -> "HarnessState":
        if self.file_path is None or not self.file_path.exists():
            self._loaded_mtime = None
            return self
        mtime = self._disk_mtime()
        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            # 损坏/不可读的状态文件不许炸内核：当空处理，下次 save 干净重写
            data = {}
        if not isinstance(data, dict):
            data = {}

        entries: dict[HarnessKind, dict[str, HarnessEntry]] = {kind: {} for kind in _KINDS}
        raw_entries = data.get("entries", {})
        if isinstance(raw_entries, dict):
            for kind in _KINDS:
                raw_kind_entries = raw_entries.get(kind, {})
                if not isinstance(raw_kind_entries, dict):
                    continue
                for entry_id, raw_entry in raw_kind_entries.items():
                    if isinstance(raw_entry, dict):
                        entry_data = {key: value for key, value in raw_entry.items() if key in _ENTRY_FIELDS}
                        entry_data["id"] = str(entry_id)
                        entry_data["kind"] = kind
                        if not isinstance(entry_data.get("title"), str) or not isinstance(
                            entry_data.get("content"), str
                        ):
                            continue
                        if not isinstance(entry_data.get("path"), str):
                            entry_data["path"] = "general"
                        if entry_data.get("scope") not in ("local", "global"):
                            entry_data["scope"] = self.scope
                        if not isinstance(entry_data.get("source"), str):
                            entry_data["source"] = "agent"
                        version = entry_data.get("version", 1)
                        if isinstance(version, str):
                            try:
                                version = int(version)
                            except ValueError:
                                version = 1
                        if not isinstance(version, int):
                            version = 1
                        entry_data["version"] = version
                        if not isinstance(entry_data.get("reference"), dict):
                            entry_data["reference"] = {}
                        if not isinstance(entry_data.get("arguments"), dict):
                            entry_data["arguments"] = {}
                        if not isinstance(entry_data.get("metadata"), dict):
                            entry_data["metadata"] = {}
                        entries[kind][str(entry_id)] = HarnessEntry(**entry_data)
        self.entries = entries

        self.refinements = []
        raw_refinements = data.get("refinements", [])
        if isinstance(raw_refinements, list):
            for raw_event in raw_refinements:
                if isinstance(raw_event, dict):
                    event_data = {key: value for key, value in raw_event.items() if key in _REFINEMENT_FIELDS}
                    if not isinstance(event_data.get("id"), str) or not isinstance(
                        event_data.get("trigger"), str
                    ):
                        continue
                    changes = event_data.get("changes")
                    if isinstance(changes, str):
                        event_data["changes"] = [changes]
                    elif isinstance(changes, list):
                        event_data["changes"] = [str(change) for change in changes]
                    elif not isinstance(changes, list):
                        continue
                    self.refinements.append(RefinementEvent(**event_data))
        self._loaded_mtime = mtime
        return self

    def _global_target(self, global_: bool, extra: dict[str, Any] | None = None) -> "HarnessState | None":
        if not _resolve_global_flag(global_, extra):
            return None
        target = get_harness_state(state_dir=self._global_target_state_dir, global_=True)
        if self.file_path is not None and target.file_path == self.file_path and target.scope == self.scope:
            return None
        return target

    def save(self) -> "HarnessState":
        if self.file_path is None:
            # in_memory 兜底：无可持久化内容
            return self
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema": 1,
            "entries": {
                kind: {entry_id: asdict(entry) for entry_id, entry in records.items()}
                for kind, records in self.entries.items()
            },
            "refinements": [asdict(event) for event in self.refinements],
        }
        with self.file_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._loaded_mtime = self._disk_mtime()
        return self

    def upsert(
        self,
        kind: HarnessKind,
        title: str,
        content: str,
        *,
        id: str | None = None,
        path: str = "general",
        reference: dict[str, Any] | None = None,
        arguments: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "agent",
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        id, global_ = _strip_scope_prefix(id, global_)
        if target := self._global_target(global_, kwargs):
            return target.upsert(
                kind,
                title,
                content,
                id=id,
                path=path,
                reference=reference,
                arguments=arguments,
                metadata=metadata,
                source=source,
            )
        self._ensure_local_writable()
        self._sync_from_disk()
        return self._upsert(
            kind,
            title,
            content,
            id=id,
            path=path,
            reference=reference,
            arguments=arguments,
            metadata=metadata,
            source=source,
        )

    def _upsert(
        self,
        kind: HarnessKind,
        title: str,
        content: str,
        *,
        id: str | None = None,
        path: str | None = None,
        reference: dict[str, Any] | None = None,
        arguments: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "agent",
    ) -> HarnessEntry:
        # 调用方负责先 sync_from_disk：create()/update() 同步一次后直调本函数，
        # 避免存在性检查与写入之间被二次重载拆开（create-or-fail 变静默 update）
        if kind not in self.entries:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {_KINDS}")

        entry_id = id or _slug(title, kind)
        existing = self.entries[kind].get(entry_id)
        if existing:
            existing.title = title
            existing.content = content
            # 省略（None）即保留 path/reference/arguments/metadata——
            # 只改 title/content 不重置分组路径、不清掉 skill 的引用契约。
            # 显式值（含 {}）仍覆盖。
            if path is not None:
                existing.path = path
            if reference is not None:
                existing.reference = dict(reference)
            if arguments is not None:
                existing.arguments = dict(arguments)
            if metadata is not None:
                existing.metadata = dict(metadata)
            existing.source = source
            existing.updated_at = _now()
            existing.version += 1
            entry = existing
        else:
            entry = HarnessEntry(
                id=entry_id,
                kind=kind,
                title=title,
                content=content,
                path=path if path is not None else "general",
                scope=self.scope,
                reference=dict(reference or {}),
                arguments=dict(arguments or {}),
                metadata=dict(metadata or {}),
                source=source,
            )
            self.entries[kind][entry_id] = entry
        self.save()
        return entry

    def get(self, kind: HarnessKind, id: str, *, global_: bool = False, **kwargs: Any) -> HarnessEntry | None:
        id, global_ = _strip_scope_prefix(id, global_)
        if target := self._global_target(global_, kwargs):
            return target.get(kind, id)
        self._sync_from_disk()
        if kind not in self.entries:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {_KINDS}")
        return self.entries[kind].get(id)

    def delete(self, kind: HarnessKind, id: str, *, global_: bool = False, **kwargs: Any) -> bool:
        id, global_ = _strip_scope_prefix(id, global_)
        if target := self._global_target(global_, kwargs):
            return target.delete(kind, id)
        self._ensure_local_writable()
        self._sync_from_disk()
        if kind not in self.entries:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {_KINDS}")
        if id not in self.entries[kind]:
            return False
        del self.entries[kind][id]
        self.save()
        return True

    def list(self, kind: HarnessKind | None = None, *, global_: bool = False, **kwargs: Any) -> list[HarnessEntry]:
        if target := self._global_target(global_, kwargs):
            return target.list(kind)
        self._sync_from_disk()
        kinds = [kind] if kind else list(_KINDS)
        records: list[HarnessEntry] = []
        for current_kind in kinds:
            if current_kind not in self.entries:
                raise ValueError(f"unknown harness kind {current_kind!r}; expected one of {_KINDS}")
            records.extend(self.entries[current_kind].values())
        return sorted(records, key=lambda entry: (entry.kind, entry.path, entry.title, entry.id))

    def create(
        self,
        kind: HarnessKind,
        title: str,
        content: str,
        *,
        id: str | None = None,
        path: str = "general",
        reference: dict[str, Any] | None = None,
        arguments: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "agent",
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        id, global_ = _strip_scope_prefix(id, global_)
        if target := self._global_target(global_, kwargs):
            return target.create(
                kind,
                title,
                content,
                id=id,
                path=path,
                reference=reference,
                arguments=arguments,
                metadata=metadata,
                source=source,
            )
        self._ensure_local_writable()
        self._sync_from_disk()
        if kind not in self.entries:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {_KINDS}")
        entry_id = id or _slug(title, kind)
        if entry_id in self.entries[kind]:
            raise ValueError(f"{kind} entry {entry_id!r} already exists")
        return self._upsert(
            kind,
            title,
            content,
            id=entry_id,
            path=path,
            reference=reference,
            arguments=arguments,
            metadata=metadata,
            source=source,
        )

    def update(
        self,
        kind: HarnessKind,
        id: str,
        title: str,
        content: str,
        *,
        path: str | None = None,
        reference: dict[str, Any] | None = None,
        arguments: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "agent",
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        id, global_ = _strip_scope_prefix(id, global_)
        if target := self._global_target(global_, kwargs):
            return target.update(
                kind,
                id,
                title,
                content,
                path=path,
                reference=reference,
                arguments=arguments,
                metadata=metadata,
                source=source,
            )
        self._ensure_local_writable()
        self._sync_from_disk()
        if kind not in self.entries:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {_KINDS}")
        if id not in self.entries[kind]:
            raise ValueError(f"{kind} entry {id!r} does not exist")
        return self._upsert(
            kind,
            title,
            content,
            id=id,
            path=path,
            reference=reference,
            arguments=arguments,
            metadata=metadata,
            source=source,
        )

    def create_memory(
        self,
        title: str,
        content: str,
        *,
        id: str | None = None,
        path: str = "general",
        metadata: dict[str, Any] | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        return self.create("memory", title, content, id=id, path=path, metadata=metadata, global_=global_, **kwargs)

    def update_memory(
        self,
        id: str,
        title: str,
        content: str,
        *,
        path: str | None = None,
        metadata: dict[str, Any] | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        return self.update("memory", id, title, content, path=path, metadata=metadata, global_=global_, **kwargs)

    def delete_memory(self, id: str, *, global_: bool = False, **kwargs: Any) -> bool:
        return self.delete("memory", id, global_=global_, **kwargs)

    def create_prompt_note(
        self,
        title: str,
        content: str,
        *,
        id: str | None = None,
        path: str = "policy",
        metadata: dict[str, Any] | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        return self.create("prompt", title, content, id=id, path=path, metadata=metadata, global_=global_, **kwargs)

    def update_prompt_note(
        self,
        id: str,
        title: str,
        content: str,
        *,
        path: str | None = None,
        metadata: dict[str, Any] | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        return self.update("prompt", id, title, content, path=path, metadata=metadata, global_=global_, **kwargs)

    def delete_prompt_note(self, id: str, *, global_: bool = False, **kwargs: Any) -> bool:
        return self.delete("prompt", id, global_=global_, **kwargs)

    def create_skill(
        self,
        title: str,
        content: str,
        *,
        id: str | None = None,
        path: str = "general",
        reference: dict[str, Any] | None = None,
        arguments: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        return self.create(
            "skill",
            title,
            content,
            id=id,
            path=path,
            reference=_validate_python_skill_reference(reference),
            arguments=arguments,
            metadata=metadata,
            global_=global_,
            **kwargs,
        )

    def update_skill(
        self,
        id: str,
        title: str,
        content: str,
        *,
        path: str | None = None,
        reference: dict[str, Any] | None = None,
        arguments: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        # 只在提供 reference 时校验；省略保留现有引用（见 _upsert），
        # 避免每次只改 title/content 都要求重发完整 Python 引用
        validated_reference = _validate_python_skill_reference(reference) if reference is not None else None
        return self.update(
            "skill",
            id,
            title,
            content,
            path=path,
            reference=validated_reference,
            arguments=arguments,
            metadata=metadata,
            global_=global_,
            **kwargs,
        )

    def delete_skill(self, id: str, *, global_: bool = False, **kwargs: Any) -> bool:
        return self.delete("skill", id, global_=global_, **kwargs)

    def create_subagent(
        self,
        title: str,
        content: str,
        *,
        id: str | None = None,
        path: str = "general",
        metadata: dict[str, Any] | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        return self.create("subagent", title, content, id=id, path=path, metadata=metadata, global_=global_, **kwargs)

    def update_subagent(
        self,
        id: str,
        title: str,
        content: str,
        *,
        path: str | None = None,
        metadata: dict[str, Any] | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> HarnessEntry:
        return self.update("subagent", id, title, content, path=path, metadata=metadata, global_=global_, **kwargs)

    def delete_subagent(self, id: str, *, global_: bool = False, **kwargs: Any) -> bool:
        return self.delete("subagent", id, global_=global_, **kwargs)

    def record_refinement(
        self,
        trigger: str,
        changes: list[str] | str,
        *,
        evidence: str = "",
        outcome: str = "",
        id: str | None = None,
        global_: bool = False,
        **kwargs: Any,
    ) -> RefinementEvent:
        if target := self._global_target(global_, kwargs):
            return target.record_refinement(trigger, changes, evidence=evidence, outcome=outcome, id=id)
        self._ensure_local_writable()
        self._sync_from_disk()
        event_id = id or f"refine_{len(self.refinements) + 1:04d}"
        normalized_changes = [changes] if isinstance(changes, str) else list(changes)
        event = RefinementEvent(
            id=event_id,
            trigger=trigger,
            changes=normalized_changes,
            evidence=evidence,
            outcome=outcome,
        )
        self.refinements.append(event)
        self.save()
        return event

    def plan_refinement(
        self,
        observation: str,
        *,
        failing_component: str = "",
        next_step: str = "",
    ) -> list[str]:
        target = f" for {failing_component}" if failing_component else ""
        plan = [
            f"Diagnose the repeated failure or opportunity{target}: {observation}",
            "Update the smallest useful prompt note, memory item, skill, or subagent spec.",
            "Run the next action with the changed harness state, then record the outcome.",
        ]
        if next_step:
            plan.append(f"Immediate validation step: {next_step}")
        return plan

    def overview(self, *, max_entries_per_kind: int = 20, global_: bool = False, **kwargs: Any) -> str:
        if target := self._global_target(global_, kwargs):
            return target.overview(max_entries_per_kind=max_entries_per_kind)
        self._sync_from_disk()
        lines = [
            f"Harness state ({self.scope}): {self.file_path}",
            "harness skill 条目是 Python REPL 技能：须含 Python reference 与 arguments。"
            "派生子代理：handle = await qra('子任务提示')——admission 即返回 "
            "rlm_child_id/name/session_dir/model，永远不是子代理的答案。"
            "结果经 await agent_message.send(message, receiver_role='parent') 或 "
            "await qra.subagent_result(handle) 轮询取得；"
            "await qra.list_subagents() 找回直属子句柄。",
        ]
        for kind in _KINDS:
            records = self.list(kind)[:max_entries_per_kind]
            lines.append(f"{kind}: {len(self.entries[kind])}")
            for entry in records:
                summary = entry.content.strip().replace("\n", " ")
                if len(summary) > 120:
                    summary = f"{summary[:117]}..."
                argument_summary = ""
                if entry.kind == "skill" and entry.arguments:
                    argument_text = json.dumps(entry.arguments, ensure_ascii=False, sort_keys=True)
                    if len(argument_text) > 120:
                        argument_text = f"{argument_text[:117]}..."
                    argument_summary = f" args={argument_text}"
                reference_summary = ""
                if entry.kind == "skill" and entry.reference:
                    reference_text = json.dumps(entry.reference, ensure_ascii=False, sort_keys=True)
                    if len(reference_text) > 120:
                        reference_text = f"{reference_text[:117]}..."
                    reference_summary = f" ref={reference_text}"
                lines.append(
                    f"  - [{entry.scope}:{entry.id}] {entry.title} ({entry.path}, v{entry.version})"
                    f"{reference_summary}{argument_summary}: {summary}"
                )
            overflow = len(self.entries[kind]) - len(records)
            if overflow > 0:
                lines.append(f"  - +{overflow} more")
        if self.refinements:
            lines.append(f"refinements: {len(self.refinements)}")
            for event in self.refinements[-5:]:
                lines.append(f"  - [{event.id}] {event.trigger}: {', '.join(event.changes)}")
        else:
            lines.append("refinements: 0")
        return "\n".join(lines)

    def snapshot(self, *, global_: bool = False, **kwargs: Any) -> dict[str, Any]:
        if target := self._global_target(global_, kwargs):
            return target.snapshot()
        self._sync_from_disk()
        return {
            "file_path": str(self.file_path),
            "scope": self.scope,
            "entries": {
                kind: {entry_id: asdict(entry) for entry_id, entry in records.items()}
                for kind, records in self.entries.items()
            },
            "refinements": [asdict(event) for event in self.refinements],
        }


def get_harness_state(
    state_dir: str | Path | None = None, *, global_: bool = False, **kwargs: Any
) -> HarnessState:
    """返回缓存的 local harness 状态；global_=True 返回全局状态。"""
    global_ = _resolve_global_flag(global_, kwargs)
    file_path = _state_file(state_dir, global_=global_)
    scope: HarnessScope = "global" if global_ else "local"
    cache_key = (file_path, scope)
    state = _state_cache.get(cache_key)
    if state is None:
        state = HarnessState(file_path, scope=scope)
        # 只在显式 state_dir 真正偏离 env 解析结果时钉住全局目标目录，
        # 避免显式目录别名 env 路径时把后续 global_=True 写入困在 local
        if state_dir is not None:
            try:
                env_file: Path | None = _state_file(global_=global_)
            except RuntimeError:
                env_file = None
            if file_path != env_file:
                state._global_target_state_dir = Path(state_dir).expanduser().resolve()
        _state_cache[cache_key] = state
    return state


__all__ = [
    "HarnessEntry",
    "HarnessKind",
    "HarnessScope",
    "HarnessState",
    "RefinementEvent",
    "get_harness_state",
]
