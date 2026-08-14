"""QRA v5 · 记忆层（D2 字符预算版 · 学 Hermes）

核心：字符硬预算（不用 token——模型无关）+ 满即报错逼合并 + 原子写
四类：memory(事实) / skill(方法论) / prompt(行为) / subagent(委托)
"""
import json, os, re, datetime

MEM_FILE = os.path.expanduser("~/hermes_output/career/tools/quant_research_agent/data/harness.json")
_BUDGETS = {"memory": 1500, "skill": 3000, "prompt": 500, "subagent": 1500}  # 每类字符预算
_cache = None

def _slug(raw: str) -> str:
    norm = "".join(ch.lower() if ch.isalnum() else "_" for ch in raw.strip())
    norm = "_".join(p for p in norm.split("_") if p)
    return (norm or "untitled")[:80]

def _load():
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(MEM_FILE):
        _cache = json.load(open(MEM_FILE, encoding="utf-8"))
        return _cache
    _cache = {"memory": {}, "skill": {}, "prompt": {}, "subagent": {}, "refinements": []}
    return _cache

def _save():
    os.makedirs(os.path.dirname(MEM_FILE), exist_ok=True)
    tmp = MEM_FILE + ".tmp"
    json.dump(_cache, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, MEM_FILE)  # 原子写

def _usage(kind: str) -> int:
    return sum(len(e["content"]) for e in _load()[kind].values())

def upsert(kind: str, title: str, content: str) -> dict:
    """D2: 写入前检查预算；超预算返回错误让调用方合并"""
    mem = _load()
    eid = _slug(title)
    existing = mem[kind].get(eid, {})
    old_len = len(existing.get("content", ""))
    new_len = len(content)
    # 预算检查：新条目总字符（减旧条目）不能超
    projected = _usage(kind) - old_len + new_len
    if projected > _BUDGETS[kind]:
        return {"ok": False, "error": f"budget_exceeded",
                "usage": _usage(kind), "budget": _BUDGETS[kind],
                "projected": projected,
                "hint": "记忆已满，请先合并/删除旧条目再写入"}
    mem[kind][eid] = {"id": eid, "title": title, "content": content,
                      "ts": datetime.datetime.now().isoformat(),
                      "version": existing.get("version", 0) + 1}
    _save()
    return {"ok": True, "id": eid, "usage": _usage(kind), "budget": _BUDGETS[kind]}

def get(kind: str, eid: str):
    return _load()[kind].get(eid)

def list_kind(kind: str):
    return list(_load()[kind].values())

def delete(kind: str, eid: str) -> bool:
    if eid in _load()[kind]:
        del _load()[kind][eid]
        _save()
        return True
    return False

def usage_report() -> str:
    return "\n".join(f"{k}: {_usage(k)}/{v} 字符 ({_usage(k)/v*100:.0f}%)" for k, v in _BUDGETS.items())

def record_refinement(trigger: str, changes: list[str], evidence: str = "") -> str:
    mem = _load()
    rid = f"refine_{len(mem['refinements']) + 1:04d}"
    mem["refinements"].append({"id": rid, "trigger": trigger, "changes": changes,
                               "evidence": evidence,
                               "ts": datetime.datetime.now().isoformat()})
    _save()
    return rid

def plan_refinement(observation: str, component: str = "") -> list[str]:
    target = f" for {component}" if component else ""
    return [
        f"Diagnose: {observation}{target}",
        "Update the smallest useful memory/skill entry.",
        "Run next action, then record outcome.",
    ]

# 兼容旧接口
def remember(key: str, value: str):
    return upsert("memory", f"{key}: {value}", value)

def recall(key: str, limit: int = 5) -> str:
    items = [e for e in list_kind("memory") if e["title"].startswith(key + ":")]
    if not items:
        return "（暂无长期记忆）"
    return "\n".join(f"- {e['title'].split(': ',1)[-1]}" for e in items[-limit:])
