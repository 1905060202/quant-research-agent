"""QRA W2 · LLM 客户端（Anthropic 协议 → DeepSeek）

复用 agent 体系现有配置：~/.claude/settings.json 的 ANTHROPIC_* 环境变量
"""
import os, json, requests

def _load_config():
    """从 claude settings 加载 LLM 配置"""
    settings = os.path.expanduser("~/.claude/settings.json")
    if os.path.exists(settings):
        env = json.load(open(settings)).get("env", {})
        base = env.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
        key = env.get("ANTHROPIC_AUTH_TOKEN", "")
        model = env.get("ANTHROPIC_MODEL", "deepseek-v4-flash")
        return base, key, model
    # 兜底环境变量
    return (os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"),
            os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
            os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash"))

def chat(system: str, user: str, max_tokens: int = 800) -> str:
    """单轮 LLM 调用（Anthropic messages API）"""
    base, key, model = _load_config()
    url = base.rstrip("/") + "/v1/messages"
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    resp = requests.post(url, json=payload,
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                  "content-type": "application/json"},
                         timeout=60)
    if resp.status_code != 200:
        return f"[LLM错误 {resp.status_code}] {resp.text[:200]}"
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

if __name__ == "__main__":
    # 自检
    r = chat("你是测试助手", "说'连接成功'四个字")
    print("LLM 自检:", r)
