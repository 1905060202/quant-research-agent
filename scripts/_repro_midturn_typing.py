#!/usr/bin/env python3
"""复现：输出流式期间打字 → 观察进程是否存活/输出是否正常（诊断脚本，不入门禁）。

场景 A：流式中打普通字符 + 回车
场景 B：流式中打方向键（←→↑↓）
场景 C：流式中粘贴（带 \x1b[200~ 括号粘贴包裹，模拟 iTerm2 默认行为）

用法：.venv-v7/bin/python scripts/_repro_midturn_typing.py [A|B|C]
"""

from __future__ import annotations

import os
import pty
import select
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "A"


def main() -> int:
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(f"{ROOT}/bin/qra", ["bin/qra", "console"])

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

    drain(60)  # agent 构造
    os.write(fd, "用一句话告诉我贵州茅台现价\n".encode())
    time.sleep(4)  # 等输出流式进行中
    t0 = time.time()
    print(f"[{SCENARIO}] 流式进行中，注入输入（t={time.time()-t0:.1f}s）")

    if SCENARIO == "A":
        for ch in "hello":
            os.write(fd, ch.encode())
            time.sleep(0.05)
        os.write(fd, b"\n")
    elif SCENARIO == "B":
        for seq in (b"\x1b[D", b"\x1b[C", b"\x1b[A", b"\x1b[B", b"x", b"\n"):
            os.write(fd, seq)
            time.sleep(0.05)
    elif SCENARIO == "C":
        os.write(fd, b"\x1b[200~" + b"pasted-data-1234" * 40 + b"\x1b[201~\n")

    # 观察 20s：进程是否还活着、输出是否还在正常产生
    alive = True
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                alive = False
                rc = os.waitstatus_to_exitcode(status)
                print(f"[{SCENARIO}] 进程退出 rc={rc}（t={time.time()-t0:.1f}s）")
                break
        except ChildProcessError:
            rc = -99
            alive = False
            break
        drain(2)
    else:
        print(f"[{SCENARIO}] 20s 后进程仍存活（可能挂死或还在跑）")

    tail = out[-600:].decode("utf-8", "replace")
    print(f"[{SCENARIO}] 输出尾部：\n{tail}")
    if alive:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        return 2 if b"hello" not in out and SCENARIO == "A" else 1
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
