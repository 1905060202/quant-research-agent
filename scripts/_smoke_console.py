"""QRA console P0 命令面全链路冒烟（scripts/smoke_console.sh 调用）。

单 pty console 进程串行走完计划中的手工冒烟清单：
  /help → /compact(守卫) → /model 路由表 → /yolo off → ! echo 无害直达
  → ! rm 危险命令被模态问 → 拒 → /yolo on → 真实提问(建会话A) → /clear(建会话B)
  → /sessions 提取会话 ID → /resume 无参 + 裸数字 → /resume <idA> 回到 A
  → 再提问(落 A) → ↑ 历史重绘 → 退格清行 → /model opus → /model deepseek
  → /export → /usage → /status → 大块粘贴确认拒绝 → 空行退出
进程退出后做 state.db 抽查：A/B 两行都在、A 消息数 ≥2、B 消息数 0、
导出文件含提问原文。

真实 API 调用 2 次（两次提问），其余全离线命令面。
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import sqlite3
import sys
import threading
import time
import urllib.request

SINA_URL = "https://hq.sinajs.cn/list=sh600519"
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(b: bytes) -> str:
    return ANSI_RE.sub(b"", b).decode("utf-8", "replace")


def sina_price() -> str | None:
    try:
        req = urllib.request.Request(SINA_URL, headers={"Referer": "https://finance.sina.com.cn"})
        body = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "replace")
        fields = body.split('="', 1)[1].split('"', 1)[0].split(",")
        return f"{float(fields[3]):.2f}"
    except Exception:
        return None


TRANSCRIPT = "/tmp/smoke_console_live.txt"
WATCHDOG_S = 900   # 全程 15 分钟护栏：超时即dump现场失败，不无限等


class _SmokeTimeout(Exception):
    pass


class ConsoleProc:
    def __init__(self, root: str):
        self.root = root
        self.out: list[bytes] = []   # 线程安全：list.append 原子，drainer 与 wait 并发读
        self.t0 = time.time()
        self._tf = open(TRANSCRIPT, "w", buffering=1)
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(f"{root}/bin/qra", ["bin/qra", "console"])
        self.pid = pid
        self.fd = fd
        # 常驻 drainer：持续抽 master。没有它，大块 send 与 console 回显会
        # pty 双向互锁（写端阻塞时无人读 master → console echo 写满 slave
        # 输出 → 读线程停 → 写端永不完成）。真实终端边写边读，不会触发。
        self._drainer_stop = threading.Event()
        self._drainer = threading.Thread(target=self._drain_loop, daemon=True)
        self._drainer.start()
        self.drain(60)   # agent 构造（MCP 发现等）

    def _drain_loop(self) -> None:
        while not self._drainer_stop.is_set():
            rlist, _, _ = select.select([self.fd], [], [], 0.5)
            if not rlist:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:   # EOF：console 退出
                break
            self.out.append(chunk)
            self._tf.write(_strip_ansi(chunk))
            self._tf.flush()

    def _plain(self) -> str:
        return _strip_ansi(b"".join(self.out))

    def drain(self, timeout: float) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if time.time() - self.t0 > WATCHDOG_S:
                raise _SmokeTimeout("watchdog")
            time.sleep(0.2)

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def wait(self, markers: tuple, timeout: float) -> tuple[bool, str]:
        """逐条轮询输出直到任一标记出现。返回 (命中, 输出明文)。"""
        end = time.time() + timeout
        while time.time() < end:
            if time.time() - self.t0 > WATCHDOG_S:
                raise _SmokeTimeout("watchdog")
            plain = self._plain()
            for m in markers:
                if m in plain:
                    return True, plain
            time.sleep(0.2)
        return False, self._plain()

    def wait_count(self, marker: str, n: int, timeout: float) -> tuple[bool, str]:
        """等 marker 第 n 次出现（防早先步骤的同名文本误命中）。"""
        end = time.time() + timeout
        while time.time() < end:
            if time.time() - self.t0 > WATCHDOG_S:
                raise _SmokeTimeout("watchdog")
            plain = self._plain()
            if plain.count(marker) >= n:
                return True, plain
            time.sleep(0.2)
        return False, self._plain()

    def step(self, label: str, data: bytes, markers: tuple, timeout: float = 30.0) -> bool:
        print(f"  · {label} [{int(time.time() - self.t0)}s]", flush=True)
        self.send(data)
        ok, plain = self.wait(markers, timeout)
        if not ok:
            print(f"  ✗ {label}：{timeout}s 内未见标记 {markers}")
            print(plain[-400:])
        return ok

    def finish(self) -> tuple[bool, str]:
        """空行退出 + reap。"""
        self.send(b"\n")
        deadline = time.time() + 120
        rc = None
        while time.time() < deadline:
            try:
                wpid, status = os.waitpid(self.pid, os.WNOHANG)
                if wpid != 0:
                    rc = os.waitstatus_to_exitcode(status)
                    break
            except ChildProcessError:
                rc = -99
                break
            self.drain(1)
        if rc is None:
            os.kill(self.pid, 9)
            return False, "挂死（120s 未退出）"
        plain = self._plain()
        if rc != 0:
            return False, f"rc={rc} 尾部：{plain[-500:]}"
        return True, plain


def check_db(root: str, id_a: str, id_b: str, q_text: str) -> bool:
    """state.db 抽查：A/B 会话行都在、A 消息 ≥2、B 消息 0。"""
    ok = True
    dbp = f"{root}/.hermes/state.db"
    if not os.path.exists(dbp):
        print(f"  ✗ state.db 不存在：{dbp}")
        return False
    conn = sqlite3.connect(dbp)
    try:
        tabs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        def cols(t):
            return {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}

        # 精确表名（tabs 无序，模糊 next() 会命中 session_model_usage 这类
        # 也含 session_id 列的表）。真表：sessions（PK=id）、messages（session_id）。
        if "sessions" not in tabs or "id" not in cols("sessions"):
            print(f"  ✗ sessions 表或 id 列缺失（tables={sorted(tabs)}）")
            return False
        if "messages" not in tabs or "session_id" not in cols("messages"):
            print(f"  ✗ messages 表或 session_id 列缺失（tables={sorted(tabs)}）")
            return False
        sess_tab, msg_tab = "sessions", "messages"

        for sid, want in ((id_a, True), (id_b, True)):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {sess_tab} WHERE id=?", (sid,)).fetchone()[0]
            if n != 1:
                print(f"  ✗ 会话 {sid[:16]}… 行数={n}（期望 1）")
                ok = False
        ma = conn.execute(
            f"SELECT COUNT(*) FROM {msg_tab} WHERE session_id=?", (id_a,)).fetchone()[0]
        mb = conn.execute(
            f"SELECT COUNT(*) FROM {msg_tab} WHERE session_id=?", (id_b,)).fetchone()[0]
        if ma < 2:
            print(f"  ✗ 会话 A 消息数={ma}（期望 ≥2，resume 后新消息应落 A）")
            ok = False
        if mb != 0:
            print(f"  ✗ 会话 B 消息数={mb}（期望 0，/clear 后尚未提问）")
            ok = False
        # resume 后的消息内容落 A
        hit = conn.execute(
            f"SELECT COUNT(*) FROM {msg_tab} WHERE session_id=? AND content LIKE ?",
            (id_a, f"%{q_text}%")).fetchone()[0]
        if hit < 1:
            print(f"  ✗ A 中未见 resume 后的提问「{q_text}」")
            ok = False
    finally:
        conn.close()
    return ok


def check_export(root: str, marker_q: str) -> bool:
    """HERMES_HOME/exports/ 最新 md 导出含提问原文。"""
    d = f"{root}/.hermes/exports"
    files = sorted((f for f in os.listdir(d) if f.endswith(".md")), key=lambda f: f)
    if not files:
        print(f"  ✗ exports 目录无 md 文件：{d}")
        return False
    for f in reversed(files):
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            continue
        if marker_q in content:
            return True
    print(f"  ✗ 导出文件中未见提问原文「{marker_q}」（{files[-1]}）")
    return False


def run_smoke(root: str) -> bool:
    price = sina_price()
    q_a = "用一句话告诉我贵州茅台现价"
    q_b = "用八个字回答你的名字"
    ok = True
    p = ConsoleProc(root)

    # ---- 命令面（离线部分） ----
    ok &= p.step("/help", b"/help\n", ("QRA console 命令",))
    ok &= p.step("/compact 守卫", b"/compact\n", ("(._.)", "压缩"))
    ok &= p.step("/model 路由表", b"/model\n", ("CC proxy",))
    ok &= p.step("/yolo off", b"/yolo\n", ("YOLO 已关闭",))
    ok &= p.step("! echo 无害", b"! echo SMOKE_BANG_OK\n", ("SMOKE_BANG_OK",))
    ok &= p.step("! rm 危险命令", b"! rm -rf /tmp/qra_smoke_probe_nonexistent\n",
                 ("⚠ 危险命令批准？",))
    ok &= p.step("拒绝危险命令", b"n\n", ("⛔",))
    ok &= p.step("/yolo on", b"/yolo\n", ("YOLO 已开启",))

    # ---- 真实提问：建会话 A ----
    # 完成信号：sina 价（最强，含答案本体）→ 否则 footer ¥（回合结束才渲染）。
    # "思考" 不可用：回合刚启动就出现，会把 /sessions 提前到消息落库之前。
    print(f"  · 提问(建会话A) [{int(time.time() - p.t0)}s]", flush=True)
    p.send((q_a + "\n").encode())
    hit, plain = p.wait(((price,) if price else ("¥",)), 150)
    if not hit:
        print(f"  ✗ 提问(建会话A)：150s 内未见完成信号")
        ok = False

    # ---- /clear 建会话 B ----
    sent_clear = b"/clear\n"
    p.send(sent_clear)
    hit, plain = p.wait(("✨ 新会话",), 30)
    if not hit:
        print("  ✗ /clear 无标记")
        ok = False
    m = re.search(r"新会话 (\w+)", plain)
    id_b = m.group(1) if m else ""
    if not id_b:
        print("  ✗ /clear 未输出新会话 ID")
        ok = False

    # ---- /sessions 提取会话 A 的 ID ----
    # "最近会话" 在 /help 输出里就有（撞车）；D-05 表格改 HORIZONTALS
    # （无竖线）后改用表头行 "  #   标题" 作唯一标记。
    p.send(b"/sessions\n")
    hit, _ = p.wait_count("  #   标题", 1, 30)
    if not hit:
        print("  ✗ /sessions 无列表")
        ok = False
    # rich 表格逐行渲染，标记命中瞬间行可能未画完——静置后取全量
    id_a = ""
    settle_end = time.time() + 3
    while time.time() < settle_end and not id_a:
        time.sleep(0.3)
        for line in p._plain().splitlines():
            # HORIZONTALS 空格分列：ID 形如 20260817_xxx，其后是消息数
            m = re.search(r"(\d{8}_\w+)\s+(\d+)\s+\S", line)
            if m and int(m.group(2)) >= 1:
                id_a = m.group(1)
                break   # 首行即取：不 break 会被最后一行的 ID 覆盖
    if not id_a:
        print("  ✗ /sessions 中未提取到有消息的会话 ID（表格格式变化？）")
        ok = False

    # ---- /resume 无参 + 裸数字 ----
    # 第 2 个表头（第 1 个是上一步 /sessions 的表）
    p.send(b"/resume\n")
    hit, _ = p.wait_count("  #   标题", 2, 30)
    if not hit:
        print("  ✗ /resume 无参无列表")
        ok = False
    ok &= p.step("裸数字选 1", b"1\n", ("↻ 已恢复会话",))

    # ---- /resume <idA> 回到 A ----
    if id_a:
        ok &= p.step("/resume <idA>", (f"/resume {id_a}\n").encode(),
                     ("↻ 已恢复会话",))

    # ---- resume 后提问：落 A ----
    # 本回合 footer = 第 2 个 ¥（第 1 个是回合 A 的；/usage 在其后才出现，无污染）
    print(f"  · resume 后提问 [{int(time.time() - p.t0)}s]", flush=True)
    p.send((q_b + "\n").encode())
    end = time.time() + 150
    need = p._plain().count("¥") + 1
    while time.time() < end:
        if p._plain().count("¥") >= need:
            break
        time.sleep(0.5)
    else:
        print(f"  ✗ resume 后提问：150s 内未见第 {need} 个 footer（¥）")
        ok = False

    # ---- ↑ 历史重绘 ----
    p.send(b"\x1b[A")
    time.sleep(2)
    p.drain(1)
    plain = p._plain()
    if plain.count(q_b) < 2:
        print(f"  ✗ ↑ 历史重绘未见第二次出现（count={plain.count(q_b)}）")
        ok = False
    p.send(b"\x7f" * 20)           # 退格清行
    ok &= p.step("清行后 bang", b"! echo HIST_OK\n", ("HIST_OK",))

    # ---- 双路由往返 ----
    ok &= p.step("/model opus", b"/model opus\n", ("✅ 已切换到 opus",))
    ok &= p.step("/model deepseek", b"/model deepseek\n", ("✅ 已切换到 deepseek",))

    # ---- export / usage / status ----
    ok &= p.step("/export", b"/export md\n", ("已导出：",))
    ok &= p.step("/usage", b"/usage\n", ("输入 ", "API 调用"))
    ok &= p.step("/status", b"/status\n", ("YOLO：",))

    # ---- 大块粘贴 → 确认 → 拒绝 ----
    # 常驻 drainer 已消除 pty 双向互锁，但大块仍按终端节奏分 500B 小块送，
    # 保证 burst 计时（<200ms 间隔）与真实粘贴一致。
    for _ in range(16):
        p.send(b"Z" * 500)
        time.sleep(0.01)
    p.send(b"\n")
    hit, plain = p.wait(("检测到大块粘贴",), 20)
    if not hit:
        print("  ✗ 大块粘贴未触发确认模态")
        ok = False
    p.send(b"n\n")

    # ---- 收尾 ----
    ok &= p.step("END bang", b"! echo END_SMOKE\n", ("END_SMOKE",))
    exited, plain = p.finish()
    if not exited:
        print(f"  ✗ 退出：{plain}")
        ok = False

    # ---- state.db + 导出抽查 ----
    if id_a and id_b:
        ok &= check_db(root, id_a, id_b, q_b)
    else:
        print("  ✗ 会话 ID 提取失败，跳过 db 抽查")
        ok = False
    ok &= check_export(root, q_a)
    return ok


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    try:
        ok = run_smoke(root)
    except _SmokeTimeout as e:
        print(f"  ✗ {e}：现场转储 {TRANSCRIPT}")
        try:
            with open(TRANSCRIPT, encoding="utf-8") as f:
                print(f.read()[-1500:])
        except OSError:
            pass
        ok = False
    print("冒烟通过 ✓" if ok else "冒烟失败 ✗")
    sys.exit(0 if ok else 1)
