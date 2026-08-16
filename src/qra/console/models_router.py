"""双路由切换：deepseek（直连） ↔ opus（CC proxy 127.0.0.1:8789）。

AIAgent.switch_model（agent_runtime_helpers.py:2465）原地换客户端 + 失败快照回滚；
provider 变更时 base_url 为空会 ValueError → 路由表必须显式给足 base_url。
持久化照抄 cli._persist_model_switch_to_session：update_session_model +
patch_session_model_config({"gateway_runtime": route, **route})，
/resume 经 SessionDB.session_gateway_runtime 读回。
"""

from __future__ import annotations

# 主路由（build_agent 构造完成后捕获：model/provider/base_url/api_mode
# 全部来自真实 runtime，切换回来时原样落地）
_primary: dict | None = None


def capture_primary(agent) -> None:
    """build_agent 构造完成后调用：记录 deepseek 主路由的完整落地参数。"""
    global _primary
    _primary = {
        "model": getattr(agent, "model", "") or "",
        "provider": getattr(agent, "provider", None) or None,
        "base_url": getattr(agent, "base_url", "") or "",
        "api_mode": getattr(agent, "api_mode", "") or "",
        "api_key": "",
    }


def _opus_route() -> dict:
    """opus 路由：config console.routes.opus 可覆盖，否则默认 CC proxy。"""
    route = {"model": "opus", "provider": "anthropic",
             "base_url": "http://127.0.0.1:8789",
             "api_key": "", "api_mode": ""}
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        over = (cfg.get("console") or {}).get("routes", {}).get("opus") or {}
        for k in ("model", "provider", "base_url", "api_key", "api_mode"):
            if over.get(k):
                route[k] = over[k]
    except Exception:
        pass
    return route


ROUTE_NAMES = ("deepseek", "opus")


def _route_for(name: str) -> dict:
    if name == "deepseek":
        if _primary is None:
            raise ValueError("主路由未捕获（deepseek 直连参数缺失），无法切换")
        return dict(_primary)
    if name == "opus":
        return _opus_route()
    raise ValueError(f"未知路由：{name}")


def infer_route_name(base_url: str) -> str:
    return "opus" if base_url and "8789" in base_url else "deepseek"


def switch_route(agent, db, sess, name: str) -> str | None:
    """切换到指定路由；成功更新 sess + 会话行持久化。失败返回错误文本。

    switch_model 内部已快照回滚，失败时 agent 保持旧路由（vendor 保证）。
    """
    try:
        route = _route_for(name)
    except ValueError as exc:
        return str(exc)
    try:
        agent.switch_model(
            route["model"], route["provider"],
            api_key=route.get("api_key") or "",
            base_url=route.get("base_url") or "",
            api_mode=route.get("api_mode") or "",
        )
    except Exception as exc:
        return f"切换失败：{exc}"
    sess.model = agent.model
    sess.provider = agent.provider
    sess.base_url = agent.base_url or ""
    sess.api_mode = agent.api_mode or ""
    sess.route_name = name
    if db is not None and sess.session_id:
        try:
            # 持久化照抄 cli._persist_model_switch_to_session（#79536 形状）：
            # update_session_model 不带 provider kwarg；route 一律 or-None
            # （_merge_model_config_json 只删显式 None，falsy 会留陈旧键）；
            # gateway_runtime 与顶层键同写——CLI 读嵌套、TUI 网关读顶层。
            db.update_session_model(sess.session_id, agent.model)
            rt = {
                "provider": agent.provider or None,
                "base_url": agent.base_url or None,
                "api_mode": agent.api_mode or None,
            }
            db.patch_session_model_config(
                sess.session_id, {"gateway_runtime": rt, **rt})
        except Exception:
            pass
    return None


def restore_route(agent, db, sess, meta: dict) -> str | None:
    """/resume 时从会话行恢复持久化路由（判据照抄 cli._restore_session_model）。

    无持久化 model 或与当前一致 → 不切换，仅推断 route_name。
    持久化路由失效（base_url 缺失 / 旧行）→ 保留当前路由，返回警告文本。
    """
    from hermes_state import SessionDB
    stored_model = (meta or {}).get("model")
    if not stored_model:
        sess.route_name = infer_route_name(getattr(agent, "base_url", "") or "")
        return None
    stored = SessionDB.session_gateway_runtime(meta or {}) or {}
    stored_provider = stored.get("provider") or None
    stored_base_url = stored.get("base_url") or None
    stored_api_mode = stored.get("api_mode") or None
    model_changed = stored_model != getattr(agent, "model", "")
    provider_changed = (bool(stored_provider)
                        and stored_provider != getattr(agent, "provider", None))
    if not model_changed and not provider_changed:
        sess.route_name = infer_route_name(getattr(agent, "base_url", "") or "")
        return None
    try:
        agent.switch_model(
            stored_model, stored_provider,
            base_url=stored_base_url or "",
            api_mode=stored_api_mode or "",
        )
    except Exception as exc:
        sess.route_name = infer_route_name(getattr(agent, "base_url", "") or "")
        return f"会话路由恢复失败（保留当前路由）：{exc}"
    sess.model = agent.model
    sess.provider = agent.provider
    sess.base_url = agent.base_url or ""
    sess.api_mode = agent.api_mode or ""
    sess.route_name = infer_route_name(sess.base_url)
    return None
