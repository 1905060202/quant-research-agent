# CI/CD

> 权威源：CI 管道的唯一说明。改 `.github/workflows/ci.yml` 后同步改本文。

## 设计原则

1. **零 secret**：CI 不跑真实 API 层（门禁 3/4 层需要 key，本地跑）——CI 只跑
   离线层 1/2/6。这同时是脱敏红线的一部分：GitHub 仓库里不存在任何凭据。
2. **本地与 CI 同款命令**：CI 执行 `scripts/verify_qra.sh --offline` +
   `scripts/scan_credentials.sh` + `scripts/check_docs.py`，本地 push 前跑完全
   一样的命令——不存在「CI 过了本地不知道怎么过」的黑盒。
3. **vendor 按钉针克隆**：vendor/ 是 gitignored 的，CI 每次按
   `docs/vendor_sync_log.md` 记录钉针现克隆 hermes-agent（11c5aae），不从缓存
   信任任何漂移状态。

## 管道（.github/workflows/ci.yml）

```
push(main) / pull_request / workflow_dispatch
  ├─ setup-python 3.12 → python -m venv .venv-v7
  ├─ 克隆 vendor/hermes-agent → checkout 11c5aae（VERSION 文件同源）
  ├─ pip install -e vendor/hermes-agent（全依赖）+ jupyter_client ipykernel dill
  ├─ ① verify_qra.sh --offline   门禁层 1（py_compile）/ 2（单测）/ 6（内核 38 用例）
  ├─ ② scan_credentials.sh       零凭据扫描（tracked+untracked 全扫）
  └─ ③ check_docs.py             文档链接/路径核对
```

## 本地 vs CI 矩阵

| 检查 | 本地（必跑） | CI（自动） | 说明 |
|---|---|---|---|
| 层 1 py_compile | ✓ | ✓ | 秒级 |
| 层 2 单测（console 5 件套 + vendor_sync 16） | ✓ | ✓ | 秒级 |
| 层 6 内核 38 用例 | ✓ | ✓ | ~35s |
| 层 3 -z 真实 API ×2 | ✓ | ✗ | 需 key，计费 |
| 层 4 交互 pty 竞态 ×2 | ✓ | ✗ | 需 key + 真终端 |
| 层 5 命令 pty（离线） | ✓ | ✗（暂缓） | pty 在 runner 上不稳定收益低，可在 CI 里补 |
| 冒烟 _smoke_console / _smoke_qra_run | 相关改动时 | ✗ | 需 key |
| 零凭据扫描 | ✓ | ✓ | push 前最后一道 |
| 文档核对 | 改文档时 | ✓ | |

## 本地复现 CI（一分钟）

```bash
bash scripts/verify_qra.sh --offline
bash scripts/scan_credentials.sh
.venv-v7/bin/python scripts/check_docs.py
```

## 红灯处理

- **CI 红 = 禁止 push**（push 即发布，见 `docs/development.md` 提交协议）。
- 层 1/2/6 红：本地跑同层复现（离线层本机与 runner 无环境差异，除了 OS——
  若本地 macOS 绿、CI ubuntu 红，先查路径分隔符/可执行位/CRLF）。
- 凭据扫描红：按 `docs/development.md`「脱敏红线」处置顺序——先轮换 key
  止血，再处理历史，最后改代码。
- 文档核对红：改文档时顺手 `.venv-v7/bin/python scripts/check_docs.py` 自检，
  别把红灯留给下一次 push。

## CD 说明（为什么没有部署）

本仓库交付物 = 仓库本身（本地 CLI：`ln -s bin/qra ~/.local/bin/qra` 即安装），
无服务端部署目标。**push 即发布**——所以 push 前检查单 = 发布检查单，CI 是
发布前的自动保险，不是发布动作。
