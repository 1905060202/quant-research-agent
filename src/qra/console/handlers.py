"""P0 命令处理器。所有 vendor 序列照抄官方实现（文件:行号见注释）。

- /resume 序列 = cli_commands_mixin.py:1010-1143（含 #47202 flush、#15000
  压缩链重定向、#6672 on_session_switch、yolo/模型恢复）
- /yolo 语义 = cli.py:11637 _toggle_yolo（session 级 enable/disable + 行持久化）
- /compact 序列 = cli.py:11710 _manual_compress（<4 守卫、system_message=None
  防身份重复 #15281、lock-skip 判据、fork 后 session_id 以 agent 为准、flush
  handoff、finalize committed）
- 持久化形状 = cli.py:_persist_model_switch_to_session（#79536）
"""

from __future__ import annotations

import os
import subprocess
import time

from qra.console import approvals, exporter, models_router
from qra.console.session_state import PRICE_USD, USD_CNY


# ---------------------------------------------------------------- 基础

def _say(ctx, text: str) -> None:
    if ctx.plain:
        print(text)
    else:
        ctx.console.print(text)


def cmd_loop(ctx, args: str) -> None:
    """CC /loop 对齐：自动继续模式（D007 P2 附录第 7 条立项）。

    语义：/loop <prompt> 置位 ctx.loop_prompt，主循环消费后进入循环——
    每轮结束自动以同 prompt 重跑，间隔默认 60s（QRA_LOOP_INTERVAL 覆盖），
    Ctrl+C 任意时刻退出回提示符。进程内实现，不依赖 cron。
    空参数只打印用法（离线，无 API 调用）。
    """
    prompt = args.strip()
    if not prompt:
        _say(ctx, "  用法：/loop <prompt>——每轮自动以同 prompt 继续，Ctrl+C 退出")
        _say(ctx, "  间隔默认 60s（QRA_LOOP_INTERVAL 环境变量可调）；会话级，不落盘")
        return
    ctx.loop_prompt = prompt
    _say(ctx, "  ⟳ 已进入 /loop 模式，Ctrl+C 退出")


def _relative_time(ts) -> str:
    """本地相对时间（不 import hermes_cli.main，避免重型依赖）。"""
    if not ts:
        return "—"
    try:
        dt = time.time() - float(ts)
    except (TypeError, ValueError):
        return "—"
    if dt < 60:
        return f"{int(dt)}s 前"
    if dt < 3600:
        return f"{int(dt // 60)}m 前"
    if dt < 86400:
        return f"{int(dt // 3600)}h 前"
    return f"{int(dt // 86400)}d 前"


def _list_recent(ctx, limit: int = 10):
    """最近会话列表（vendor _list_recent_sessions 同款过滤面）。"""
    from hermes_cli.session_listing import query_session_listing
    try:
        return query_session_listing(
            ctx.db, source="cli",
            exclude_sources=["kanban", "tool"],
            limit=limit,
        )
    except Exception:
        return []


def _show_sessions(ctx, rows, hint: str = "") -> None:
    if not rows:
        _say(ctx, "（没有可列出的会话）")
        return
    if ctx.plain:
        for i, r in enumerate(rows, 1):
            title = r.get("title") or "(未命名)"
            _say(ctx, f"{i}. {title}  {r.get('id')}  "
                      f"msg={r.get('message_count') or 0}  "
                      f"{_relative_time(r.get('last_active') or r.get('started_at'))}")
    else:
        from rich.table import Table
        t = Table(title="最近会话")
        t.add_column("#", justify="right", style="dim")
        t.add_column("标题")
        t.add_column("会话 ID", style="dim")
        t.add_column("消息", justify="right")
        t.add_column("最后活动")
        for i, r in enumerate(rows, 1):
            title = (r.get("title") or "(未命名)")[:32]
            t.add_row(
                str(i), title, r.get("id") or "—",
                str(r.get("message_count") or 0),
                _relative_time(r.get("last_active") or r.get("started_at")),
            )
        ctx.console.print(t)
    if hint:
        _say(ctx, hint)


def _resolve_target(ctx, arg: str):
    """目标解析：纯数字→待选号列表下标；否则 ID / 标题直查。

    返回 (session_id, meta) 或 (None, 错误文本)。
    """
    if arg.isdigit():
        rows = ctx.pending.get("resume") or _list_recent(ctx)
        idx = int(arg) - 1
        if 0 <= idx < len(rows):
            sid = rows[idx].get("id")
            meta = ctx.db.get_session(sid) or {}
            return sid, meta
        return None, f"待选号 {arg} 超出范围（1-{len(rows)}）"
    meta = None
    try:
        meta = ctx.db.get_session(arg)
    except Exception:
        pass
    if not meta:
        try:
            meta = ctx.db.get_session_by_title(arg)
        except Exception:
            pass
    if not meta:
        return None, f"找不到会话：{arg}（/resume 无参列出可选）"
    return meta.get("id"), meta


# ---------------------------------------------------------------- 命令

def cmd_resume(ctx, args: str) -> None:
    agent, db, sess = ctx.agent, ctx.db, ctx.sess
    arg = (args or "").strip()
    if not arg:
        rows = _list_recent(ctx)
        ctx.pending["resume"] = rows
        _show_sessions(ctx, rows, "输入 /resume <数字|ID|标题> 恢复会话（直接敲数字也可）")
        return
    target_id, meta_or_err = _resolve_target(ctx, arg)
    if target_id is None:
        _say(ctx, meta_or_err)
        return
    ctx.pending.pop("resume", None)

    # #15000：压缩链空头 → 重定向到持消息的后代
    try:
        resolved_id = db.resolve_resume_session_id(target_id)
    except Exception:
        resolved_id = target_id
    if resolved_id and resolved_id != target_id:
        _say(ctx, f"  会话 {target_id} 已被压缩进 {resolved_id}，恢复到后代。")
        target_id = resolved_id
        meta = db.get_session(target_id) or {}
    else:
        meta = meta_or_err

    if target_id == agent.session_id:
        _say(ctx, "  已在该会话上。")
        return

    old_session_id = agent.session_id
    # #47202：切换前冲刷未落盘消息
    if sess.history:
        try:
            agent._flush_messages_to_session_db(
                sess.history, conversation_history=sess.history)
        except Exception:
            pass
    try:
        db.end_session(old_session_id, "resumed_other")
    except Exception:
        pass

    agent.session_id = target_id
    sess.session_id = target_id
    approvals.sync_session_key(target_id)

    try:
        model_history, _display = db.get_resume_conversations(target_id)
    except Exception:
        model_history = []
    restored = [m for m in (model_history or []) if m.get("role") != "session_meta"]
    # 跨路由重放：最终路由若是 opus（CC proxy），剥掉 thinking/reasoning 块
    if models_router.infer_route_name(getattr(agent, "base_url", "") or "") == "opus":
        restored = approvals.strip_reasoning(restored)
    sess.history = restored
    sess._title_set = False   # 会话已换：新会话首条消息可再触发自动标题

    try:
        db.reopen_session(target_id)
    except Exception:
        pass

    agent.reset_session_state()
    if hasattr(agent, "_last_flushed_db_idx"):
        agent._last_flushed_db_idx = len(sess.history)
    if hasattr(agent, "_todo_store"):
        try:
            from tools.todo_tool import TodoStore
            agent._todo_store = TodoStore()
        except Exception:
            pass
    if hasattr(agent, "_invalidate_system_prompt"):
        agent._invalidate_system_prompt()
    # #6672：内存提供方感知会话轮换（reset=False，累积态仍有效）
    try:
        _mm = getattr(agent, "_memory_manager", None)
        if _mm is not None:
            _mm.on_session_switch(
                target_id, parent_session_id=old_session_id or "",
                reset=False, reason="resume",
            )
    except Exception:
        pass

    # 恢复目标会话的 yolo（cli._restore_session_yolo 语义）
    try:
        from hermes_state import SessionDB
        from tools.approval import (
            _YOLO_MODE_FROZEN, enable_session_yolo, is_session_yolo_enabled,
        )
        if not _YOLO_MODE_FROZEN and SessionDB.session_yolo_enabled(meta or {}):
            if not is_session_yolo_enabled(target_id or "default"):
                enable_session_yolo(target_id or "default")
    except Exception:
        pass
    sess.yolo = True
    try:
        from tools.approval import is_session_yolo_enabled
        sess.yolo = is_session_yolo_enabled(target_id or "default")
    except Exception:
        pass

    # 恢复目标会话的路由（persisted model/gateway_runtime）
    warn = models_router.restore_route(agent, db, sess, meta or {})
    if warn:
        _say(ctx, f"  ⚠ {warn}")

    title_part = f" \"{meta['title']}\"" if (meta or {}).get("title") else ""
    if sess.history:
        _say(ctx, f"  ↻ 已恢复会话 {target_id}{title_part}"
                  f"（{len(sess.history)} 条消息）")
    else:
        _say(ctx, f"  ↻ 已恢复会话 {target_id}{title_part} — 无消息，重新开始。")


def cmd_sessions(ctx, args: str) -> None:
    rows = _list_recent(ctx)
    ctx.pending["resume"] = rows
    _show_sessions(ctx, rows, "/resume <数字|ID|标题> 恢复会话")


def cmd_clear(ctx, args: str) -> None:
    agent, db, sess = ctx.agent, ctx.db, ctx.sess
    old_id = agent.session_id
    if sess.history:
        try:
            db.end_session(old_id, "user_cleared")
        except Exception:
            pass
    from qra.console.session_state import new_session_id
    new_id = new_session_id()
    agent.session_id = new_id
    sess.session_id = new_id
    agent.reset_session_state()
    # 新会话键的 yolo 先立起来：_ensure_db_session 创建行时会把活动 yolo
    # 写进 model_config（run_agent.py:630 注释），错过就没有第二次机会
    from tools.approval import enable_session_yolo, disable_session_yolo
    key = new_id or "default"
    if sess.yolo:
        enable_session_yolo(key)
    else:
        disable_session_yolo(key)
    # 急切建行（#15000 类守卫：不建行则 /clear 后永远无审计行）
    agent._session_db_created = False
    agent._ensure_db_session()
    approvals.sync_session_key(new_id)
    sess.history = []
    sess._title_set = False
    _say(ctx, f"  ✨ 新会话 {new_id}（旧会话 {old_id} 已保留在库里）")


def cmd_export(ctx, args: str) -> None:
    arg = (args or "").strip().lower()
    fmt = arg if arg in ("md", "jsonl") else "md"
    try:
        path = exporter.export_session(ctx.db, ctx.sess.session_id, fmt=fmt)
    except Exception as exc:
        _say(ctx, f"  导出失败：{exc}")
        return
    _say(ctx, f"  📄 已导出：{path}")


def cmd_usage(ctx, args: str) -> None:
    agent, sess = ctx.agent, ctx.sess
    it = getattr(agent, "session_input_tokens", 0) or 0
    ot = getattr(agent, "session_output_tokens", 0) or 0
    cr = getattr(agent, "session_cache_read_tokens", 0) or 0
    rt = getattr(agent, "session_reasoning_tokens", 0) or 0
    calls = getattr(agent, "session_api_calls", 0) or 0
    cost_usd = getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0
    usd = (it * PRICE_USD["input"] + ot * PRICE_USD["output"]
           + cr * PRICE_USD["cache_read"]) / 1_000_000
    lines = [
        f"输入 {it:,} tok · 输出 {ot:,} tok · 缓存读 {cr:,} tok · 推理 {rt:,} tok",
        f"API 调用 {calls} 次",
        f"估算费用 ${usd:.4f}（约 ¥{usd * USD_CNY:.3f}）",
    ]
    if cost_usd:
        lines.append(f"agent 侧估算 ${cost_usd:.4f}（约 ¥{cost_usd * USD_CNY:.3f}）")
    try:
        credits = agent.get_credits_state()
    except Exception:
        credits = None
    if credits is not None:
        try:
            spent = agent.get_credits_spent_micros()
        except Exception:
            spent = None
        rem = getattr(credits, "remaining_micros", None)
        lines.append(f"credits 剩余 ${rem / 1e6:.2f}"
                     + (f" · 本会话消耗 ${spent / 1e6:.2f}" if spent is not None else ""))
    for line in lines:
        _say(ctx, "  " + line)


def cmd_status(ctx, args: str) -> None:
    agent, sess = ctx.agent, ctx.sess
    try:
        act = agent.get_activity_summary() or {}
    except Exception:
        act = {}
    desc = act.get("last_activity_description") or act.get("last_activity_desc") or "—"
    uptime = int(time.time() - sess.started_at)
    route = sess.route_name or models_router.infer_route_name(
        getattr(agent, "base_url", "") or "")
    yolo = "开" if sess.yolo else "关"
    lines = [
        f"会话：{sess.session_id}",
        f"模型：{agent.model}（provider={agent.provider or '—'}）· 路由：{route}",
        f"YOLO：{yolo} · 历史 {len(sess.history)} 条 · 运行 {uptime // 60}m{uptime % 60}s",
        f"最近活动：{desc}",
    ]
    for line in lines:
        _say(ctx, "  " + line)


def cmd_model(ctx, args: str) -> None:
    agent, db, sess = ctx.agent, ctx.db, ctx.sess
    arg = (args or "").strip().lower()
    if not arg:
        cur = sess.route_name or models_router.infer_route_name(
            getattr(agent, "base_url", "") or "")
        lines = [
            "  ● deepseek  直连（config 主模型）" if cur == "deepseek"
            else "  ○ deepseek  直连（config 主模型）",
            "  ● opus      CC proxy 127.0.0.1:8789" if cur == "opus"
            else "  ○ opus      CC proxy 127.0.0.1:8789",
        ]
        for line in lines:
            _say(ctx, line)
        _say(ctx, "  用法：/model <deepseek|opus>")
        return
    if arg not in models_router.ROUTE_NAMES:
        _say(ctx, f"  未知路由：{arg}（可选 {', '.join(models_router.ROUTE_NAMES)}）")
        return
    err = models_router.switch_route(agent, db, sess, arg)
    if err:
        _say(ctx, f"  ⚠ {err}")
        return
    _say(ctx, f"  ✅ 已切换到 {arg} 路由：{agent.model} @ {agent.base_url or '(默认)'}")


def cmd_memory(ctx, args: str) -> None:
    try:
        from tools.memory_tool import get_memory_dir
        mem = get_memory_dir()
    except Exception:
        mem = None
    if mem is None:
        _say(ctx, "  ⚠ 拿不到记忆目录")
        return
    target = mem / "MEMORY.md"
    if not target.exists():
        _say(ctx, f"  记忆目录：{mem}（MEMORY.md 不存在）")
        return
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    ctx.inp.pause()
    try:
        subprocess.call([editor, str(target)])
    except Exception as exc:
        _say(ctx, f"  ⚠ 编辑器启动失败：{exc}")
    finally:
        ctx.inp.resume()


def cmd_compact(ctx, args: str) -> None:
    agent, sess = ctx.agent, ctx.sess
    if len(sess.history) < 4:
        _say(ctx, "  (._.) 消息不足 4 条，无法压缩。")
        return
    original = list(sess.history)
    try:
        from agent.model_metadata import estimate_request_tokens_rough
        approx_tokens = estimate_request_tokens_rough(
            original,
            system_prompt=getattr(agent, "_cached_system_prompt", "") or "",
            tools=getattr(agent, "tools", None) or None,
        )
    except Exception:
        approx_tokens = None
    _say(ctx, f"  🗜️ 压缩 {len(original)} 条消息（~{approx_tokens or '?'} tokens）…")
    try:
        # system_message=None：_build_system_prompt(None) 从头重建，防身份块
        # 重复（#15281）；defer 通知由下方 finalize 提交
        compressed, _ = agent._compress_context(
            original, None,
            approx_tokens=approx_tokens,
            force=True,
            defer_context_engine_notification=True,
        )
    except Exception as exc:
        _say(ctx, f"  ⚠ 压缩失败：{exc}")
        return
    # 并发锁跳过（run_agent.py:_compress_context 置 _compression_skipped_due_to_lock）
    from agent.conversation_compression import (
        finalize_context_engine_compression_notification,
    )
    lock_sig = getattr(agent, "_compression_skipped_due_to_lock", None)
    if lock_sig is True or isinstance(lock_sig, str):
        agent._compression_skipped_due_to_lock = None
        finalize_context_engine_compression_notification(agent, committed=False)
        _say(ctx, "  ⚠ 另有压缩进行中，本次跳过。")
        return
    if len(compressed) == len(original):
        finalize_context_engine_compression_notification(agent, committed=False)
        _say(ctx, "  (._.) 压缩无变化。")
        return
    sess.history = compressed
    # fork 已发生：session_id 一律以 agent 为准（cli.py:11869-11890）
    if getattr(agent, "session_id", None) and agent.session_id != sess.session_id:
        sess.session_id = agent.session_id
        approvals.sync_session_key(sess.session_id)
        try:
            agent._flush_messages_to_session_db(compressed, None)
        except Exception:
            pass
    finalize_context_engine_compression_notification(agent, committed=True)
    _say(ctx, f"  ✅ 压缩完成：{len(original)} → {len(compressed)} 条"
              f"（新会话 {sess.session_id}）")


def cmd_yolo(ctx, args: str) -> None:
    """cli.py:11637 _toggle_yolo 语义：session 级开关 + 行持久化。"""
    agent, db, sess = ctx.agent, ctx.db, ctx.sess
    from tools.approval import (
        disable_session_yolo, enable_session_yolo, is_session_yolo_enabled,
    )
    key = agent.session_id or "default"
    if is_session_yolo_enabled(key):
        disable_session_yolo(key)
        if db is not None and key != "default":
            try:
                db.set_session_yolo(key, False)
            except Exception:
                pass
        sess.yolo = False
        _say(ctx, "  ⚠ YOLO 已关闭 — 危险命令需审批。")
        _say(ctx, "    ! 命令可在本终端交互式审批；agent 自身发起的危险命令将自动拒绝。")
    else:
        enable_session_yolo(key)
        if db is not None and key != "default":
            try:
                db.set_session_yolo(key, True)
            except Exception:
                pass
        sess.yolo = True
        _say(ctx, "  ⚡ YOLO 已开启 — 所有命令自动放行，请谨慎使用。")
