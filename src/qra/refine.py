"""QRA v5 · 双评审门精炼回路（D7 · 学 prime-agent）

门1（便宜）：评审该不该学（拒绝噪音/未证假设/瞬时输出）
门2（贵）：规划学什么（结构化 edits：memory/skill）
"""
import json, re
from llm import chat
from memory import upsert, record_refinement

REVIEW_PROMPT = """你是 QRA 的精炼评审门。判断这段对话轨迹是否有值得记住的内容。
规则：
- 拒绝：一次性噪音、未证实的假设、瞬时工具输出
- 接受：可复用的方法论、重复出现的模式、稳定的用户偏好、重要的结论
输出 JSON：{"shouldLearn": true/false, "rationale": "理由"}"""

PLAN_PROMPT = """你是 QRA 的学习规划器。基于对话轨迹，输出要写入记忆的内容。
规则：
- 只写可复用知识（方法论/模式/偏好/结论）
- 每条 ≤200 字
- 不写瞬时状态
输出 JSON：{"edits": [{"kind": "memory|skill", "title": "...", "content": "..."}]}"""

def review_trajectory(trajectory: str) -> dict:
    """门1：小预算评审"""
    resp = chat(REVIEW_PROMPT, trajectory[:4000], max_tokens=300)
    try:
        return json.loads(resp)
    except Exception:
        return {"shouldLearn": False, "rationale": f"评审解析失败: {resp[:100]}"}

def plan_edits(trajectory: str) -> list[dict]:
    """门2：大预算规划"""
    resp = chat(PLAN_PROMPT, trajectory[:8000], max_tokens=1500)
    try:
        data = json.loads(resp)
        return data.get("edits", [])
    except Exception:
        return []

def learn_from_trajectory(trajectory: str, trigger: str = "auto") -> dict:
    """完整精炼回路：评审→规划→写入→记录"""
    review = review_trajectory(trajectory)
    if not review.get("shouldLearn"):
        return {"learned": False, "rationale": review.get("rationale", "不需要")}
    edits = plan_edits(trajectory)
    results = []
    for e in edits:
        kind = e.get("kind", "memory")
        if kind not in ("memory", "skill"):
            continue
        r = upsert(kind, e["title"], e["content"][:200])
        results.append({"title": e["title"], "ok": r.get("ok", False),
                        "error": r.get("error")})
    rid = record_refinement(trigger, [e["title"] for e in edits],
                            evidence=review.get("rationale", ""))
    return {"learned": True, "edits": results, "refinement_id": rid}
