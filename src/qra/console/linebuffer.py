"""LineBuffer：纯行编辑模型（D011 v2，←→ 光标移动 P0）。

与终端/渲染完全解耦的纯 Python 类——字符列表 + 光标位置，
行中插入/删除/左右移动/Home/End 全部在此实现，输入层只负责
「读到什么键 → 调什么方法 → 重绘」。单测直接覆盖，不经 pty。

设计约束：本类不感知显示列宽（CJK 双列等由输入层的
_char_width/显示层负责），pos 恒为「第几个字符（0 基，指向
光标前字符数）」。
"""

from __future__ import annotations


class LineBuffer:
    __slots__ = ("_chars", "_pos")

    def __init__(self, initial: str = "") -> None:
        self._chars: list[str] = list(initial)
        self._pos = len(self._chars)

    # ------------------------------------------------------------ 查询

    @property
    def text(self) -> str:
        return "".join(self._chars)

    @property
    def pos(self) -> int:
        return self._pos

    def __len__(self) -> int:
        return len(self._chars)

    def __repr__(self) -> str:
        t = self.text
        return f"LineBuffer({t[:self._pos]!r}❘{t[self._pos:]!r})"

    # ------------------------------------------------------------ 编辑

    def insert(self, ch: str) -> None:
        """在光标处插入字符，光标右移。ch 为单个（解码后）字符。"""
        if not ch:
            return
        self._chars.insert(self._pos, ch)
        self._pos += 1

    def backspace(self) -> str | None:
        """删光标前一个字符，光标左移；空行返回 None。"""
        if self._pos == 0:
            return None
        self._pos -= 1
        return self._chars.pop(self._pos)

    def delete(self) -> str | None:
        """删光标处字符（Delete 键）；行尾返回 None。"""
        if self._pos >= len(self._chars):
            return None
        return self._chars.pop(self._pos)

    def kill_to_end(self) -> str:
        """删光标到行尾（Ctrl+K 预留）；返回被删文本。"""
        killed = "".join(self._chars[self._pos:])
        del self._chars[self._pos:]
        return killed

    # ------------------------------------------------------------ 移动

    def left(self) -> None:
        self._pos = max(0, self._pos - 1)

    def right(self) -> None:
        self._pos = min(len(self._chars), self._pos + 1)

    def home(self) -> None:
        self._pos = 0

    def end(self) -> None:
        self._pos = len(self._chars)

    def move_to(self, pos: int) -> None:
        """直接设光标（SGR 鼠标点击定位用），越界钳制。"""
        self._pos = max(0, min(len(self._chars), pos))

    # ------------------------------------------------------------ 整体

    def replace(self, new_text: str) -> None:
        """整行替换（↑↓ 历史 / Tab 补全 / 斜杠菜单选中），光标到行尾。"""
        self._chars = list(new_text)
        self._pos = len(self._chars)

    def clear(self) -> None:
        self._chars.clear()
        self._pos = 0
