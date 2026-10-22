#!/usr/bin/env python3
"""边界检查 —— 保证「一个 MCP 不依赖另一个 MCP」这条硬约束真的成立。

检查三件事：

1. **跨 MCP import**：MCP 仓库的源码里不许出现 `from <另一个 MCP 包> import`
2. **越界依赖声明**：pyproject 的 dependencies 里不许出现其他 `mcp-*`
3. **共享库反向依赖**：dbt-core / dbt-store / dbt-know 不许 import 任何 MCP

    python scripts/check-boundaries.py [--verbose]

退出码 0 = 边界干净。CI 里跑这个。
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

MCP_REPOS = {
    "mcp-sql-review": "sql_review",
    "mcp-change-risk": "change_risk",
    "mcp-dbwatch": "dbwatch",
    "mcp-migration-diff": "migration_diff",
    "mcp-data-semantics": "data_semantics",
    "mcp-profile-hub": "profile_hub",
}
LIB_REPOS = {
    "dbt-core": "dbcore",
    "dbt-store": "dbstore",
    "dbt-know": "dbknow",
}
ALL_PKG = {**MCP_REPOS, **LIB_REPOS}


def iter_py(repo: str):
    base = ROOT / repo / "src"
    if not base.is_dir():
        return
    for f in base.rglob("*.py"):
        yield f


def imports_of(path: pathlib.Path) -> set[str]:
    """返回该文件导入的顶层包名。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            if n.level:                      # 相对 import，属本包
                continue
            if n.module:
                names.add(n.module.split(".")[0])
    return names


def main() -> int:
    verbose = "--verbose" in sys.argv
    problems: list[str] = []

    # 1 + 3：源码级 import
    for repo, pkg in ALL_PKG.items():
        for f in iter_py(repo):
            imported = imports_of(f)
            for other_repo, other_pkg in ALL_PKG.items():
                if other_pkg == pkg:
                    continue
                if other_pkg in imported:
                    # 合法：MCP -> 共享库、共享库 -> 共享库
                    # 禁止：MCP -> MCP、共享库 -> MCP
                    is_violation = (
                        (repo in MCP_REPOS and other_repo in MCP_REPOS)
                        or (repo in LIB_REPOS and other_repo in MCP_REPOS)
                    )
                    if is_violation:
                        problems.append(
                            f"[跨仓 import] {f.relative_to(ROOT)} 导入了 {other_pkg}"
                            f"（{repo} → {other_repo}）"
                        )
            if verbose:
                print(f"  {f.relative_to(ROOT)}: {sorted(imported)}")

    # 2：pyproject 依赖声明
    for repo in MCP_REPOS:
        pp = ROOT / repo / "pyproject.toml"
        if not pp.exists():
            continue
        text = pp.read_text(encoding="utf-8")
        for other in MCP_REPOS:
            if other == repo:
                continue
            if f'"{other}' in text:
                problems.append(f"[越界依赖] {repo}/pyproject.toml 声明了 {other}")

    # 4：orchestrator 必须依赖全部 MCP（它是唯一允许的例外，且必须显式声明）
    orch = ROOT / "dbt-orchestrator/pyproject.toml"
    if orch.exists():
        text = orch.read_text(encoding="utf-8")
        missing = [m for m in MCP_REPOS if f'"{m}' not in text]
        if missing:
            problems.append(f"[编排器缺依赖] dbt-orchestrator 未声明：{missing}")

    print(f"检查了 {len(ALL_PKG)} 个仓的源码与依赖声明")
    if problems:
        print(f"\n发现 {len(problems)} 处边界违规：")
        for p in problems:
            print("  ✗", p)
        return 1
    print("边界干净：MCP 之间零代码依赖，共享库不反向依赖 MCP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
