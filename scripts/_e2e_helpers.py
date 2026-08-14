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


if __name__ == "__main__":
    print("helper 模块，由 verify_qra.sh 调用")
    sys.exit(1)
