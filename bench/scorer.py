#!/usr/bin/env python3
"""QRA-Bench 评分器：机械评分，0/0.5/1 粒度，复合题取子项平均。

用法：
    .venv-v7/bin/python bench/scorer.py --system qra --outdir bench/results/qra
    .venv-v7/bin/python bench/scorer.py --system cc  --outdir bench/results/cc
    .venv-v7/bin/python bench/scorer.py --compare   # 两系统对照总表

输出：<outdir>/../score_<system>_<日期>.md + stdout 汇总。

gold 类型（bench/qra_bench_v1.json）：
  quote         每标的「现价在 rel 容差内 + 涨跌幅在 abs 容差内」双命中
  signal_hot    提到基准 HOT 前 N 中 ≥names_required 个名称；stale_required 时须附时效词
  signal_cold   板块名 + 冷榜词 + 排名在 rank_tol 内（排名不可得则只看前两项）
  kb            要素命中数 + 引用文档名在 KB 清单内
  honesty       无捏造数字（题目自带数字回显豁免）+ 必需诚实词；suggest_terms 兜底 0.5
  memory_dup    答文出现判重词
  numeric       数值在容差内（% 表述 ×100 等价）
  elements      要素命中数分级
  citation      要素命中 + 必须引用 acceptable_docs 之一（没引封顶 0.5）
  structure     小节标题命中数分级
  kb_doc_mentioned  引用过指定文档名
  quote_any     N 标的里 ≥min_quotes 个双命中
  file_contains 文件存在且含 pattern
  composite     子 gold 得分平均

幻觉率口径（两道计数，任一命中该题记幻觉）：
  ① honesty 题出现题目之外的数字（排除 19xx/20xx 年份）
  ② 答文引用 .md 文档名不在 KB 清单
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BENCH = json.loads((PROJECT / "bench" / "qra_bench_v1.json").read_text(encoding="utf-8"))
QUESTIONS = {q["id"]: q for q in BENCH["questions"]}

NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
YEAR_RE = re.compile(r"(?<![\d.])(?:19|20)\d{2}(?![\d.])")
CODE_RE = re.compile(r"(?<![\d.])\d{6}(?![\d.])")  # 6 位整数=股票代码形态，不是价格
ROMAN_SUFFIX = re.compile(r"[ⅠⅡⅢⅣⅤ]+$")
MD_DOC_RE = re.compile(r"[\w一-鿿-]{2,}\.md")


def load_kb_docs() -> set[str]:
    """KB 文档清单：data/kb_fts.db 优先，回退 kb_sources 文件名。"""
    db = PROJECT / "data" / "kb_fts.db"
    if db.is_file():
        try:
            conn = sqlite3.connect(db)
            rows = conn.execute("SELECT DISTINCT doc_name FROM documents").fetchall()
            conn.close()
            return {r[0] for r in rows}
        except sqlite3.Error:
            pass
    kb_dir = PROJECT / "kb_sources"
    return {p.name for sub in ("cheetah", "methodology")
            for p in (kb_dir / sub).glob("*.md")}


KB_DOCS = load_kb_docs()


def numbers_in(text: str) -> list[float]:
    return [float(n) for n in NUM_RE.findall(text)]


def numbers_not_in_prompt(text: str, prompt: str) -> list[float]:
    """题目自带数字（如代码 999999）回显不算捏造。年份、6 位代码形态（含工具
    错误信息回显的示例代码 600519）豁免——它们不是价格。"""
    prompt_nums = {n for n in NUM_RE.findall(prompt)}
    text_wo_years = YEAR_RE.sub("", text)
    text_wo_codes = CODE_RE.sub("", text_wo_years)
    return [n for n in NUM_RE.findall(text_wo_codes) if n not in prompt_nums]


def hit_names(answer: str, names: list[str]) -> int:
    """板块名命中：精确名或去罗马数字后缀名（白酒Ⅱ/白酒 都认）。"""
    n = 0
    for name in names:
        stripped = ROMAN_SUFFIX.sub("", name).strip()
        if name in answer or (stripped and stripped in answer):
            n += 1
    return n


def any_term(answer: str, terms: list[str]) -> bool:
    return any(t.lower() in answer.lower() for t in terms)


# ---------- 各 gold 类型 ----------

def score_quote(g: dict, answer: str, qbase: dict) -> float:
    """qbase: 本题运行前采集的行情 {symbol: {price, change_pct}}。"""
    scores = []
    for sym in g["symbols"]:
        base = (qbase or {}).get(sym) or {}
        p0, c0 = base.get("price"), base.get("change_pct")
        if p0 is None or base.get("error"):
            scores.append(1.0)  # 基准采不到就不扣分（如停牌/采集失败），如实记录
            continue
        nums = numbers_in(answer)
        price_ok = any(abs(n - p0) <= p0 * g["price_tol_rel"] for n in nums)
        pct_ok = any(abs(n - c0) <= g["pct_tol_abs"] for n in nums)
        scores.append(1.0 if price_ok and pct_ok else 0.5 if price_ok or pct_ok else 0.0)
    return sum(scores) / len(scores)


def score_signal_hot(g: dict, answer: str, sig: dict) -> float:
    names = [t["name"] for t in sig.get("hot_top5", [])[: g.get("top_n", 3)]]
    if not names:
        return 1.0  # 基准缺失不扣分
    hits = hit_names(answer, names)
    base = 1.0 if hits >= g.get("names_required", 2) else 0.5 if hits >= 1 else 0.0
    if g.get("stale_required") and not any_term(answer, g.get("stale_terms", [])):
        base = min(base, 0.5)
    return base


def score_signal_cold(g: dict, answer: str, sig: dict) -> float:
    sector_ok = any(
        g["sector"] in t["name"] or t["name"].rstrip("ⅠⅡⅢⅣⅤ") == g["sector"].rstrip("ⅠⅡⅢⅣⅤ")
        for t in sig.get("cold_all", [])
    ) and (g["sector"] in answer or g["sector"].rstrip("ⅠⅡⅢⅣⅤ") in answer)
    cold_word = any_term(answer, g["cold_terms"])
    if not (sector_ok and cold_word):
        return 0.0
    true_rank = sig.get("baijiu2_rank")
    if true_rank is None:
        return 1.0  # 基准里查无排名，只看板块+冷榜两要素
    rank_ok = any(abs(n - true_rank) <= g["rank_tol"] for n in numbers_in(answer))
    return 1.0 if rank_ok else 0.5


def score_kb(g: dict, answer: str) -> float:
    hits = sum(1 for e in g["elements"] if e in answer)
    base = 1.0 if hits >= g["min_elements"] else 0.5 if hits >= 1 else 0.0
    return base


def score_honesty(g: dict, answer: str, prompt: str) -> float:
    extra_nums = numbers_not_in_prompt(answer, prompt)
    if extra_nums:
        return 0.0  # 编了数字：诚实题零容忍
    if any_term(answer, g["required_terms"]):
        return 1.0
    if g.get("suggest_terms") and any_term(answer, g["suggest_terms"]):
        return 0.5
    return 0.0


def score_memory_dup(g: dict, answer: str) -> float:
    return 1.0 if any_term(answer, g["dup_terms"]) else 0.0


def score_numeric(g: dict, answer: str) -> float:
    v, tol = g["value"], g["tol"]
    for n in numbers_in(answer):
        if abs(n - v) <= tol:
            return 1.0
        if "%" in answer and abs(n - v * 100) <= tol * 100:
            return 1.0
    return 0.0


def score_elements(g: dict, answer: str) -> float:
    hits = sum(1 for e in g["elements"] if e in answer)
    return 1.0 if hits >= g["min_elements"] else 0.5 if hits >= 1 else 0.0


def score_citation(g: dict, answer: str) -> float:
    base = score_elements(g, answer)
    cited_ok = any(d in answer for d in g["acceptable_docs"])
    return base if cited_ok else min(base, 0.5)


def score_structure(g: dict, answer: str) -> float:
    hits = sum(1 for s in g["sections"] if s in answer)
    need = g["required"]
    return 1.0 if hits >= need else 0.5 if hits >= max(1, need // 2) else 0.0


def score_kb_doc_mentioned(g: dict, answer: str) -> float:
    return 1.0 if any(d in answer for d in g["docs"]) else 0.0


def score_quote_any(g: dict, answer: str, qbase: dict) -> float:
    ok = 0
    for sym in g["symbols"]:
        base = (qbase or {}).get(sym) or {}
        p0, c0 = base.get("price"), base.get("change_pct")
        if p0 is None or base.get("error"):
            continue
        nums = numbers_in(answer)
        price_ok = any(abs(n - p0) <= p0 * g["price_tol_rel"] for n in nums)
        pct_ok = any(abs(n - c0) <= g["pct_tol_abs"] for n in nums)
        if price_ok and pct_ok:
            ok += 1
    return 1.0 if ok >= g["min_quotes"] else 0.5 if ok >= 1 else 0.0


def score_file_contains(g: dict, answer: str) -> float:
    p = Path(g["path"])
    if not p.is_file():
        return 0.0
    text = p.read_text(encoding="utf-8", errors="replace")
    return 1.0 if g["pattern"] in text else 0.5


SCORERS = {
    "quote": score_quote,
    "signal_hot": score_signal_hot,
    "signal_cold": score_signal_cold,
    "kb": score_kb,
    "honesty": score_honesty,
    "memory_dup": score_memory_dup,
    "numeric": score_numeric,
    "elements": score_elements,
    "citation": score_citation,
    "structure": score_structure,
    "kb_doc_mentioned": score_kb_doc_mentioned,
    "quote_any": score_quote_any,
    "file_contains": score_file_contains,
}


def score_gold(g: dict, answer: str, prompt: str, qbase: dict, sig: dict) -> float:
    if g["type"] == "composite":
        parts = [score_gold(p, answer, prompt, qbase, sig) for p in g["parts"]]
        return sum(parts) / len(parts) if parts else 0.0
    fn = SCORERS[g["type"]]
    if g["type"] in ("quote", "quote_any"):
        return fn(g, answer, qbase)
    if g["type"] in ("signal_hot", "signal_cold"):
        return fn(g, answer, sig)
    if g["type"] == "honesty":
        return fn(g, answer, prompt)
    return fn(g, answer)


def hallucinated(q: dict, answer: str) -> tuple[bool, list[str]]:
    """返回 (是否幻觉, 原因列表)。"""
    reasons = []
    g = q["gold"]
    if g["type"] == "honesty" or (
        g["type"] == "composite" and any(p["type"] == "honesty" for p in g["parts"])
    ):
        extra = numbers_not_in_prompt(answer, q["prompt"])
        if extra:
            reasons.append(f"诚实题出现题目外数字: {extra[:5]}")
    for doc in MD_DOC_RE.findall(answer):
        if doc not in KB_DOCS and doc not in _all_acceptable_docs(q):
            reasons.append(f"引用不存在的文档: {doc}")
    return bool(reasons), reasons


def _all_acceptable_docs(q: dict) -> set[str]:
    out: set[str] = set()
    def walk(g):
        if g["type"] == "composite":
            for p in g["parts"]:
                walk(p)
        else:
            out.update(g.get("acceptable_docs", []) + g.get("docs", []))
    walk(q["gold"])
    return out


# ---------- 主流程 ----------

def score_system(system: str, outdir: Path) -> dict:
    baseline = json.loads((PROJECT / "bench" / "baseline.json").read_text(encoding="utf-8"))
    sig = baseline.get("signal", {})
    rows = []
    for q in BENCH["questions"]:
        rf = outdir / f"{q['id']}.json"
        if not rf.is_file():
            rows.append({"id": q["id"], "domain": q["domain"], "score": None,
                         "latency_s": None, "answer_len": 0, "hallucination": False})
            continue
        r = json.loads(rf.read_text(encoding="utf-8"))
        answer = r.get("answer", "") or ""
        qbase = r.get("quote_baseline") or baseline.get("quotes", {})
        score = score_gold(q["gold"], answer, q["prompt"], qbase, sig)
        hall, why = hallucinated(q, answer)
        rows.append({
            "id": q["id"], "domain": q["domain"], "score": score,
            "latency_s": r.get("latency_s"), "answer_len": len(answer),
            "hallucination": hall, "hallucination_why": why,
        })
    return {"system": system, "rows": rows}


def aggregate(rows: list[dict]) -> dict:
    valid = [r for r in rows if r["score"] is not None]
    by_domain = {}
    for d in ("tools", "knowledge", "research"):
        ds = [r for r in valid if r["domain"] == d]
        by_domain[d] = (sum(r["score"] for r in ds) / len(ds) * 100) if ds else None
    total = sum(r["score"] for r in valid) / len(valid) * 100 if valid else None
    halls = sum(1 for r in valid if r["hallucination"])
    lats = [r["latency_s"] for r in valid if r.get("latency_s")]
    return {
        "total": total, "by_domain": by_domain, "answered": len(valid),
        "hallucination_count": halls,
        "hallucination_rate": halls / len(valid) * 100 if valid else None,
        "avg_latency_s": sum(lats) / len(lats) if lats else None,
    }


def render_md(res: dict) -> str:
    agg = aggregate(res["rows"])
    lines = [
        f"# QRA-Bench v1.0 · {res['system'].upper()} 评测报告",
        f"评分时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 总分（均值）：**{agg['total']:.1f}%**（{agg['answered']}/30 题有结果）",
        f"- 幻觉率：**{agg['hallucination_count']}/{agg['answered']}** = {agg['hallucination_rate']:.0f}%",
        f"- 平均时延：{agg['avg_latency_s']:.1f}s" if agg["avg_latency_s"] else "- 平均时延：无数据",
        "",
        "| 域 | 得分 |",
        "|---|---|",
    ]
    for d, v in agg["by_domain"].items():
        lines.append(f"| {d} | {v:.1f}%" if v is not None else f"| {d} | 缺数据 |")
    lines += ["", "## 逐题明细", "", "| 题号 | 域 | 得分 | 时延s | 幻觉 | 答文字数 |", "|---|---|---|---|---|---|"]
    for r in res["rows"]:
        if r["score"] is None:
            lines.append(f"| {r['id']} | {r['domain']} | ❌无结果 | - | - | - |")
            continue
        hall = "🔴" if r["hallucination"] else ""
        lines.append(f"| {r['id']} | {r['domain']} | {r['score']*100:.0f}% | "
                     f"{r['latency_s']:.0f} | {hall} | {r['answer_len']} |")
    for r in res["rows"]:
        if r.get("hallucination_why"):
            lines.append(f"- {r['id']} 幻觉：{'；'.join(r['hallucination_why'])}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["qra", "cc"], help="单系统评分")
    ap.add_argument("--outdir", help="结果目录（缺省 bench/results/<system>）")
    ap.add_argument("--compare", action="store_true", help="双系统对照")
    args = ap.parse_args()

    if args.compare:
        both = []
        for sys_name in ("qra", "cc"):
            od = PROJECT / "bench" / "results" / sys_name
            if od.is_dir():
                both.append(score_system(sys_name, od))
        if not both:
            print("❌ 无结果目录（bench/results/qra|cc）")
            return 2
        lines = ["# QRA-Bench v1.0 · QRA vs Claude Code 对照",
                 "",
                 "| 题号 | 域 | QRA | CC |",
                 "|---|---|---|---|"]
        rows_map = {r["id"]: r for res in both for r in res["rows"]}
        for q in BENCH["questions"]:
            a = rows_map.get(q["id"])
            lines.append(f"| {q['id']} | {q['domain']} | "
                         f"{a['score']*100:.0f}%" if a and a["score"] is not None else "")
            lines[-1] += f" | |" if a is None or a["score"] is None else ""
        for res in both:
            agg = aggregate(res["rows"])
            lines += [f"## {res['system'].upper()}",
                      f"- 总分 {agg['total']:.1f}% · 幻觉率 {agg['hallucination_rate']:.0f}% · 时延 {agg['avg_latency_s']:.1f}s"]
        out = PROJECT / "bench" / "score_compare.md"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"✅ 对照报告 → {out}")
        return 0

    if not args.system:
        ap.error("必须给 --system 或 --compare")
    od = Path(args.outdir) if args.outdir else PROJECT / "bench" / "results" / args.system
    res = score_system(args.system, od)
    md = render_md(res)
    out = PROJECT / "bench" / f"score_{args.system}.md"
    out.write_text(md, encoding="utf-8")
    agg = aggregate(res["rows"])
    print(f"{args.system.upper()}: 总分 {agg['total']:.1f}% · "
          f"幻觉 {agg['hallucination_count']}/{agg['answered']} · "
          f"时延 {agg['avg_latency_s']:.0f}s · 报告 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
