#!/usr/bin/env bash
# QRA 回归门禁（vendor 同步后必跑；也可日常独立跑）
#
# 层次：
#   1. py_compile console
#   2. 单测（折叠状态机 / InputLayer 行编辑 / 命令注册表 / 历史 / 粘贴保护）
#   3. -z 真实 API 工具题 ×2（答案与新浪同源价格动态对照）
#   4. console 交互 pty 竞态用例 ×2（回合中空行 → 缓冲 → 回合后退出）
#   5. 命令 pty（全离线）：/help !echo /model /sessions /yolo 逐条断言标记
#   6. 原始字节 pty（D011）：斜杠菜单/Esc/光标编辑//fold //agents //mouse/
#      括号粘贴/Ctrl+C 恢复/空行退出（输入层回显协议，只发字节不发提问）
#   7. qra_python 持久内核（D007 P2）：py_compile 插件 + 四级验证单测
#      （执行/跨轮变量/dill 恢复/bench 题 + 机理：中断·自愈·LRU·debounce）
#
# 用法：scripts/verify_qra.sh          # 全部门禁（含真实 API，约 4-7 分钟）
#       scripts/verify_qra.sh --offline # 离线层 1/2/7（CI 同款，零凭据零网络）
# 返回非零 = 门禁失败

set -uo pipefail

# --offline：只跑不依赖凭据与网络的层（1/2/7），与 .github/workflows/ci.yml 同款。
if [[ "${1:-}" == "--offline" ]]; then
    OFFLINE=1
    shift
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv-v7/bin/python"
FAIL=0

echo "== 1/6 py_compile =="
"$PY" -m py_compile "$ROOT/src/qra/console/main.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/commands.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/handlers.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/input_layer.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/renderer.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/termio.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/linebuffer.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/session_state.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/models_router.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/approvals.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/console/exporter.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/config_guard.py" || FAIL=1
"$PY" -m py_compile "$ROOT/src/qra/vendor_sync.py" || FAIL=1

echo "== 2/6 单测 =="
# 失败详情必须落盘：tail -3 会丢 flaky 证据，无法复现
for _suite in "console/tests" "tests"; do
    _out="$(mktemp /tmp/qra_gate_XXXX.log)"
    if (cd "$ROOT" && "$PY" -m unittest discover -s "src/qra/$_suite" > "$_out" 2>&1); then
        tail -3 "$_out"
    else
        FAIL=1
        tail -3 "$_out"
        echo "  失败详情（$_suite）:"
        grep -A 12 -E "^(FAIL|ERROR):" "$_out" | head -60
    fi
    rm -f "$_out"
done

echo "== 3/6 -z 工具题（真实 API）=="
if [ "${OFFLINE:-0}" -ne 1 ]; then
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
else
    echo "  （--offline 跳过：需真实 API 凭据）"
fi

echo "== 4/6 console 交互 pty 竞态 =="
if [ "${OFFLINE:-0}" -ne 1 ]; then
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
else
    echo "  （--offline 跳过：需真实 API 凭据）"
fi

echo "== 5/6 命令 pty（全离线）=="
if [ "${OFFLINE:-0}" -ne 1 ]; then
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
else
    echo "  （--offline 跳过：console 启动依赖本地环境）"
fi

echo "== 6/7 原始字节 pty（D011 输入层）=="
if [ "${OFFLINE:-0}" -ne 1 ]; then
"$PY" - "$ROOT" <<'PYEOF' || FAIL=1
import sys
sys.path.insert(0, sys.argv[1] + "/scripts")
from _e2e_helpers import run_console_raw
ok = run_console_raw(sys.argv[1])
print(f"  原始字节 pty: {'✓' if ok else '✗'}")
sys.exit(0 if ok else 1)
PYEOF
else
    echo "  （--offline 跳过：console 启动依赖本地环境）"
fi

echo "== 7/7 qra_python 持久内核（D007 P2）=="
"$PY" -m py_compile "$ROOT/.hermes/plugins/qra_python/__init__.py" || FAIL=1
(cd "$ROOT" && "$PY" -m unittest discover -s .hermes/plugins/qra_python/tests 2>&1 | tail -3) || FAIL=1

if [ "$FAIL" -ne 0 ]; then
    echo "❌ 门禁失败"
    exit 1
fi
echo "✅ 门禁全过"
