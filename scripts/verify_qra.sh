#!/usr/bin/env bash
# QRA 回归门禁（vendor 同步后必跑；也可日常独立跑）
#
# 层次：
#   1. py_compile console
#   2. 单测 9 项（折叠状态机 / InputLayer 行编辑）
#   3. -z 真实 API 工具题 ×2（答案与新浪同源价格动态对照）
#   4. console 交互 pty 竞态用例 ×2（回合中空行 → 缓冲 → 回合后退出）
#
# 用法：scripts/verify_qra.sh          # 全部门禁（含真实 API，约 3-6 分钟）
# 返回非零 = 门禁失败

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv-v7/bin/python"
FAIL=0

echo "== 1/4 py_compile =="
"$PY" -m py_compile "$ROOT/src/qra/console/main.py" || FAIL=1

echo "== 2/4 单测 9 项 =="
(cd "$ROOT" && "$PY" -m unittest discover -s src/qra/console/tests 2>&1 | tail -3) || FAIL=1

echo "== 3/4 -z 工具题（真实 API）=="
for i in 1 2; do
    "$PY" - "$ROOT" "$i" <<'PYEOF' || FAIL=1
import sys
sys.path.insert(0, sys.argv[1] + "/scripts")
from _e2e_helpers import run_z
ok = run_z(sys.argv[1])
print(f"  -z#{sys.argv[2]}: {'✓' if ok else '✗'}")
sys.exit(0 if ok else 1)
PYEOF
done

echo "== 4/4 console 交互 pty 竞态 =="
for i in 1 2; do
    "$PY" - "$ROOT" "$i" <<'PYEOF' || FAIL=1
import sys
sys.path.insert(0, sys.argv[1] + "/scripts")
from _e2e_helpers import run_interactive
ok = run_interactive(sys.argv[1], f"交互#{sys.argv[2]}")
print(f"  交互#{sys.argv[2]}: {'✓' if ok else '✗'}")
sys.exit(0 if ok else 1)
PYEOF
done

if [ "$FAIL" -ne 0 ]; then
    echo "❌ 门禁失败"
    exit 1
fi
echo "✅ 门禁全过"
