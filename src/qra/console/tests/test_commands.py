"""命令注册表 / 三分流 / Tab 补全单元测试（console P0 命令面）。

覆盖：
- parse_input 三分流：bang / command / prompt + 路径防护不误判
- 别名注册（h→help、r→resume、m→model、?→help）
- complete()：唯一匹配加空格 / 多候选最长公共前缀 / 无进展返回 None
- vendor bang 语义（!! → !、bare ! → ""、句中 ! 不进 bang）

运行：.venv-v7/bin/python -m unittest discover -s src/qra/console/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from qra.console import commands  # noqa: E402


class ParseInputTests(unittest.TestCase):
    def test_slash_without_args_is_command(self):
        self.assertEqual(commands.parse_input("/help"), ("command", "help", ""))

    def test_slash_with_args(self):
        self.assertEqual(
            commands.parse_input("/resume 1"), ("command", "resume", "1"))

    def test_name_lowercased(self):
        # 注册表键是裸名小写，"/H" 必须能命中 help
        self.assertEqual(commands.parse_input("/H"), ("command", "h", ""))

    def test_no_slash_is_prompt(self):
        self.assertEqual(commands.parse_input("help"), ("prompt", "help"))

    def test_plain_question_is_prompt(self):
        self.assertEqual(
            commands.parse_input("贵州茅台现价是多少"),
            ("prompt", "贵州茅台现价是多少"))

    def test_bang_simple(self):
        self.assertEqual(commands.parse_input("! git status"), ("bang", "git status"))

    def test_bang_padded(self):
        self.assertEqual(commands.parse_input("!  ls -la"), ("bang", "ls -la"))

    def test_bang_bare_is_empty_command(self):
        self.assertEqual(commands.parse_input("!"), ("bang", ""))

    def test_bang_double_is_literal_bang(self):
        # vendor 语义：!! 是用户 shell 的历史展开，命令本体是 "!"
        self.assertEqual(commands.parse_input("!!"), ("bang", "!"))

    def test_mid_text_bang_is_prompt(self):
        # 句中 "!" 不是直达（vendor is_bang_command 只认行首）
        self.assertEqual(
            commands.parse_input("fix the bug!"), ("prompt", "fix the bug!"))


class PathGuardTests(unittest.TestCase):
    def test_absolute_path_is_prompt(self):
        self.assertEqual(commands.parse_input("/tmp/x"), ("prompt", "/tmp/x"))

    def test_usr_bin_path_is_prompt(self):
        self.assertEqual(
            commands.parse_input("/usr/bin/env"), ("prompt", "/usr/bin/env"))

    def test_double_slash_is_prompt(self):
        self.assertEqual(commands.parse_input("//x"), ("prompt", "//x"))

    def test_path_after_command_still_command(self):
        # 首词 /export 内无第二个 / → 命令；参数里的路径不参与防护
        self.assertEqual(
            commands.parse_input("/export md"), ("command", "export", "md"))


class AliasTests(unittest.TestCase):
    def test_aliases_share_same_def(self):
        reg = commands.all_commands()
        for alias, canonical in (("h", "help"), ("?", "help"),
                                 ("r", "resume"), ("ls", "sessions"),
                                 ("new", "clear"), ("compress", "compact"),
                                 ("e", "export"), ("m", "model"),
                                 ("cost", "usage"), ("st", "status"),
                                 ("mem", "memory")):
            self.assertIs(reg[alias], reg[canonical], f"{alias} → {canonical}")

    def test_registry_keys_are_bare_names(self):
        # 键必须全部无前导 /（parse_input 剥 / 后按裸名查表）
        for name in commands.all_commands():
            self.assertFalse(name.startswith("/"), name)


class CompleteTests(unittest.TestCase):
    def test_unique_match_adds_space(self):
        self.assertEqual(commands.complete("/res"), "/resume ")
        self.assertEqual(commands.complete("/cl"), "/clear ")

    def test_lcp_progress(self):
        # /me → mem + memory：最长公共前缀 /mem 比输入 /me 长 → 有进展
        self.assertEqual(commands.complete("/me"), "/mem")

    def test_lcp_no_progress_returns_none(self):
        # /m → m/mem/memory/model，lcp="m" 不比输入长 → None（Tab 无效果）
        self.assertIsNone(commands.complete("/m"))
        self.assertIsNone(commands.complete("/s"))

    def test_unknown_prefix_returns_none(self):
        self.assertIsNone(commands.complete("/xyz"))

    def test_non_slash_returns_none(self):
        self.assertIsNone(commands.complete("res"))
        self.assertIsNone(commands.complete("ab"))

    def test_uppercase_input_matches(self):
        self.assertEqual(commands.complete("/RES"), "/resume ")


class DispatchTests(unittest.TestCase):
    """dispatch 三分流回归：三元组解包曾崩掉整条命令面（门禁 5/5 层实证）。"""

    class _FakeConsole:
        def print(self, *a, **k):
            pass

    def _ctx(self):
        from qra.console.session_state import CommandContext
        return CommandContext(agent=None, db=None, sess=None, console=self._FakeConsole(),
                              inp=None, events=None, plain=False)

    def test_prompt_returns_prompt(self):
        self.assertEqual(commands.dispatch(self._ctx(), "普通问题"), "prompt")

    def test_unknown_command_no_crash(self):
        self.assertEqual(commands.dispatch(self._ctx(), "/nosuchcmd"), "command")

    def test_registered_handler_receives_args(self):
        ctx = self._ctx()
        seen = []
        commands.register(commands.CommandDef(
            "dtest", "", "测试", "x", lambda c, a: seen.append(a), aliases=()))
        try:
            self.assertEqual(commands.dispatch(ctx, "/dtest hello world"), "command")
            self.assertEqual(seen, ["hello world"])
        finally:
            commands._COMMANDS.pop("dtest", None)

    def test_bang_returns_bang_and_runs(self):
        # 无害 echo：走完整 bang 链路（parse → guards → 子进程）
        self.assertEqual(commands.dispatch(self._ctx(), "! echo DISPATCH_OK"), "bang")


class LoopCommandTests(unittest.TestCase):
    """/loop（CC 对齐自动继续）：空参只讲用法（离线），有参置位消费点。"""

    def _ctx(self):
        from qra.console.session_state import CommandContext
        return CommandContext(agent=None, db=None, sess=None,
                              console=DispatchTests._FakeConsole(),
                              inp=None, events=None, plain=False)

    def test_parse_loop_is_command(self):
        self.assertEqual(commands.parse_input("/loop 每天跑一遍日报"),
                         ("command", "loop", "每天跑一遍日报"))

    def test_bare_loop_shows_usage_without_setting(self):
        ctx = self._ctx()
        self.assertEqual(commands.dispatch(ctx, "/loop"), "command")
        self.assertIsNone(ctx.loop_prompt, "空参不得触发循环")

    def test_loop_with_prompt_sets_pending(self):
        ctx = self._ctx()
        self.assertEqual(commands.dispatch(ctx, "/loop 复盘今天的行情"), "command")
        self.assertEqual(ctx.loop_prompt, "复盘今天的行情")


if __name__ == "__main__":
    unittest.main()
