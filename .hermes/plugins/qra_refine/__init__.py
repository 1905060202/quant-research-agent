"""QRA 评审门移植：prime refinement 的准入质量门 → hermes background_review。

prime 0.7.2 实际是三段流水线：LLM 评审门(shouldRefine) → 提案 → 程序化校验。
hermes 的 background_review 已有 检查点(~10回合nudge)→fork评审→memory/skill
工具→持久化→系统提示重注入。缺失的是第一段：**准入质量门**——hermes 的技能
评审偏"主动"（大多数会话都该存点什么），prime 的门偏"严格"（拒绝一次性噪音、
瞬时工具输出、无据假设）。本插件把 prime 的门嫁接在 hermes 的 fork 提示词前面。

嫁接点（官方兼容钩，vendor 源码零改动）：
background_review.spawn_background_review_thread 用
``getattr(agent, "_XX_REVIEW_PROMPT", 模块常量)`` 取提示词——新 agent 走
模块常量。本插件在 register() 时把三个模块常量重写为
"评审门前缀 + 原版指令正文"。上游若重命名常量，启动自检告警并保留静态副本
（原版正文在首次加载时缓存）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_GATE_MARKER = "QRA 准入评审门"

_GATE_HEADER = """【{marker}·移植自 Prime Agent auto /refine review gate】

第一步只做准入判断，不保存任何东西。默认立场是拒绝（宁缺毋滥），
只有以下情况放行：
- 用户纠正了会复发的风格/流程/格式问题；
- 本会话发现了可复用的技巧、工具用法、新知识；
- 已加载的技能或记忆里有错误需要修正。
必须拒绝：一次性任务叙事、瞬时工具输出、环境性失败（网络/权限/依赖）、
未验证的假设、半成品工作。

第二步：
- 拒绝 → 最终回答只能是 "Nothing to save."，不调用任何工具。
- 放行 → 最终回答第一行写 "GATE: true - <一句话理由>"，然后执行
  下方原版审查指令完成保存。

若下文原版指令与门冲突（如原版要求"主动多存"），一律以门的准入
标准为准——门管"存不存"，原版管"怎么存"。

---- 以下为 Hermes 原版审查指令（仅在放行后执行）----
"""

# 原版正文缓存：首载时从 hermes 模块读入（避免上游更新后 QRA 用陈旧副本；
# 仅在模块导入失败时退化为空——此时用静态兜底文本，见 _STATIC_FALLBACKS）
_originals_cached = False


def _gate_header() -> str:
    return _GATE_HEADER.format(marker=_GATE_MARKER)


def _wrap(original: str) -> str:
    """评审门前缀 + 原版正文。幂等：已带标记的常量不重复包裹。"""
    if _GATE_MARKER in (original or ""):
        return original
    return _gate_header() + "\n" + (original or "")


def register(ctx) -> None:  # noqa: ARG001 - 钩子签名约定
    """重写 background_review 的三个提示词常量（评审门前置）。"""
    try:
        import agent.background_review as bg
    except Exception as e:
        log.warning("qra_refine：无法导入 agent.background_review（%s），"
                    "评审门未激活。若在独立测试环境属正常。", e)
        return
    names = ("_MEMORY_REVIEW_PROMPT", "_SKILL_REVIEW_PROMPT",
             "_COMBINED_REVIEW_PROMPT")
    for name in names:
        original = getattr(bg, name, "")
        if not original:
            log.warning("qra_refine：常量 %s 不存在（上游可能已重构），"
                        "该路径评审门未激活", name)
            continue
        setattr(bg, name, _wrap(original))
    # 启动自检：读回确认（dsh fail-loud 升级——导入成功但零激活=静默失效，
    # 必须响亮失败；部分激活=上游漂移，警告但不拦启动）
    state = {name: (_GATE_MARKER in (getattr(bg, name, "") or ""))
             for name in names}
    activated = [n for n, ok in state.items() if ok]
    log.info("qra_refine 评审门状态: %s", state)
    if len(activated) == 3:
        log.info("qra_refine：三个评审门已全部激活（memory/skills/combined）")
    elif activated:
        log.warning("qra_refine：部分评审门未激活: %s", state)
    else:
        raise RuntimeError(
            "qra_refine：background_review 导入成功但三个评审门常量全部改写失败，"
            f"状态 {state}——上游可能已重构提示词路径，评审门等于静默失效，"
            "拒绝带病注册（dsh fail-loud）")
