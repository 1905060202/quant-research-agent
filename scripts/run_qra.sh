#!/usr/bin/env bash
# QRA 启动器：Hermes 底座 + QRA 定制层
#
# 用法（在项目根目录）：
#   ./scripts/run_qra.sh -z "你的问题"
#   ./scripts/run_qra.sh                     # 交互模式
#
# 凭据来源（优先级从高到低，零凭据落盘）：
#   1. 环境变量 ANTHROPIC_TOKEN / ANTHROPIC_BASE_URL（已设置则直接使用）
#   2. ~/.claude/settings.json 的 ANTHROPIC_AUTH_TOKEN（与 Claude Code 共用，待轮换）
set -euo pipefail

cd "$(dirname "$0")/.."

export HERMES_HOME="$PWD/.hermes"
export HERMES_ENABLE_PROJECT_PLUGINS=1

if [[ -z "${ANTHROPIC_TOKEN:-}" ]]; then
    export ANTHROPIC_TOKEN="$(jq -r '.env.ANTHROPIC_AUTH_TOKEN // empty' "$HOME/.claude/settings.json" 2>/dev/null)"
fi
if [[ -z "${ANTHROPIC_TOKEN:-}" ]]; then
    echo "错误：找不到 ANTHROPIC_TOKEN。请 export ANTHROPIC_TOKEN=... 后重试。" >&2
    exit 1
fi
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.deepseek.com/anthropic}"

# QRA 工具集并集：hermes cli 平台默认集只有 19 个内置集，不含插件注册的
# "qra" 集（2026-08-16 qra.run 冒烟实测：插件加载成功但工具不在会话工具表，
# console 与 -z 同根因）。动态解析默认集再并上 qra——不硬编码内置集名，
# 上游漂移自动跟随。默认 --toolsets 在前、用户 "$@" 在后：argparse 后者
# 覆盖前者，显式指定工具集的用户意图优先。
DEFAULT_TOOLSETS="$(
  .venv-v7/bin/python - <<'PYEOF'
import sys
sys.path.insert(0, "vendor/hermes-agent")
from hermes_cli.tools_config import _get_platform_tools
print(",".join(sorted(_get_platform_tools({}, "cli") | {"qra"})))
PYEOF
)"

exec .venv-v7/bin/hermes "$@" \
    --toolsets "$DEFAULT_TOOLSETS" \
    --model deepseek-v4-pro \
    --provider anthropic
