#!/usr/bin/env bash
# QRA 回归门禁（vendor 同步后必跑；也可日常独立跑）
#
# 层次：
#   1. py_compile console
#   2. 单测（折叠状态机 / InputLayer 行编辑 / 命令注册表 / 历史 / 粘贴保护）
#   3. -z 真实 API 工具题 ×2（答案与新浪同源价格动态对照）
#   4. console 交互 pty 竞态用例 ×2（回合中空行 → 缓冲 → 回合后退出）
#   5. 命令 pty（全离线）：/help !echo /model /sessions /yolo 逐条断言标记
#   6. qra_python 持久内核（D007 P2）：py_compile 插件 + 四级验证单测
#      （执行/跨轮变量/dill 恢复/bench 题 + 机理：中断·自愈·LRU·debounce）
#
# 用法：scripts/verify_qra.sh          # 全部门禁（含真实 API，约 4-7 分钟）
# 返回非零 = 门禁失败

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv-v7/bin/python"
FAIL=0

echo "== 1/6 py_compile =="
"$PY" -m py_compile "$ROOT/src/qra/console/main.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/commands.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/handlers.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/input_layer.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/session_state.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/models_router.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/approvals.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/exporter.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/config_guard.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/vendor_sync.py" || FAIL=1

echo "== 2/6 单测 =="
(cd "$ROOT" && "$PY" -m unittest discover -s src/qra/console/tests 2>&1 | tail -3) || FAIL=1
(cd "$ROOT" && "$PY" -m unittest discover -s src/qra/tests 2>&1 | tail -3) || FAIL=1

echo "== 3/6 -z 工具题（真实 API）=="
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

echo "== 4/6 console 交互 pty 竞态 =="
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

echo "== 5/6 命令 pty（全离线）=="
"$PY" - "$ROOT" <<'PYEOF' || FAIL=1
import sys
sys.path.insert(0, sys.argv[1] + "/scripts")
from _e2e_helpers import run_console_cmd
cases = [
    ("/help", "QRA console 命令"),
    ("! echo QRA_BANG_OK", "QRA_BANG_OK"),
    ("/model", "CC proxy"),
    ("/sessions", ("恢复会话", "没有可列出的会话")),
    ("/yolo", "YOLO 已关闭"),
    ("/loop", "用法：/loop"),
]
ok = run_console_cmd(sys.argv[1], cases)
print(f"  命令 pty: {'✓' if ok else '✗'}")
sys.exit(0 if ok else 1)
PYEOF

echo "== 6/6 qra_python 持久内核（D007 P2）=="
"$PY" -m py_compile "$ROOT/.hermes/plugins/qra_python/__init__.py" || FAIL=1
(cd "$ROOT" && "$PY" -m unittest discover -s .hermes/plugins/qra_python/tests 2>&1 | tail -3) || FAIL=1

if [ "$FAIL" -ne 0 ]; then
    echo "❌ 门禁失败"
    exit 1
fi
echo "✅ 门禁全过"
