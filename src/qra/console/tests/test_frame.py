"""Frame 屏幕模型模拟测试（D011 v4）：以独立终端模拟器为判官。

不满足于断言「输出字节里包含什么」——用一个 ECMA-48 语义的迷你屏幕
模拟器解释 TermIO 发出的真实字节流，直接断言「屏幕上第 N 行是什么」：

- 提示符钉底 / 内容区域内滚动（offset 公式实证）
- 菜单/面板开合的内容恢复（a0/a1 边界实证，宁留小洞不印错位）
- busy 覆盖期间流式内容 → 诚实降级不重印
- click 屏幕行 → 绝对行命中（跨滚动换算）
- Tab 面板渲染/滚动 / 活动条出现与消失 / busy 反显
"""

from __future__ import annotations

import io
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console.frame import Frame  # noqa: E402
from qra.console.renderer import TurnRenderer  # noqa: E402
from qra.console.termio import TermIO  # noqa: E402
from rich.cells import cell_len  # noqa: E402


class _State:
    """最小 TurnState 替身（renderer 只读 show_thinking）。"""

    def __init__(self, show_thinking: bool = True) -> None:
        self.show_thinking = show_thinking


class _Screen:
    """迷你终端屏幕模拟器（ECMA-48 子集，与 TermIO 发出的序列一一对应）。

    write(s) 流入字节；SGR 忽略（raw 保留原始字节流供断言）；整行建模
    （rows: dict[row] = 字符串，宽 W）；pending-wrap 语义与 xterm 一致：
    打满最后一列后下一个可打印字符（或换行）才折行，SGR/光标定位不动它。
    """

    encoding = "utf-8"

    def __init__(self, width: int = 80, height: int = 24) -> None:
        self.W = width
        self.H = height
        self.rows: dict[int, str] = {}
        self.r = 1
        self.c = 1
        self.region_bottom = height
        self.raw = ""   # 原始字节流（反显/样式断言用）
        self._pending = False
        self._buf = ""

    def write(self, s: str) -> None:
        self.raw += s
        self._buf += s
        self._parse()

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise io.UnsupportedOperation("screen has no fd")

    def line(self, row: int) -> str:
        return self.rows.get(row, "").rstrip()   # 滚动行有补齐空格，裁掉再比

    # ------------------------------------------------------------ 内部

    def _getrow(self, row: int) -> str:
        s = self.rows.get(row, "")
        if len(s) < self.W:
            s += " " * (self.W - len(s))
        return s

    def _put(self, row: int, col: int, ch: str) -> None:
        s = self._getrow(row)
        self.rows[row] = s[:col - 1] + ch + s[col:]

    def _lf(self) -> None:
        self._pending = False
        if self.r >= self.region_bottom:
            for rr in range(2, self.region_bottom + 1):
                self.rows[rr - 1] = self._getrow(rr)
            self.rows.pop(self.region_bottom, None)
            self.c = 1   # 光标留区域底部
        else:
            self.r += 1
            self.c = 1

    def _parse(self) -> None:
        b = self._buf
        i = 0
        n = len(b)
        while i < n:
            ch = b[i]
            if ch == "\x1b":
                m = re.match(r"\x1b\[([0-9;?]*)([a-zA-Z])", b[i:])
                if m is None:
                    self._buf = b[i:]   # 不完整序列：等更多字节
                    return
                code, final = m.group(1), m.group(2)
                i += m.end()
                self._pending = False
                if final == "m":
                    pass
                elif final == "H":
                    parts = code.split(";")
                    self.r = max(1, min(self.H, int(parts[0] or 1)))
                    self.c = max(1, min(self.W, int(parts[1] or 1)))
                elif final == "J":
                    if code in ("", "0"):
                        s = self._getrow(self.r)[:self.c - 1]
                        self.rows[self.r] = s + " " * (self.W - len(s))
                        for rr in range(self.r + 1, self.H + 1):
                            self.rows.pop(rr, None)
                    elif code == "2":
                        for rr in range(1, self.H + 1):
                            self.rows.pop(rr, None)
                elif final == "K":
                    if code == "2":
                        self.rows.pop(self.r, None)
                    else:
                        s = self._getrow(self.r)[:self.c - 1]
                        self.rows[self.r] = s + " " * (self.W - len(s))
                elif final == "r":
                    if code and ";" in code:
                        self.region_bottom = int(code.split(";")[1])
                    else:
                        self.region_bottom = self.H
                elif final in ("A", "B", "G"):
                    nval = int(code or 1)
                    if final == "A":
                        self.r = max(1, self.r - nval)
                    elif final == "B":
                        self.r = min(self.H, self.r + nval)
                    else:
                        self.c = max(1, min(self.W, nval))
                continue
            if ch == "\r":
                self.c = 1
                self._pending = False
                i += 1
                continue
            if ch == "\n":
                self._lf()
                i += 1
                continue
            if self._pending:
                self._lf()   # 打满一行后的下一字符：先折行
            self._put(self.r, self.c, ch)
            if self.c >= self.W:
                self._pending = True
            else:
                self.c += 1
            i += 1
        self._buf = ""


def _setup(width: int = 80, height: int = 24):
    os.environ["COLUMNS"] = str(width)
    os.environ["LINES"] = str(height)
    screen = _Screen(width, height)
    # 显式注入尺寸（_term_size 有 ≥20 钳制，窄屏测试走不了 env）
    tio = TermIO(file=screen, width=width, height=height)
    r = TurnRenderer(tio, _State())
    fr = Frame(tio, prompt="❯ ")
    fr.offset_provider = r.offset
    fr.restore_cb = r.reprint_abs
    fr.rows_provider = lambda: r._row
    return screen, tio, r, fr


class _Env(unittest.TestCase):
    def setUp(self):
        self._old_columns = os.environ.get("COLUMNS")
        self._old_lines = os.environ.get("LINES")

    def tearDown(self):
        if self._old_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = self._old_columns
        if self._old_lines is None:
            os.environ.pop("LINES", None)
        else:
            os.environ["LINES"] = self._old_lines


class FrameLayoutTests(_Env):
    def test_prompt_pinned_and_offset_formula(self):
        """提示符永远钉在第 24 行；30 行内容在 23 行区域内滚动，
        offset = 30-23+1 = 8 → 顶行 = line-08、底行 = line-29。"""
        screen, tio, r, fr = _setup()
        fr.present()
        for i in range(30):
            r.append_line(f"line-{i:02d}")
        self.assertEqual(tio.region_bottom, 23)
        self.assertEqual(screen.line(24), "❯")        # 输入框钉底（rstrip 裁尾随空格）
        self.assertEqual(screen.line(1), "line-08")   # offset 公式实证
        self.assertEqual(screen.line(22), "line-29")
        self.assertEqual(screen.line(23), "")         # 区域底部空行（光标）

    def test_menu_open_close_restores_scrolled_content(self):
        """菜单开（覆盖底部 2 行）→ 关：被覆盖行零损失（a0/a1 边界实证）。"""
        screen, tio, r, fr = _setup()
        fr.present()
        for i in range(30):
            r.append_line(f"line-{i:02d}")
        before = {row: screen.line(row) for row in range(1, 25)}
        fr.menu_changed(["help", "sessions"], 0)
        self.assertEqual(tio.region_bottom, 21)
        self.assertEqual(screen.line(1), "line-08")   # 开合不滚动内容
        fr.menu_closed()
        self.assertEqual(tio.region_bottom, 23)
        after = {row: screen.line(row) for row in range(1, 25)}
        self.assertEqual(before, after)               # 逐行零损失

    def test_menu_restore_without_scroll(self):
        """无滚动时开合：恢复路径不印任何多余行（空白行不做假重印）。"""
        screen, tio, r, fr = _setup()
        fr.present()
        for i in range(3):
            r.append_line(f"line-{i}")
        before = {row: screen.line(row) for row in range(1, 25)}
        fr.menu_changed(["help"], 0)
        self.assertEqual(tio.region_bottom, 22)
        fr.menu_closed()
        after = {row: screen.line(row) for row in range(1, 25)}
        self.assertEqual(before, after)

    def test_panel_toggle_restores_content_idle(self):
        """Tab 开面板 → Esc 关（无新内容）：内容零损失。"""
        screen, tio, r, fr = _setup()
        fr.present()
        for i in range(12):
            r.append_line(f"line-{i:02d}")
        fr.panel_provider = lambda: ("shell", ["out-1"])
        before = {row: screen.line(row) for row in range(1, 25)}
        fr.toggle_panel()
        self.assertEqual(tio.region_bottom, 13)
        fr.toggle_panel()
        self.assertEqual(tio.region_bottom, 23)
        after = {row: screen.line(row) for row in range(1, 25)}
        self.assertEqual(before, after)

    def test_busy_panel_stream_then_close_degrades_honestly(self):
        """面板开（区域收缩）期间流式新内容：关闭不重印（行数判据失效），
        由流式内容自然回填——诚实降级，内容一行不丢。"""
        screen, tio, r, fr = _setup()
        fr.present()
        for i in range(5):
            r.append_line(f"line-{i:02d}")
        fr.panel_provider = lambda: ("shell", ["out-1"])
        fr.toggle_panel()
        self.assertEqual(tio.region_bottom, 13)
        r.append_line("streamed-while-covered")   # 覆盖期间新内容
        fr.toggle_panel()
        self.assertEqual(tio.region_bottom, 23)
        self.assertEqual(screen.line(5), "line-04")
        self.assertEqual(screen.line(6), "streamed-while-covered")

    def test_long_draft_wraps_prompt_rows(self):
        """长草稿 → 提示符带 2 行 → 区域同步收缩/扩张。"""
        screen, tio, r, fr = _setup(width=40, height=24)
        fr.present()
        self.assertEqual(tio.region_bottom, 23)
        fr.input_changed("x" * 60, 60)             # 62 格 ÷ 40 = 2 行
        self.assertEqual(tio.region_bottom, 22)
        fr.input_changed("", 0)
        self.assertEqual(tio.region_bottom, 23)


class FrameClickTests(_Env):
    def test_click_hits_block_through_offset(self):
        """25 行 + 1 折叠工具行 → offset=4，折叠行屏幕行 22；点击展开后
        块尾重印触发区域滚动，账本与屏幕行号一致。"""
        screen, tio, r, fr = _setup()
        fr.present()
        for i in range(25):
            r.append_line(f"line-{i:02d}")
        r.begin()
        r.tool_start("t1", "qra_quote", {})
        r.tool_complete("t1", "qra_quote", {}, "RESULT-1", True)
        # 折叠行绝对行 25 → 屏幕行 25 - 4 + 1 = 22
        self.assertEqual(screen.line(22)[:4], "⏺ 工具")
        self.assertTrue(r.click(22, 3))
        self.assertFalse(r.blocks[-1].collapsed)
        # 展开：摘要 1 行 + 结果 1 行 → 区域滚动一次，行号下移
        self.assertIn("⏺ 工具", screen.line(21))
        self.assertIn("RESULT-1", screen.line(22))

    def test_click_ignores_frame_rows(self):
        screen, tio, r, fr = _setup()
        fr.present()
        r.begin()
        r.tool_start("t1", "qra_quote", {})
        r.tool_complete("t1", "qra_quote", {}, "x", True)
        self.assertFalse(r.click(24, 3))   # 帧行（提示符），不属内容
        self.assertTrue(r.blocks[0].collapsed)


class MultilineFrameTests(_Env):
    """多行草稿渲染回归锁（2026-08-17 崩溃修复）。

    根因：草稿含 \\n 时按单行宽度模型渲染，行文本含真换行 → 终端错位
    → 按键重绘加剧 → 终端模拟器崩溃。修复后帧按硬换行 + 折行展开，
    每行文本是终端安全片段，光标/点击映射换行感知。
    """

    def test_multiline_draft_renders_two_rows(self):
        screen, tio, r, fr = _setup(80, 24)
        fr.input_changed("a\nb", 3)
        top = fr._frame_top()
        self.assertEqual(fr._prompt_rows(), 2)
        self.assertEqual(screen.line(top), "❯ a")
        self.assertEqual(screen.line(top + 1), "b")
        # 光标落第二行行尾（b 之后）
        self.assertEqual(tio.cursor_pos, (top + 1, 2))

    def test_multiline_empty_middle_line(self):
        screen, tio, r, fr = _setup(80, 24)
        fr.input_changed("a\n\nb", 4)
        top = fr._frame_top()
        self.assertEqual(fr._prompt_rows(), 3)
        self.assertEqual(screen.line(top), "❯ a")
        self.assertEqual(screen.line(top + 1), "")
        self.assertEqual(screen.line(top + 2), "b")
        self.assertEqual(tio.cursor_pos, (top + 2, 2))

    def test_multiline_segment_wraps(self):
        # 宽 6：'❯ ' 2 列 + 'abcd' = 满行，'efg' 续行，'x' 再续（❯ 宽 1）
        screen, tio, r, fr = _setup(6, 24)
        fr.input_changed("abcdefg\nx", 9)
        top = fr._frame_top()
        rows = fr._prompt_layout()
        self.assertEqual([x[0] for x in rows], ["❯ abcd", "efg", "x"])
        self.assertEqual(screen.line(top), "❯ abcd")
        self.assertEqual(screen.line(top + 1), "efg")
        self.assertEqual(screen.line(top + 2), "x")
        self.assertEqual(tio.cursor_pos, (top + 2, 2))

    def test_wide_char_rows_never_exceed_terminal_width(self):
        # 宽 3 终端：❯+空格 = 2 列，剩 1 列装不下 2 列宽的中字 → 每行一个
        # 中字。旧窗口算法行界切进宽字中间会产出 "中中"（4 列 > 3 列行宽），
        # 真终端对超宽行二次折行 → 光标/帧错位（与多行崩溃同源病根）。
        screen, tio, _, fr = _setup(3, 24)
        fr.input_changed("中中中", 3)
        top = fr._frame_top()
        self.assertEqual([x[0] for x in fr._prompt_layout()],
                         ["❯ ", "中", "中", "中"])
        self.assertEqual(screen.line(top), "❯")       # rstrip 裁尾部空格
        self.assertEqual(screen.line(top + 1), "中")
        self.assertEqual(screen.line(top + 2), "中")
        self.assertEqual(screen.line(top + 3), "中")
        self.assertEqual(tio.cursor_pos, (top + 3, 3))

    def test_layout_rows_never_exceed_width(self):
        # 行宽不变量锁：任意草稿（宽字符/硬换行/窄屏）下每行显示宽 ≤ 终端宽
        for w in (3, 5, 8, 12, 20):
            _, tio, _, fr = _setup(w, 24)
            for draft in ("中中中中中", "a\n中中\nbcd", "中\n中\n中",
                          "x" * 50, "中" * 30, "ab中cd中ef\ngh中"):
                fr.input_changed(draft, len(draft))
                for text, _s, _e in fr._prompt_layout():
                    self.assertLessEqual(cell_len(text), w,
                                         f"宽 {w} 行文本超宽: {text!r}")

    def test_layout_rows_never_contain_controls(self):
        # 不变量锁：帧画出的每一行都是终端安全片段（无 \n \r \t）
        _, _, _, fr = _setup(40, 24)
        for draft in ("a\nb", "a\n\nb", "abcdefghijk\nxy", "a\n" * 3 + "z"):
            fr.input_changed(draft, len(draft))
            for text, _s, _e in fr._prompt_layout():
                self.assertNotIn("\n", text)
                self.assertNotIn("\r", text)
                self.assertNotIn("\t", text)

    def test_cursor_between_lines(self):
        _, tio, _, fr = _setup(80, 24)
        fr.input_changed("ab\ncd", 2)   # 光标在 ab 之后（\n 之前）
        top = fr._frame_top()
        self.assertEqual(tio.cursor_pos, (top, 5))   # '❯ ab' = 4 列 → col 5

    def test_cursor_full_row_falls_to_next_line(self):
        # 光标恰在满行行尾（col == 行宽）：落下一行行首，避开 pending-wrap
        _, tio, _, fr = _setup(6, 24)
        fr.input_changed("abcdefg\nx", 4)   # 光标在 abcd 后 → '❯ abcd' 满行 6 列
        top = fr._frame_top()
        # pos=6 命中行 0（s=0 e=6）→ col = cell_len('❯ abcd') = 6 >= 6 → 折下行行首
        self.assertEqual(tio.cursor_pos, (top + 1, 1))

    def test_submit_clears_multiline_back_to_single_row(self):
        screen, tio, r, fr = _setup(80, 24)
        fr.input_changed("a\nb", 3)
        top = fr._frame_top()
        self.assertEqual(fr._prompt_rows(), 2)
        fr.input_changed("", 0)          # 提交后清空
        top2 = fr._frame_top()           # 帧变矮，顶部下移一行
        self.assertEqual(fr._prompt_rows(), 1)
        self.assertEqual(screen.line(top2), "❯")
        self.assertEqual(screen.line(top2 - 1), "")   # 旧行被区域扩张擦除
        self.assertEqual(screen.line(top2 + 1), "")

    def test_click_to_draft_maps_rows_and_cols(self):
        _, _, _, fr = _setup(80, 24)
        fr.input_changed("ab\ncd", 5)
        self.assertEqual(fr.click_to_draft(0, 0), 0)    # '❯' 起点 → draft 0
        self.assertEqual(fr.click_to_draft(0, 3), 1)    # 点在 b 上 → draft 1
        self.assertEqual(fr.click_to_draft(0, 4), 2)    # 行尾后 → ab 之后
        self.assertEqual(fr.click_to_draft(1, 0), 3)    # 第二行 c 之前
        self.assertEqual(fr.click_to_draft(1, 1), 4)    # 点在 d 上 → 行尾
        self.assertEqual(fr.click_to_draft(1, 99), 5)   # 超右端钳行尾 = draft 长 5
        self.assertIsNone(fr.click_to_draft(2, 0))      # 行超界 → None


class FrameActivityTests(_Env):
    def test_activity_row_shows_tool_then_disappears(self):
        """工具执行中 → 输入框下方活动条（第 24 行）；完成后结构性消失。"""
        screen, tio, r, fr = _setup()
        fr.present()
        r.begin()
        r.tool_start("t1", "qra_quote", {})
        fr.activity_provider = r.activity
        fr.present()
        self.assertEqual(tio.region_bottom, 22)          # 帧高 2：提示符+活动
        self.assertIn("qra_quote", screen.line(24))
        self.assertIn("执行中", screen.line(24))
        self.assertIn("Tab 查看", screen.line(24))
        r.tool_complete("t1", "qra_quote", {}, "ok", True)
        fr.tick()                                        # 结构性消失
        self.assertEqual(tio.region_bottom, 23)
        self.assertEqual(screen.line(24), "❯")
        self.assertIn("⏺ 工具", screen.line(1))          # 摘要行在内容区

    def test_busy_inversion_emits_reverse_video(self):
        """busy 中提示符带反显（\x1b[7m），草稿内容仍实时在框内。"""
        screen, tio, r, fr = _setup()
        fr.present()
        fr.input_changed("hi", 2)
        fr.set_busy(True)
        self.assertIn("\x1b[7m", screen.raw)             # 反显开启
        self.assertEqual(screen.line(24), "❯ hi")        # SGR 被剥后内容一致
        fr.set_busy(False)
        self.assertEqual(screen.line(24), "❯ hi")


class FramePanelTests(_Env):
    def test_panel_renders_title_body_and_scrolls(self):
        """Tab 面板：标题行 + 9 行主体 + 滚动窗口正确。"""
        screen, tio, r, fr = _setup()
        fr.present()
        fr.panel_provider = lambda: ("shell", [f"out-{i}" for i in range(20)])
        fr.toggle_panel()
        self.assertTrue(fr.panel_open)
        self.assertEqual(tio.region_bottom, 13)          # 帧高 11
        self.assertEqual(screen.line(14), "❯")           # 提示符带
        self.assertEqual(screen.line(15), "▸ shell")     # 面板标题
        self.assertIn("out-0", screen.line(16))
        self.assertIn("out-8", screen.line(24))          # 9 行主体到底
        fr.panel_scroll_by(15)
        # 钳制：max(0, 20-9) = 11 → 视图 = out-11..out-19
        self.assertIn("out-19", screen.line(24))
        self.assertNotIn("out-0", " ".join(
            screen.line(i) for i in range(16, 25)))      # 顶部行滚出


if __name__ == "__main__":
    unittest.main()
