#!/usr/bin/env bash
# QRA Console：prime 式 CoT 全展示终端（ADR D007 · Phase 1）
#
# 用法：
#   qra console -z "你的问题"     # 单发
#   qra console                   # 多轮交互（Ctrl+T 折叠思考，空输入退出）
#
# 架构：导入 run_agent.AIAgent 自建显示层，vendor 零改动（D002）。
# 凭据来源与 run_qra.sh 相同（环境变量 → ~/.claude/settings.json）。
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

exec .venv-v7/bin/python -m src.qra.console.main "$@"
