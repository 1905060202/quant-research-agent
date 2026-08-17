"""浅色主题调色板（审计 D-07）：品牌色在浅色终端下的重映射。

参照 hermes vendor cli.py `_LIGHT_MODE_REMAP`（全表照抄）+ 检测链精简版：
env 覆盖（QRA_TUI_LIGHT / QRA_TUI_THEME / HERMES_TUI_LIGHT）→ 启动期
OSC 11 背景查询（probe_terminal_background，main.py 在画横幅前调用）→
默认暗色。任何新增彩色必须先经 remap() 走查（D-07 铁律）。

探测协议（vendor cli.py `_detect_light_mode` 同款思路，精简为两级）：
DA1 哨兵先问终端支不支持查询——不会应答的终端（pty 探针/哑终端）直接
落暗色，不干等；支持则查 OSC 11 取背景 RGB，亮度 > 0.5 判浅色。
"""
from __future__ import annotations

import os

# hermes cli.py 原表：暗色调色 → 浅色终端可读替代（不改 vendor，照抄）
_LIGHT_MODE_REMAP: dict[str, str] = {
    "#FFF8DC": "#1A1A1A",
    "#FFD700": "#9A6B00",
    "#FFBF00": "#8A5A00",
    "#B8860B": "#5C4500",
    "#DAA520": "#6B4F00",
    "#F1E6CF": "#1A1A1A",
    "#c9d1d9": "#24292F",
    "#EAF7FF": "#0F1B26",
    "#F5F5F5": "#1A1A1A",
    "#FFF0D4": "#1A1A1A",
    "#CD7F32": "#8A4F1A",
    "#FFEFB5": "#3A2A00",
}

_light: bool | None = None


def env_override() -> bool | None:
    """env 覆盖层（最高优先）：QRA_TUI_LIGHT / HERMES_TUI_LIGHT / QRA_TUI_THEME。"""
    for var in ("QRA_TUI_LIGHT", "HERMES_TUI_LIGHT"):
        v = (os.environ.get(var) or "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    theme = (os.environ.get("QRA_TUI_THEME") or "").strip().lower()
    if theme == "light":
        return True
    if theme == "dark":
        return False
    return None


def is_light() -> bool:
    global _light
    if _light is None:
        v = env_override()
        _light = v if v is not None else False   # 未探测 → 默认暗色（hermes 同）
    return _light


def set_light(value: bool) -> None:
    """启动期 OSC 11 探测结果注入（env 覆盖优先，探测只补空位）。"""
    global _light
    _light = bool(value)


def remap(hex_color: str) -> str:
    """浅色模式下把品牌色换成深色可读版本；暗色模式原样返回。

    表键大小写不一（vendor 原表混写），exact → upper → lower 三档查。
    """
    if not is_light():
        return hex_color
    table = _LIGHT_MODE_REMAP
    return (table.get(hex_color)
            or table.get(hex_color.upper())
            or table.get(hex_color.lower())
            or hex_color)


def accent() -> str:
    return remap("#FFBF00")


def gold() -> str:
    return remap("#FFD700")


def dim_gold() -> str:
    return remap("#B8860B")


def probe_terminal_background(fd_in: int, fd_out: int | None = None) -> None:
    """启动期 OSC 11 背景探测。要求 echo 已关：临时 raw 窗口查询后立刻
    还原 termios，应答字节全部 drain，不污染后续输入流。

    必须在 InputLayer 读线程启动前调用（应答字节不会被 CSI 状态机当
    按键吞掉）；pty 探针/哑终端对 DA1 无应答 → 直接落暗色（确定性）。
    """
    import select
    import termios
    import time

    if env_override() is not None:
        return   # env 覆盖优先，探测白跑
    fd = fd_out if fd_out is not None else fd_in
    try:
        old = termios.tcgetattr(fd)
    except (termios.error, ValueError):
        return   # stdout 非 tty（重定向/测试）→ 不探测，暗色
    new = old[:]
    new[3] &= ~(termios.ICANON | termios.ECHO)
    new[6][termios.VMIN] = 0
    new[6][termios.VTIME] = 0

    def drain(secs: float) -> bytes:
        out = bytearray()
        end = time.monotonic() + secs
        while time.monotonic() < end:
            r, _, _ = select.select([fd_in], [], [], 0.05)
            if not r:
                continue
            try:
                out.extend(os.read(fd_in, 256))
            except OSError:
                break
        return bytes(out)

    try:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
        except termios.error:
            return
        os.write(fd, b"\x1b[c")            # DA1 哨兵：不支持的终端不理会
        if not drain(0.15):
            set_light(False)
            return
        os.write(fd, b"\x1b]11;?\x1b\\")   # OSC 11 查询（ST 终止）
        resp = drain(0.25)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except termios.error:
            pass
    import re
    m = re.search(
        rb"11;[^;]*rgb:([0-9a-fA-F]{4})/([0-9a-fA-F]{4})/([0-9a-fA-F]{4})",
        resp)
    if not m:
        set_light(False)
        return
    r, g, b = (int(m.group(i), 16) / 65535.0 for i in (1, 2, 3))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    set_light(lum > 0.5)
