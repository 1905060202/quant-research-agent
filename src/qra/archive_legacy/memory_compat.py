
# ---- 兼容旧接口（remember/recall）----
def remember(key: str, value: str):
    """旧接口：写入 memory 层（按 slug 去重）"""
    upsert("memory", f"{key}: {value}", value)

def recall(key: str, limit: int = 5) -> str:
    mem = _load()
    items = [e for e in mem["memory"].values() if e["title"].startswith(key + ":")]
    if not items:
        return "（暂无长期记忆）"
    return "\n".join(f"- {e['title'].split(': ',1)[-1]}" for e in items[-limit:])
