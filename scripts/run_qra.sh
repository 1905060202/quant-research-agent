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

exec .venv-v7/bin/hermes "$@" \
    --model deepseek-v4-pro \
    --provider anthropic
