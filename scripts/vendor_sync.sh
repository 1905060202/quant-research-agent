#!/usr/bin/env bash
# QRA vendor 同步脚本（D009 流程的机械部分）
#
# 用法：
#   scripts/vendor_sync.sh            # fetch + 嫁接面核对 + 报告（不落地）
#   scripts/vendor_sync.sh --apply    # 核对通过后 ff-only 快进 + 更新 VERSION
#   scripts/vendor_sync.sh --full     # --apply + 跑完整回归门禁（含真实 API E2E）
#
# 设计（docs/decisions/D009_vendor同步流程.md）：
#   - 同步点取 upstream/main，快进不 merge（钉针永远是 main 的直系祖先，已验证）
#   - 嫁接面清单 = QRA 触碰过的 hermes 内部文件；命中任何一项 → 拒绝自动落地，
#     必须人工看 diff 并适配（"新嫁接必须入清单"铁律：src/qra 或 .hermes/plugins
#     新增 hermes 内部依赖时，同步更新本文件 GRAFT_PATHS）
#   - 回滚 = git checkout <旧钉针> + 回写 VERSION（vendor 自带真实上游历史）

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/vendor/hermes-agent"
VERSION_FILE="$VENDOR/VERSION"

# 代理优先（大陆网络直连 GitHub 时断时续）；7890 不在则直连
PROXY_OPT=()
CURL_PROXY=()
if nc -z -G 1 127.0.0.1 7890 2>/dev/null; then
    PROXY_OPT=(-c http.proxy=http://127.0.0.1:7890)
    CURL_PROXY=(-x http://127.0.0.1:7890)
fi

# ---------------------------------------------------------------- 嫁接面清单
# 命中即高危：这些文件是 QRA 的外部依赖面，上游改了必须人工确认
GRAFT_PATHS=(
    run_agent.py                    # qra_console: AIAgent 构造+回调
    hermes_state.py                 # qra_console: SessionDB
    hermes_cli/config.py            # console 模型/provider 解析
    hermes_cli/models.py            # detect_provider_for_model / 前缀剥离
    hermes_cli/model_normalize.py   # normalize_model_for_provider
    hermes_cli/model_switch.py
    hermes_cli/fallback_config.py
    hermes_cli/mcp_startup.py
    hermes_cli/runtime_provider.py
    hermes_cli/tools_config.py
    hermes_cli/oneshot.py           # _normalize_toolsets / 最小参数集先例
    hermes_cli/main.py              # -z 启动守卫等 CLI 路径
    cli.py                          # CLI 入口（run_qra.sh 走这里）
    hermes_cli/auth.py
    tools/approval.py               # 审批面板（交互模式工具确认）
    agent/background_review.py      # qra_refine: _XX_REVIEW_PROMPT 常量
    agent/plugin_llm.py             # qra_* 插件注册面
    agent/plugin_stream_hooks.py
    hermes_cli/plugins.py
    hermes_cli/plugin_capabilities.py
    hermes_cli/plugin_index.py
)

usage() { sed -n '2,12p' "$0"; exit 1; }
[ $# -le 1 ] || usage
MODE="${1:-}"

cd "$VENDOR"
OLD_PIN="$(cat "$VERSION_FILE" 2>/dev/null || echo unknown)"

echo "== fetch upstream =="
git "${PROXY_OPT[@]}" fetch upstream main 2>&1 | tail -2 || {
    echo "fetch 失败（网络？）。可重试或手动：git -c http.proxy=... fetch upstream main"
    exit 1
}
NEW_PIN="$(git rev-parse upstream/main)"
echo "旧钉针: $OLD_PIN"
echo "新钉针: $NEW_PIN"

if [ "$OLD_PIN" = "$NEW_PIN" ]; then
    echo "已是最新，无需同步。"
    exit 0
fi

echo "== 变更清单（${OLD_PIN}..${NEW_PIN}）=="
if git cat-file -e "$OLD_PIN^{commit}" 2>/dev/null; then
    git log --oneline "$OLD_PIN..$NEW_PIN" | head -30
    CHANGED="$(git diff --name-only "$OLD_PIN" "$NEW_PIN")"
else
    echo "(浅取历史里已无旧钉针，跳过本地 diff——嫁接面核对改用 GitHub compare API)"
    CHANGED=""
fi

echo "== 嫁接面核对 =="
HITS=()
for p in "${GRAFT_PATHS[@]}"; do
    if printf '%s\n' "${CHANGED:-}" | grep -qx "$p"; then
        HITS+=("$p")
    fi
done
# 浅取兜底：本地拿不到 diff 时查 API
if [ -z "${CHANGED:-}" ] && command -v curl >/dev/null; then
    API="https://api.github.com/repos/NousResearch/hermes-agent/compare/$OLD_PIN...main"
    APIFILES="$(curl -s "${CURL_PROXY[@]}" "$API" | \
        python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    print("\n".join(f["filename"] for f in d.get("files",[])))
except Exception:
    pass' 2>/dev/null || true)"
    for p in "${GRAFT_PATHS[@]}"; do
        if printf '%s\n' "${APIFILES:-}" | grep -qx "$p"; then
            HITS+=("$p")
        fi
    done
fi

if [ ${#HITS[@]} -gt 0 ]; then
    echo "⚠️  上游改动了 QRA 嫁接面文件："
    printf '    %s\n' "${HITS[@]}"
    echo "    自动同步已拒绝。请人工 git diff 每个文件、适配 QRA 侧代码，"
    echo "    跑通回归门禁后再用 --apply 落地。"
    exit 2
fi
echo "嫁接面零命中 ✓"

if [ "$MODE" = "--apply" ] || [ "$MODE" = "--full" ]; then
    echo "== ff-only 快进 =="
    git merge --ff-only upstream/main
    echo "$NEW_PIN" > "$VERSION_FILE"
    echo "VERSION 已更新 → $NEW_PIN"
fi

if [ "$MODE" = "--full" ]; then
    echo "== 回归门禁 =="
    "$ROOT/scripts/verify_qra.sh" || { echo "门禁失败！回滚：cd vendor/hermes-agent && git checkout $OLD_PIN && echo $OLD_PIN > VERSION"; exit 3; }
    echo "== 门禁全过 ✓ 记录到 docs/vendor_sync_log.md =="
fi

echo "完成。模式: ${MODE:-report}"
