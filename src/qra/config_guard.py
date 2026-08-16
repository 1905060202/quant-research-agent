"""QRA 配置门卫：dsh 精华吸收（fail-loud 启动自检 + 配置 schema 硬校验）。

dsh（deepseek-harness，cordis）的工程纪律：非法配置启动即拒、契约前置、
不带病运行。QRA 借底座（hermes），缺的就是这两条——此模块补齐。

只硬校验 QRA 自有键（config.yaml 注释里声明过归属的），不动 hermes
核心键：model/provider/plugins.enabled/memory.provider/approvals.timeout/
model_overrides。未知插件名不判错（用户可自行加 hermes 插件），结构违规
才判错。缺 config.yaml 属硬错：QRA 定制层（插件/记忆/审批）全部依赖它，
hermes 会用默认值静默裸跑——这正是要拦住的「带病运行」。

控制台入口（src/qra/console/main.py）启动时调用 guard_config()：
有违规 → 红色面板 + exit 2。`qra -z` 走 hermes CLI 路径不经此处，
属已知范围限制（P1 记录于 D007 附录）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# QRA 自有键的合法域（与 .hermes/config.yaml 注释一一对应）
KNOWN_PLUGINS = {"qra", "qra_verify", "qra_refine", "qra_memory", "qra_python"}
KNOWN_MODELS = {"deepseek-v4-pro", "deepseek-v4-flash"}


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(env) if env else Path.home() / ".hermes"


def validate_config(cfg: dict) -> list[str]:
    """纯函数：校验 QRA 自有键，返回问题列表（空 = 通过）。"""
    problems: list[str] = []

    model = cfg.get("model")
    if not isinstance(model, dict):
        problems.append("model 必须是对象（映射表）")
    else:
        default = model.get("default")
        if default not in KNOWN_MODELS:
            problems.append(
                f"model.default 必须是 {sorted(KNOWN_MODELS)} 之一，"
                f"当前：{default!r}（厂商前缀形式只对聚合器 provider 有效）")
        if model.get("provider") != "anthropic":
            problems.append(
                f"model.provider 必须是 \"anthropic\"（API 只认裸名），"
                f"当前：{model.get('provider')!r}")

    plugins = cfg.get("plugins")
    if not isinstance(plugins, dict):
        problems.append("plugins 必须是对象（映射表）")
    else:
        enabled = plugins.get("enabled")
        if not isinstance(enabled, list) or not all(
                isinstance(x, str) and x.strip() for x in enabled):
            problems.append(
                f"plugins.enabled 必须是非空字符串列表，当前：{enabled!r}")

    memory = cfg.get("memory")
    if not isinstance(memory, dict):
        problems.append("memory 必须是对象（映射表）")
    elif memory.get("provider") != "qra_memory":
        problems.append(
            f"memory.provider 必须是 \"qra_memory\"（QRA 持久记忆层），"
            f"当前：{memory.get('provider')!r}")

    approvals = cfg.get("approvals")
    if not isinstance(approvals, dict):
        problems.append("approvals 必须是对象（映射表）")
    else:
        timeout = approvals.get("timeout")
        if not isinstance(timeout, int) or timeout <= 0:
            problems.append(
                f"approvals.timeout 必须是正整数（秒），当前：{timeout!r}")

    overrides = cfg.get("model_overrides")
    if not isinstance(overrides, dict):
        problems.append("model_overrides 必须是对象（映射表）")
    else:
        opus_ctx = overrides.get("anthropic", {}).get("opus", {}).get(
            "context_window") if isinstance(overrides.get("anthropic"), dict) \
            and isinstance(overrides.get("anthropic", {}).get("opus"), dict) else None
        if opus_ctx is not None and (not isinstance(opus_ctx, int) or opus_ctx <= 0):
            problems.append(
                f"model_overrides.anthropic.opus.context_window 必须是正整数，"
                f"当前：{opus_ctx!r}")

    return problems


def guard_config() -> None:
    """控制台启动入口：加载 $HERMES_HOME/config.yaml 并硬校验，违规 exit 2。"""
    path = _hermes_home() / "config.yaml"
    if not path.exists():
        print(f"❌ QRA 配置缺失：{path}", file=sys.stderr)
        print("   QRA 定制层（插件/记忆/审批）依赖它；请从项目根目录用 "
              "bin/qra / scripts/run_qra.sh 启动（两者都会设好 HERMES_HOME）。",
              file=sys.stderr)
        sys.exit(2)
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except ImportError:
        print(f"❌ 无法校验配置：环境缺少 pyyaml（{path}）", file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # yaml 解析失败属硬错：坏配置不许带病启动
        print(f"❌ QRA 配置无法解析：{path}\n   {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(2)
    if not isinstance(cfg, dict):
        print(f"❌ QRA 配置根必须是映射表：{path}", file=sys.stderr)
        sys.exit(2)

    problems = validate_config(cfg)
    if problems:
        print(f"❌ QRA 配置校验失败（{path}）：", file=sys.stderr)
        for p in problems:
            print(f"   - {p}", file=sys.stderr)
        print("   修复 config.yaml 后重试（dsh fail-loud：坏配置不带病运行）。",
              file=sys.stderr)
        sys.exit(2)
