#!/usr/bin/env bash
# 兼容薄壳：同步逻辑在 src/qra/vendor_sync.py，主入口是 QRA 原生命令 `qra sync`。
# 保留此脚本只为兼容 D009 文档与既有引用；旧参数映射：--apply→apply --full→full。
QRA="$(cd "$(dirname "$0")/.." && pwd)/bin/qra"
case "${1:-}" in
    --apply) shift; exec "$QRA" sync apply "$@" ;;
    --full)  shift; exec "$QRA" sync full "$@" ;;
    *)       exec "$QRA" sync "$@" ;;
esac
