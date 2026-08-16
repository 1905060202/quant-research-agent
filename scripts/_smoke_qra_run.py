#!/usr/bin/env python3
"""qra.run 递归 e2e 冒烟（完全体移植后首次真实链路验证）。

链路：console 交互 → 模型调用 qra_python 内核 → 内核 await qra.run(...) →
comm 桥 → 宿主 hermes subagent_lifecycle 接纳子代理（admission）→
subagent_result 轮询到终态。子代理任务刻意极小（2+2）控制成本。
真实 API 调用 2 次（父代理 1 轮 + 子代理 1 轮）。

判定：不信任模型嘴说——文件双证据（内核审计新 exec + 子代理会话目录）
+ 真实 32 位十六进制 CHILD_ID 与终态字样同现。注意 docstring 不得出现
「终态字样 + 预期答案」的可背诵组合（run5 教训：模型读本脚本后思考文本
直接引用 docstring 字面串，regex 假阳性）。
"""

from __future__ import annotations

import os
import pty
import re
import select
import sys
import threading
import time

TRANSCRIPT = "/tmp/smoke_qra_run_live.txt"
PROMPT = (
    "测试任务：调用 qra_python 内核工具执行一段 Python——import qra_runtime，"
    "用 await qra.run 拉起一个子代理问 2+2 等于几，打印 'CHILD_ID: ' 加上"
    "handle.qra_child_id，然后 await subagent_result 轮询直到终态，最后把最终"
    "status 和 summary 告诉我。禁止调用 qra_python 之外的任何工具（读文件、"
    "终端、其他工具都不行），禁止先写计划——现在立刻调用 qra_python 开始执行，"
    "跑完再总结。"
)


def _strip_ansi(b: bytes) -> str:
    return re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][A-Z0-9]", b"", b).decode(
        "utf-8", "replace")


def main() -> int:
    root = os.environ.get("QRA_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out: list[bytes] = []
    t0 = time.time()
    tf = open(TRANSCRIPT, "w", buffering=1)

    pid, fd = pty.fork()
    if pid == 0:
        os.execv(f"{root}/bin/qra", ["bin/qra", "console"])
    stop = threading.Event()

    def drain_loop() -> None:
        while not stop.is_set():
            rlist, _, _ = select.select([fd], [], [], 0.5)
            if not rlist:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out.append(chunk)
            tf.write(_strip_ansi(chunk))
            tf.flush()

    threading.Thread(target=drain_loop, daemon=True).start()
    time.sleep(60)  # agent 构造（MCP 发现等），与 _smoke_console 一致

    t_send = time.time()
    os.write(fd, (PROMPT + "\n").encode())
    deadline = t_send + 300
    home = os.environ.get("HERMES_HOME", f"{root}/.hermes")
    kh = f"{home}/qra_python/kernel_history"
    ss = f"{home}/qra_python/sessions"

    def fresh_evidence() -> bool:
        # 硬证据（不信模型嘴说）：发送后内核审计 jsonl 有新 exec + 有子代理会话目录
        try:
            for f in os.listdir(kh):
                if os.stat(os.path.join(kh, f)).st_mtime > t_send:
                    return os.path.isdir(ss) and any(
                        os.stat(os.path.join(ss, d)).st_mtime > t_send
                        for d in os.listdir(ss))
        except OSError:
            pass
        return False

    hit = False
    plain = ""
    while time.time() < deadline:
        plain = _strip_ansi(b"".join(out))
        # 完成信号：真实 32 位十六进制 CHILD_ID + completed 终态字样同现。
        # 单看终态字样会被模型思考文本假阳性命中（run5 实测：模型读了本脚本，
        # 思考里直接引用 docstring 字面串）；思考文本凑不出真实 child id。
        if re.search(r"CHILD_ID:\s*[0-9a-f]{32}", plain) and re.search(
                r"status\s*[=:]\s*['\"]?completed", plain):
            hit = True
            break
        time.sleep(1)
    # 顺序不能反：先写 /quit 再停 drain。若 console 已死，pty 写会 EIO（已捕获）；
    # 若 console 正忙，/quit 进其输入缓冲、回合结束后处理。停 drain 后无人再读
    # pty，写满缓冲会永久阻塞——这是 run4 卡死根因（脚本被 pkill 前一直挂在收尾）。
    try:
        os.write(fd, b"/quit\n")
    except OSError:
        pass
    time.sleep(2)
    stop.set()
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    evidence = fresh_evidence()
    print(f"[{int(time.time() - t0)}s] 终态字样: {'✓' if hit else '✗'}  文件证据: {'✓' if evidence else '✗'}")
    tail = plain[-900:]
    print("---- 输出尾部 ----")
    print(tail)
    if not evidence:
        print("✗ 无文件证据（内核审计 jsonl 无新 exec / 无子代理会话目录）——链路未真正走通")
        return 1
    if not hit:
        print("✗ 300s 内未见 subagent_result 终态字样")
        return 1
    # error 终态判定同样要贴近 CHILD_ID 段（600 字符内），防思考文本里
    # 模型复述「completed 或 error」字样造成误报。
    _child_idx = [m.end() for m in re.finditer(r"CHILD_ID:\s*[0-9a-f]{32}", plain)]
    if _child_idx and re.search(
            r"status\s*[=:]\s*['\"]?error", plain[_child_idx[-1] - 32:_child_idx[-1] + 600]):
        print("✗ 子代理终态为 error")
        return 1
    print("✓ qra.run 递归链路 e2e 通过（终态字样 + 文件双证据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
