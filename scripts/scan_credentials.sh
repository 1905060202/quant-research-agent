#!/usr/bin/env bash
# 零凭据扫描（脱敏红线第 3 条，push 前必跑，CI 同款）
#
# 扫描范围：git tracked + untracked-but-not-ignored 的全部文件
#   （= 一切可能被提交进仓库的内容；vendor/ 与运行时目录已被 gitignore 排除）。
# 形态：sk-[A-Za-z0-9]{20,}（本仓库所用 key 的通用形态；其他凭据形态照猫画虎加）。
# 历史扫描（--history）：git log 全量 grep，本地执行（CI 用浅克隆跑不了）。
#
# 用法：
#   scripts/scan_credentials.sh            # 工作区扫描（默认）
#   scripts/scan_credentials.sh --history  # 工作区 + 全量历史（push 前推荐）
#
# 命中 = 退出码 1。误提交处置顺序见 docs/development.md 脱敏红线：
# 先轮换 key（止血）→ 再处理历史 → 最后改代码。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PATTERN='sk-[A-Za-z0-9]{20,}'
HITS=0

echo "== 零凭据扫描（工作区）=="
while IFS= read -r -d '' f; do
    if grep -lE "$PATTERN" "$f" >/dev/null 2>&1; then
        echo "❌ 命中：$f"
        HITS=1
    fi
done < <(git ls-files -co --exclude-standard -z)

if [[ "${1:-}" == "--history" ]]; then
    echo "== 零凭据扫描（全量 git 历史，本地专用）=="
    if git log --all -p -S "$PATTERN" --pickaxe-regex 2>/dev/null | grep -E "$PATTERN" | head -5; then
        # -S 只看出现次数变化的提交；为保万全再全量 grep 一次（慢但准）
        if git log --all -p | grep -E "$PATTERN" | head -5; then
            echo "❌ 历史命中（输出见上）——按脱敏红线处置：先轮换 key"
            HITS=1
        fi
    fi
fi

if [ "$HITS" -ne 0 ]; then
    echo "❌ 零凭据扫描失败"
    exit 1
fi
echo "✅ 零凭据扫描通过"
