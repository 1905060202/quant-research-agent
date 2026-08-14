#!/usr/bin/env bash
# QRA 日报一键生产：调用 qra_daily skill 走完整流水线（信号→行情→方法论→记忆→撰写→验证）
#
# 用法（在项目根目录）：
#   ./scripts/run_qra_daily.sh            # 生成今天的日报
#   ./scripts/run_qra_daily.sh --resume   # 日报写到一半时续跑（保留上下文）
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "${1:-}" == "--resume" ]]; then
    exec ./scripts/run_qra.sh --resume qra_daily
fi

exec ./scripts/run_qra.sh -z "请调用 qra_daily 技能，完整执行量化研究日报流水线（信号→行情→方法论→记忆→撰写→验证卡→qra_verify 验证），生成今天的日报。"
