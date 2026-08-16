"""QRA 回归门禁的 E2E 辅助（verify_qra.sh 调用）。

- 新浪行情同源取价（qra_quote 的数据源）→ 动态期望值，不写死价格
- pty 驱动 console：问答 + 回合中空行竞态用例
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import sys
import time
import urllib.request

SINA_URL = "https://hq.sinajs.cn/list=sh600519"
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")


def sina_price() -> str | None:
    """贵州茅台现价（两位小数字符串）。失败返回 None。"""
    try:
        req = urllib.request.Request(SINA_URL, headers={"Referer": "https://finance.sina.com.cn"})
        body = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "replace")
        fields = body.split('="', 1)[1].split('"', 1)[0].split(",")
        return f"{float(fields[3]):.2f}"  # fields[3] = 当前价
    except Exception:
        return None


def _strip_ansi(b: bytes) -> str:
    return ANSI_RE.sub(b"", b).decode("utf-8", "replace")


def run_z(root: str) -> bool:
    """-z 单发工具题：回答含新浪同源价格 + exit 0。"""
    expect = sina_price()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.dup2(w, 1)
        os.dup2(w, 2)  # stderr 并入，超时时能看到卡点
        os.close(w)
        token = os.environ.get("ANTHROPIC_TOKEN")
        if not token:
            try:
                token = json.load(open(os.path.expanduser("~/.claude/settings.json"))).get(
                    "env", {}).get("ANTHROPIC_AUTH_TOKEN", "")
            except Exception:
                token = ""
        env = dict(os.environ, ANTHROPIC_TOKEN=token or "")
        os.execve(f"{root}/bin/qra",
                  ["bin/qra", "-z", "用一句话告诉我贵州茅台现价"], env)
    os.close(w)
    out = b""
    deadline = time.time() + 240
    rc = None
    eof = False
    while time.time() < deadline:
        rlist, _, _ = select.select([r], [], [], 5)
        if rlist:
            try:
                chunk = os.read(r, 65536)
            except OSError:
                break
            if chunk:
                out += chunk
            else:
                # EOF：子进程已关 stdout。退出码必须等它真正退出再 reap，
                # 不能在 EOF 处直接 break —— 会错过 waitpid（竞态：新 vendor
                # 的 -z 写完立刻 exit，EOF 常比下一次 WNOHANG 先到）。
                eof = True
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid != 0:
            rc = os.waitstatus_to_exitcode(status)
            break
        if eof:
            # 管道已关：别再空转 select，轮询 reap 即可
            time.sleep(0.2)
    if rc is None:
        os.kill(pid, 9)
        plain = _strip_ansi(out)
        print(f"  ✗ -z 超时（240s）。已捕获输出尾部：")
        print(plain[-800:])
        return False
    plain = _strip_ansi(out)
    ok = rc == 0
    if expect:
        if expect not in plain:
            print(f"  ✗ 答案未含新浪同源价格（期望 {expect}）")
            ok = False
    else:
        print("  ! 新浪取价失败，降级为标记检查")
    if not ok:
        print(f"  rc={rc} 输出尾部：{plain[-300:]}")
    return ok


def run_interactive(root: str, tag: str) -> bool:
    """交互模式：问答 → 回合中发空行 → 退出 rc=0，标记齐全。"""
    expect = sina_price()
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(f"{root}/bin/qra", ["bin/qra", "console"])

    out = b""

    def drain(timeout: float) -> None:
        nonlocal out
        end = time.time() + timeout
        while time.time() < end:
            rlist, _, _ = select.select([fd], [], [], 1.0)
            if rlist:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return

    drain(60)  # 初始化
    os.write(fd, "用一句话告诉我贵州茅台现价\n".encode())
    price_seen = False
    for _ in range(40):
        drain(5)
        plain = _strip_ansi(out)
        if expect and expect in plain:
            price_seen = True
            break
    os.write(fd, "\n".encode())  # 回合中空行 → 必须被缓冲 → 回合后触发退出
    deadline = time.time() + 120
    rc = None
    while time.time() < deadline:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                rc = os.waitstatus_to_exitcode(status)
                break
        except ChildProcessError:
            rc = -99
            break
        drain(1)
    if rc is None:
        os.kill(pid, 9)
        print(f"  ✗ {tag} 挂死（120s 未退出）")
        return False
    plain = _strip_ansi(out)
    ok = all(k in plain for k in ("思考", "工具", "¥")) and rc == 0
    if expect and not price_seen and expect not in plain:
        print(f"  ✗ {tag} 答案未含期望价格 {expect}")
        ok = False
    if not ok:
        print(f"  {tag}: rc={rc} 输出尾部：{plain[-300:]}")
    return ok


def run_console_cmd(root: str, cases: list) -> bool:
    """命令 pty：单进程依次发命令，逐条断言标记，空行退出 rc=0。

    cases = [(命令行, 标记)]；标记为 str 或 tuple（任一命中即过）。
    全离线：不发模型提问，只验证 /命令与 ! 直达的交互面。
    """
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(f"{root}/bin/qra", ["bin/qra", "console"])

    out = b""

    def drain(timeout: float) -> None:
        nonlocal out
        end = time.time() + timeout
        while time.time() < end:
            rlist, _, _ = select.select([fd], [], [], 1.0)
            if rlist:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return

    drain(60)  # agent 构造（MCP 发现等）
    ok = True
    for cmd, marker in cases:
        os.write(fd, (cmd + "\n").encode())
        markers = marker if isinstance(marker, tuple) else (marker,)
        seen = False
        for _ in range(12):
            drain(5)
            plain = _strip_ansi(out)
            if any(m in plain for m in markers):
                seen = True
                break
        if not seen:
            print(f"  ✗ 命令 [{cmd}] 未出现标记 {markers}")
            ok = False
    os.write(fd, b"\n")  # 空行退出
    deadline = time.time() + 120
    rc = None
    while time.time() < deadline:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                rc = os.waitstatus_to_exitcode(status)
                break
        except ChildProcessError:
            rc = -99
            break
        drain(1)
    if rc is None:
        os.kill(pid, 9)
        print("  ✗ 命令 pty 挂死（120s 未退出）")
        return False
    if rc != 0:
        plain = _strip_ansi(out)
        print(f"  ✗ 命令 pty rc={rc} 输出尾部：{plain[-500:]}")
        ok = False
    return ok


if __name__ == "__main__":
    print("helper 模块，由 verify_qra.sh 调用")
    sys.exit(1)


def run_console_raw(root: str) -> bool:
    """D011 原始字节 pty（全离线）：斜杠菜单弹出/Esc 关闭/←→ 光标编辑/
    /fold /agents /mouse /括号粘贴/Ctrl+C 恢复，最后空行退出 rc=0。

    只发字节不发模型提问：断言输入层与命令面的回显协议。
    """
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(f"{root}/bin/qra", ["bin/qra", "console"])

    out = b""

    def drain(timeout: float) -> None:
        nonlocal out
        end = time.time() + timeout
        while time.time() < end:
            rlist, _, _ = select.select([fd], [], [], 1.0)
            if rlist:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return

    def send_and_wait(data: bytes, marker: str, timeout: float = 12.0) -> bool:
        os.write(fd, data)
        deadline = time.time() + timeout
        while time.time() < deadline:
            drain(1)
            if marker in _strip_ansi(out):
                return True
        print(f"  ✗ 字节序列 {data[:24]!r} 未出现标记 {marker!r}")
        return False

    drain(60)  # agent 构造（MCP 发现等）
    ok = True
    ok &= send_and_wait(b"/", "显示全部命令")        # 斜杠菜单弹出
    # Esc 关菜单草稿残留 "/"，先退格清掉再输入，否则会拼成 //x
    ok &= send_and_wait(b"\x1b\x7fx", "❯ x")         # Esc 关菜单 + 清残留 + 普通键回显
    ok &= send_and_wait(b"\x7f", "❯ ")               # 退格清掉 x
    ok &= send_and_wait(b"abc", "abc")               # 行编辑回显
    os.write(fd, b"\x1b[D\x1b[C\x7f\x7f\x7f\x7f")    # ←→ 光标移动 + 清草稿（不提交）
    drain(2)
    # 以下命令若被误当 prompt 提交会触发真实 API 回合，busy 中排队空行=回合后退出
    # （本用例断言离线命令面，不触碰模型）。菜单 Enter=执行（D011），单回车即跑。
    ok &= send_and_wait(b"/fold\n", "没有可折叠的块")   # 菜单选中即执行
    ok &= send_and_wait(b"/agents\n", "尚无子代理记录")
    ok &= send_and_wait(b"/mouse\n", "鼠标捕获：关（默认）")
    ok &= send_and_wait(b"\x1b[200~hello\x1b[201~", "hello")  # 括号粘贴一次重绘
    n_hello = _strip_ansi(out).count("hello")
    if n_hello > 2:
        print(f"  ✗ 括号粘贴回显 {n_hello} 次（应 ≤2）")
        ok = False
    os.write(fd, b"\x7f" * 5)                        # 清草稿
    drain(2)
    # v4 固定输入框：Tab 开面板（活动输出区）→ Esc 关回输入框 → 再开。
    # pty 输出是累积的，用计数证明第二次 Tab 真的重绘了面板。
    ok &= send_and_wait(b"\t", "▸ 本轮活动")         # Tab：面板弹出（标题行）
    n_panel = _strip_ansi(out).count("▸ 本轮活动")
    ok &= send_and_wait(b"\x1b", "❯ ")               # Esc：关闭面板
    ok &= send_and_wait(b"\t", "▸ 本轮活动")         # 再开
    if _strip_ansi(out).count("▸ 本轮活动") != n_panel + 1:
        print("  ✗ Tab 二次弹出未重绘面板")
        ok = False
    os.write(fd, b"\x1b")                            # 关面板
    drain(1)
    ok &= send_and_wait(b"\x03", "^C")               # Ctrl+C 恢复（不退出）
    os.write(fd, b"\n")                              # 空行退出
    deadline = time.time() + 120
    rc = None
    while time.time() < deadline:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                rc = os.waitstatus_to_exitcode(status)
                break
        except ChildProcessError:
            rc = -99
            break
        drain(1)
    if rc is None:
        os.kill(pid, 9)
        print("  ✗ 原始字节 pty 挂死（120s 未退出）")
        return False
    if rc != 0:
        plain = _strip_ansi(out)
        print(f"  ✗ 原始字节 pty rc={rc} 输出尾部：{plain[-500:]}")
        ok = False
    return ok
