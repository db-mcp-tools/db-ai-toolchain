#!/usr/bin/env python3
"""把 10 个仓库以 editable 方式装进当前 Python 环境。

    python scripts/dev-install.py              # 只装共享库 + 你指定的 MCP
    python scripts/dev-install.py --all        # 全装
    python scripts/dev-install.py --only dbwatch,sql-review

为什么不用 `pip install -e . ` 逐个手敲：共享库之间有先后依赖，一次装好省事。
注意：正式使用请按各仓库 README 单独 `pip install`，那不涉及同仓路径。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

LIBS = ["sqltk-core", "sqltk-store", "sqltk-know"]
MCPS = {
    "sql-review": "mcp-sql-review",
    "change-risk": "mcp-change-risk",
    "dbwatch": "mcp-dbwatch",
    "migration-diff": "mcp-migration-diff",
    "data-semantics": "mcp-data-semantics",
    "profile-hub": "mcp-profile-hub",
}


def install(repo: str) -> bool:
    path = ROOT / repo
    if not (path / "pyproject.toml").exists():
        print(f"  ✗ {repo}: 缺少 pyproject.toml")
        return False
    cmd = [sys.executable, "-m", "pip", "install", "-e", str(path), "--no-deps", "-q"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    ok = proc.returncode == 0
    detail = ""
    if not ok:
        lines = proc.stderr.strip().splitlines()
        detail = f"\n      {lines[-1] if lines else '（无错误输出）'}"
    print(f"  {'✓' if ok else '✗'} {repo}{detail}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="装全部 6 个 MCP + orchestrator")
    ap.add_argument("--only", default="", help="逗号分隔的 MCP 短名，如 dbwatch,sql-review")
    ap.add_argument("--with-deps", action="store_true", help="同时装第三方依赖（sqlglot/mcp/驱动）")
    args = ap.parse_args()

    if args.all:
        picked = list(MCPS.values()) + ["sqltk-orchestrator"]
    elif args.only:
        picked = []
        for name in [x.strip() for x in args.only.split(",") if x.strip()]:
            if name not in MCPS:
                print(f"未知 MCP：{name}（可选：{', '.join(MCPS)}）", file=sys.stderr)
                return 2
            picked.append(MCPS[name])
    else:
        picked = ["mcp-sql-review"]
        print("未指定 --all / --only，默认只装 mcp-sql-review\n")

    if args.with_deps:
        print("安装第三方依赖 sqlglot / mcp …")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "sqlglot>=25", "mcp>=1.0"], check=False)
        print()

    print("以 editable 方式安装（--no-deps，共享库按顺序装）：")
    for repo in LIBS + picked:
        if not install(repo):
            return 1

    print("\n完成。常用命令：")
    print("  mcp-sql-review tools | mcp-dbwatch tools | sqltk run --seed ...")
    print("  自测：python scripts/test-all.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
