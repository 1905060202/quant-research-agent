"""console 会话状态与输入历史（SessionState / ConsoleHistory / 命令上下文）。

从 main.py 拆出：多轮交互的会话态（session_id / 路由 / yolo / 对话历史）
与 ↑↓ 输入历史落盘。价格常量也迁到这里（渲染层 footer 与 /usage 共用）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# DeepSeek 公开价（USD / 1M tokens），¥ 按 7.2 折算
PRICE_USD = {"input": 0.27, "output": 1.10, "cache_read": 0.027}
USD_CNY = 7.2

HISTORY_CAP = 1000


def new_session_id() -> str:
    """AIAgent 同款格式：YYYYMMDD_HHMMSS_hex6（agent_init.py:1560-1563）。"""
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


@dataclass
class SessionState:
    """console 多轮交互的会话态。history 即 run_turn 的 conversation_history 入参。"""

    session_id: str = ""
    model: str = ""
    provider: str | None = None
    base_url: str = ""
    api_mode: str = ""
    route_name: str = ""            # "deepseek" | "opus" | ""
    yolo: bool = True
    history: list = field(default_factory=list)
    last_result: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    _title_set: bool = False

    def mark_title_set(self) -> None:
        self._title_set = True

    @property
    def title_done(self) -> bool:
        return self._title_set


@dataclass
class CommandContext:
    """命令处理器上下文：console 主循环持有的全部句柄。

    定义在 session_state（叶子模块）避免 commands ↔ handlers 循环 import。
    pending 供 /resume 无参后的"纯数字待选号"模式使用。
    """

    agent: object
    db: object
    sess: SessionState
    console: object          # rich.Console
    inp: object              # InputLayer
    events: object           # queue.Queue
    plain: bool
    pending: dict = field(default_factory=dict)   # {"resume": [listing rows]}


class ConsoleHistory:
    """↑↓ 输入历史：jsonl 落盘（HERMES_HOME/console_history.jsonl）。

    - cap 1000 条，超出丢最旧
    - 连续重复去重（误按 ↑ 回车不会灌两条）
    - / 与 ! 行不记（命令与直达不进历史，防 ↑ 召回长命令误执行）
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._entries: list[str] = []
        if path is None:
            try:
                from tools.memory_tool import get_memory_dir
                self._path = get_memory_dir().parent / "console_history.jsonl"
            except Exception:
                self._path = None
        self._idx = 0
        self._load()

    # ------------------------------------------------------------ 导航

    def up(self, _draft: str) -> str | None:
        """↑：返回上一条历史（首次按 ↑ 给最近一条）。无历史返回 None。"""
        if not self._entries:
            return None
        if self._idx > 0:
            self._idx -= 1
        return self._entries[self._idx]

    def down(self, _draft: str) -> str:
        """↓：返回下一条；越过末尾回到空草稿（""）。"""
        if self._idx >= len(self._entries) - 1:
            self._idx = len(self._entries)
            return ""
        self._idx += 1
        return self._entries[self._idx]

    def reset_cursor(self) -> None:
        self._idx = len(self._entries)

    # ------------------------------------------------------------ 写入

    def push(self, line: str) -> None:
        line = (line or "").strip()
        if not line or line.startswith("/") or line.startswith("!"):
            self.reset_cursor()
            return
        if self._entries and self._entries[-1] == line:
            self.reset_cursor()
            return
        self._entries.append(line)
        if len(self._entries) > HISTORY_CAP:
            del self._entries[:-HISTORY_CAP]
        self.reset_cursor()
        self._persist(line)

    # ------------------------------------------------------------ 落盘

    def _load(self) -> None:
        if not self._path:
            return
        try:
            for raw in self._path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    line = str(obj.get("line") or "").strip()
                except (json.JSONDecodeError, TypeError):
                    line = raw   # 脏行：整行当历史
                if line and (not self._entries or self._entries[-1] != line):
                    self._entries.append(line)
        except (OSError, ValueError):
            pass
        if len(self._entries) > HISTORY_CAP:
            del self._entries[:-HISTORY_CAP]
        self._idx = len(self._entries)

    def _persist(self, line: str) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"line": line}, ensure_ascii=False) + "\n")
        except OSError:
            pass
