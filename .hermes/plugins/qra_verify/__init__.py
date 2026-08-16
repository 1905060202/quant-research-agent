"""QRA 验证闭环：声称-证据账本 + pre_verify 回合末守卫。

设计（融合架构 v1.0，零核心循环改动）：
- 账本：``$HERMES_HOME/qra_verify.db``（env HERMES_HOME，run_qra.sh 已设），
  每笔"声称"一条记录：check_type + check_args + status + evidence。
- 工具 qra_verify：agent 自己登记声称（claim）并立即跑检查；check 复检；
  report 汇总；retract 诚实撤回。
- pre_verify 钩子：回合结束时（本回合改过文件才触发，核心循环的既有条件）
  账本里有 pending（补跑检查）或 failed 的声称 → 返回
  {"action":"continue","message":...} 强制模型再跑一轮；
  全部通过 → 不干预，回合正常结束。attempt≥2 自动放弃强制
  （防死循环；核心侧还有 max_verify_nudges 兜底上限）。

检查器全部确定性、无 LLM 成本：
- data_quote     实时行情合理性（新浪源直连，价格>0 且字段齐）
- file_exists    文件存在
- file_contains  文件内容命中正则
- numeric_in_range 数值在区间内

诚实原则：检查失败不惩罚、只拦下——message 给出修/撤/标注三选一。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    task TEXT DEFAULT '',
    claim_text TEXT DEFAULT '',
    check_type TEXT NOT NULL,
    check_args TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    evidence TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    checked_at TEXT
);
"""

MAX_FORCE_ATTEMPTS = 2  # pre_verify 强制续跑的上限（防死循环）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_path() -> Path:
    env_db = os.environ.get("QRA_VERIFY_DB", "").strip()
    if env_db:
        return Path(env_db).expanduser()
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        return Path(hermes_home) / "qra_verify.db"
    return Path.home() / ".hermes" / "qra_verify.db"


def _connect() -> sqlite3.Connection:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# -- 检查器（全部确定性） ------------------------------------------------

def _check_data_quote(args: dict) -> dict:
    """实时行情合理性：新浪源直连（与 qra_quote 同源，独立小实现防跨插件导入）。"""
    symbol = str(args.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "evidence": "缺少 symbol 参数"}
    code = re.sub(r"^(sh|sz)|\..*$", "", symbol.lower())
    if code.startswith(("60", "68", "90")):
        sina = f"sh{code}"
    elif code.startswith(("00", "30", "20")):
        sina = f"sz{code}"
    else:
        return {"ok": False, "evidence": f"无法推断交易所：{symbol}（北交所不支持）"}
    req = urllib.request.Request(
        f"https://hq.sinajs.cn/list={sina}",
        headers={"Referer": "https://finance.sina.com.cn",
                 "User-Agent": "Mozilla/5.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk")
    except Exception as e:
        return {"ok": False, "evidence": f"行情请求失败：{e}"}
    m = re.search(r'"([^"]*)"', raw)
    if not m or not m.group(1):
        return {"ok": False, "evidence": "行情返回为空（可能停牌或代码错误）"}
    parts = m.group(1).split(",")
    if len(parts) < 32:
        return {"ok": False, "evidence": f"行情字段不足（{len(parts)}）"}
    name, price, date, tm = parts[0], parts[3], parts[30], parts[31]
    try:
        price_f = float(price)
    except ValueError:
        price_f = 0.0
    ok = price_f > 0 and bool(date and tm)
    return {
        "ok": ok,
        "evidence": f"{name} 现价 {price}（{date} {tm}）"
        if ok else f"{name} 数据异常：price={price} date={date} time={tm}",
        "value": {"name": name, "price": price, "date": date, "time": tm},
    }


def _check_file_exists(args: dict) -> dict:
    path = Path(str(args.get("path", "") or "")).expanduser()
    exists = path.is_file()
    return {"ok": exists, "evidence": f"{path} {'存在' if exists else '不存在'}",
            "value": {"path": str(path)}}


def _check_file_contains(args: dict) -> dict:
    path = Path(str(args.get("path", "") or "")).expanduser()
    pattern = str(args.get("pattern", "") or "")
    if not path.is_file():
        return {"ok": False, "evidence": f"{path} 不存在"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "evidence": f"读文件失败：{e}"}
    m = re.search(pattern, text, re.MULTILINE)
    if not m:
        return {"ok": False, "evidence": f"{path} 中未找到模式 {pattern!r}"}
    line_no = text[: m.start()].count("\n") + 1
    return {"ok": True,
            "evidence": f"{path} 第 {line_no} 行命中 {pattern!r}",
            "value": {"line": line_no}}


def _check_numeric_in_range(args: dict) -> dict:
    try:
        value = float(args.get("value"))
        lo = float(args.get("min"))
        hi = float(args.get("max"))
    except (TypeError, ValueError) as e:
        return {"ok": False, "evidence": f"参数不是数值：{e}"}
    ok = lo <= value <= hi
    return {"ok": ok,
            "evidence": f"{value} {'在' if ok else '不在'} [{lo}, {hi}] 区间内",
            "value": {"value": value, "min": lo, "max": hi}}


_CHECKERS = {
    "data_quote": _check_data_quote,
    "file_exists": _check_file_exists,
    "file_contains": _check_file_contains,
    "numeric_in_range": _check_numeric_in_range,
}


def run_check(check_type: str, check_args: dict) -> dict:
    checker = _CHECKERS.get(check_type)
    if checker is None:
        return {"ok": False,
                "evidence": f"未知检查类型 {check_type!r}，可用："
                            + ", ".join(sorted(_CHECKERS))}
    try:
        return checker(check_args or {})
    except Exception as e:
        return {"ok": False, "evidence": f"检查器异常：{e}"}


# -- 账本 ----------------------------------------------------------------

def _update_claim_status(conn, claim_id: int) -> dict:
    row = conn.execute(
        "SELECT check_type, check_args FROM claims WHERE id=?",
        (claim_id,)).fetchone()
    if not row:
        return {"error": f"声称 {claim_id} 不存在"}
    check_type, args_raw = row[0], row[1]
    try:
        args = json.loads(args_raw or "{}")
    except json.JSONDecodeError:
        args = {}
    verdict = run_check(check_type, args)
    status = "passed" if verdict["ok"] else "failed"
    conn.execute(
        "UPDATE claims SET status=?, evidence=?, checked_at=? WHERE id=?",
        (status, verdict.get("evidence", ""), _now(), claim_id))
    conn.commit()
    return {"id": claim_id, "status": status, **verdict}


# -- 工具 ----------------------------------------------------------------

QRA_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["claim", "check", "report", "retract"],
            "description": ("claim=登记声称并立即检查；check=按 id 复检；"
                            "report=本会话声称清单；retract=诚实撤回声称"),
        },
        "task": {"type": "string", "description": "声称所属任务（claim 用）"},
        "claim_text": {"type": "string", "description": "声称内容一句话（claim 用）"},
        "check_type": {
            "type": "string",
            "description": "检查器类型：data_quote / file_exists / file_contains / "
                           "numeric_in_range",
        },
        "check_args": {
            "type": "object",
            "description": "检查器参数，如 {\"symbol\":\"600519\"}、"
                           "{\"path\":\"data/x.csv\",\"pattern\":\"2026\"}、"
                           "{\"value\":0.5,\"min\":0,\"max\":1}",
        },
        "id": {"type": "integer", "description": "声称 ID（check/retract 用）"},
    },
    "required": ["action"],
}


def qra_verify(args: dict, **_kw) -> str:
    """QRA 验证闭环工具 handler：返回 JSON。"""
    session_id = str(_kw.get("session_id") or args.get("session_id") or "default")
    action = str(args.get("action", "") or "").strip()

    conn = _connect()
    try:
        if action == "claim":
            check_type = str(args.get("check_type", "") or "")
            if not check_type:
                return json.dumps(
                    {"error": "claim 必须给 check_type 和 check_args"},
                    ensure_ascii=False)
            check_args = args.get("check_args") or {}
            if not isinstance(check_args, dict):
                return json.dumps({"error": "check_args 必须是对象"},
                                  ensure_ascii=False)
            cur = conn.execute(
                "INSERT INTO claims(session_id, task, claim_text, check_type, "
                "check_args, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (session_id, str(args.get("task", "") or ""),
                 str(args.get("claim_text", "") or ""), check_type,
                 json.dumps(check_args, ensure_ascii=False), "pending", _now()))
            conn.commit()
            return json.dumps(_update_claim_status(conn, cur.lastrowid),
                              ensure_ascii=False)

        if action == "check":
            cid = args.get("id")
            try:
                return json.dumps(_update_claim_status(conn, int(cid)),
                                  ensure_ascii=False)
            except (TypeError, ValueError):
                return json.dumps({"error": f"id 不是数字：{cid!r}"},
                                  ensure_ascii=False)

        if action == "retract":
            cid = args.get("id")
            try:
                conn.execute(
                    "UPDATE claims SET status='retracted', checked_at=? "
                    "WHERE id=? AND session_id=?",
                    (_now(), int(cid), session_id))
                conn.commit()
                if conn.total_changes == 0:
                    return json.dumps(
                        {"error": f"声称 {cid} 不存在或不属于本会话"},
                        ensure_ascii=False)
                return json.dumps({"id": int(cid), "status": "retracted",
                                   "note": "已诚实撤回"}, ensure_ascii=False)
            except (TypeError, ValueError):
                return json.dumps({"error": f"id 不是数字：{cid!r}"},
                                  ensure_ascii=False)

        if action == "report":
            rows = conn.execute(
                "SELECT id, task, claim_text, check_type, status, evidence, "
                "checked_at FROM claims WHERE session_id=? ORDER BY id",
                (session_id,)).fetchall()
            claims = [{"id": r[0], "task": r[1], "claim_text": r[2],
                       "check_type": r[3], "status": r[4], "evidence": r[5],
                       "checked_at": r[6]} for r in rows]
            summary = {"passed": 0, "failed": 0, "pending": 0, "retracted": 0}
            for c in claims:
                summary[c["status"]] = summary.get(c["status"], 0) + 1
            return json.dumps({"claims": claims, "summary": summary},
                              ensure_ascii=False)

        return json.dumps({"error": f"未知 action：{action!r}（claim/check/report/retract）"},
                          ensure_ascii=False)
    finally:
        conn.close()


# -- pre_verify 钩子 -----------------------------------------------------

def _pre_verify(session_id: str = "", attempt: int = 0,
                changed_paths: Optional[List[str]] = None,
                final_response: str = "", **extra) -> Optional[dict]:
    """回合末守卫：账本有 pending/failed 声称 → 强制再跑一轮。

    触发条件（核心循环既有）：本回合改过文件 + 本钩子已注册 + 未超上限。
    返回 {"action":"continue","message":...} 让模型修复/撤回/诚实标注；
    全部通过返回 None（不干预）。
    """
    if attempt >= MAX_FORCE_ATTEMPTS:
        return None
    conn = _connect()
    try:
        # pending 补跑检查（登记过但没跑完的）
        for (cid,) in conn.execute(
                "SELECT id FROM claims WHERE session_id=? AND status='pending'",
                (session_id,)).fetchall():
            _update_claim_status(conn, cid)
        failed = conn.execute(
            "SELECT id, task, claim_text, check_type, evidence FROM claims "
            "WHERE session_id=? AND status='failed' ORDER BY id",
            (session_id,)).fetchall()
        if not failed:
            return None
        lines = ["验证门（qra_verify）：以下声称未通过检查，不能这样收工："]
        for fid, task, text, ctype, evidence in failed:
            label = text or task or ctype
            lines.append(f"- [{fid}] {label}：{evidence}")
        lines.append("处理方式三选一：")
        lines.append(f"a) 修复数据或结论后，qra_verify action=check id={failed[0][0]} 复检")
        lines.append(f"b) qra_verify action=retract id={failed[0][0]} 撤回声称，"
                     "并在交付里诚实说明")
        lines.append("c) 在交付内容中明确标注不确定性")
        return {"action": "continue", "message": "\n".join(lines)}
    finally:
        conn.close()


def register(ctx) -> None:
    # dsh 精华：fail-loud 启动自检——账本路径不可建/不可写此刻暴露，
    # 不留到第一次 qra_verify 调用（坏盘/权限问题直接拒绝注册）
    _connect().close()
    ctx.register_tool(
        name="qra_verify",
        toolset="qra",
        schema=QRA_VERIFY_SCHEMA,
        handler=qra_verify,
        description=("验证闭环：把关键声称登记进账本并跑确定性检查"
                     "（行情合理性/文件存在/内容命中/数值区间）。"
                     "声称不过会被回合末守卫拦下要求修复、撤回或诚实标注。"
                     "相对路径以 hermes 进程 cwd（项目根）为基准。"),
        emoji="✅",
    )
    ctx.register_hook("pre_verify", _pre_verify)
