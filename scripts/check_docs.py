#!/usr/bin/env python3
"""文档核对（CI 同款，改文档后本地自检）：链接存在性 + 行内路径存在性。

三层检查（纯 stdlib，零网络）：
  1. markdown 链接 `](target)`：相对/仓根两种解析，任一存在即过；#anchor 宽松
     核对（slugify 后匹配标题，失配仅警告——锚点也可能由 GH 自动生成）。
  2. 行内路径提及：代码块/正文里形如 scripts/xxx.py、src/qra/xxx.py、
     .hermes/plugins/xxx、docs/xxx.md、bench/xxx、.github/xxx 的 token
     （含扩展名、不含 * / < 通配），必须真实存在——防「文档写了但文件没建」。
  3. 关键入口抽查：bin/qra、scripts/verify_qra.sh 等（第 2 层已覆盖，此处兜底）。

已知例外（规划中的文件，改文档时若落地请删除对应条目）：
  src/qra/json_scan.py   —— D008 落地项，规划中（HANDOFF 待办提及）
  bench/arc3_baseline.json —— D010 Phase 0 摸底产物，尚未跑

不检查：docs/archive_废弃方案/（废案文档，勿参照执行，其路径描述的是被推翻的架构）;
引用了上游仓内部文件的路径（UPSTREAM_REF，仅警告）。

用法：.venv-v7/bin/python scripts/check_docs.py [--verbose]
退出码：0=通过；1=有错误（链接/路径不存在）。
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MD_GLOBS = [
    "README.md",
    "docs/**/*.md",
    "bench/**/*.md",
    ".github/**/*.md",
]

# 规划中文件：路径 -> 出处说明
PLANNED = {
    "src/qra/json_scan.py": "D008 落地项，规划中（HANDOFF 待办）",
    "bench/arc3_baseline.json": "D010 Phase 0 摸底产物，尚未跑",
}

# 上游仓内部文件引用（研究笔记里引用 prime/arc-code/dsh 自身文档）：仅警告
UPSTREAM_REF = {
    "docs/failure-modes.md": "上游 arc-code 仓库文档（机理研究笔记引用）",
}

LINK_RE = re.compile(r"\]\(([^)]+)\)")
INLINE_PATH_RE = re.compile(
    r"(?<![\w/])(scripts|src|\.hermes|docs|bench|\.github)/"
    r"[0-9A-Za-z._/-]+\.(py|sh|md|yml|yaml|json|toml|html)"
)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().strip()
    text = re.sub(r"[^\w\s一-鿿-]", "", text)  # 保留中日韩字符
    return re.sub(r"\s+", "-", text)


def iter_md_files() -> list[str]:
    import glob

    files = []
    for pat in MD_GLOBS:
        files.extend(glob.glob(os.path.join(ROOT, pat), recursive=True))
    # 废案目录不检查（勿参照执行，路径描述的是被推翻的架构）
    return sorted(
        f for f in set(files)
        if "archive_废弃方案" not in f and "archive_legacy" not in f
    )


def check_link(f: str, target: str) -> tuple[str, str | None]:
    """返回 (status, detail)。status: ok / missing / warn-anchor。"""
    if target.startswith(("http://", "https://", "mailto:")):
        return "ok", "external"
    path_part = target.split("#", 1)[0].split("?", 1)[0]
    if not path_part:
        return "warn-anchor", f"{f}: 同文件锚点 #{target.split('#', 1)[1] if '#' in target else ''}"
    candidates = [
        os.path.normpath(os.path.join(os.path.dirname(f), path_part)),   # 相对文件
        os.path.normpath(os.path.join(ROOT, path_part.lstrip("/"))),     # 相对仓根
    ]
    hit = next((c for c in candidates if os.path.exists(c)), None)
    if hit is None:
        return "missing", f"{f}: 链接目标不存在 {target}"
    if "#" in target and hit.endswith(".md"):
        anchor = target.split("#", 1)[1]
        with open(hit, encoding="utf-8") as fh:
            headings = [
                slugify(m.group(1))
                for m in re.finditer(r"^#{1,6}\s+(.+)$", fh.read(), re.M)
            ]
        if anchor and slugify(anchor) not in headings:
            return "warn-anchor", f"{f}: 锚点未命中 #{anchor}（{os.path.relpath(hit, ROOT)}）"
    return "ok", None


def main() -> int:
    verbose = "--verbose" in sys.argv
    errors: list[str] = []
    warns: list[str] = []
    links_checked = 0
    paths_checked = 0

    for f in iter_md_files():
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        rel = os.path.relpath(f, ROOT)

        for m in LINK_RE.finditer(text):
            target = m.group(1).strip()
            status, detail = check_link(f, target)
            links_checked += 1
            if status == "missing":
                errors.append(detail)
            elif status == "warn-anchor":
                warns.append(detail)
            elif verbose:
                print(f"  link ok: {rel} → {target}")

        for m in INLINE_PATH_RE.finditer(text):
            p = m.group(0)
            if "*" in p or "<" in p:
                continue
            paths_checked += 1
            if not os.path.exists(os.path.join(ROOT, p)):
                if p in PLANNED:
                    warns.append(f"{rel}: 行内路径 {p}（{PLANNED[p]}）")
                elif p in UPSTREAM_REF:
                    warns.append(f"{rel}: 行内路径 {p}（{UPSTREAM_REF[p]}）")
                else:
                    errors.append(f"{rel}: 行内路径不存在 {p}")
            elif verbose:
                print(f"  path ok: {rel} → {p}")

    print(f"链接检查 {links_checked} 条，路径提及 {paths_checked} 条")
    for w in warns:
        print(f"⚠ {w}")
    if errors:
        print(f"❌ 文档核对失败：{len(errors)} 处")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("✅ 文档核对通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
