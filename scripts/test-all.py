#!/usr/bin/env python3
"""依次运行各仓库的测试套件。

    python scripts/test-all.py                 # 全部
    python scripts/test-all.py --only dbwatch  # 只跑一个
    python scripts/test-all.py --fast          # 跳过 orchestrator（最慢）

每个仓库独立进程运行 —— 保证「单仓可测」这件事真的成立，而不是靠一个进程里的 sys.path 互相兜着。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent

SUITES = [
    ("dbt-core", "tests/test_dbcore.py"),
    ("dbt-core", "tests/test_mcp_kit.py"),
    ("dbt-store", "tests/test_dbstore.py"),
    ("dbt-know", "tests/test_dbknow.py"),
    ("mcp-sql-review", "tests/test_sql_review.py"),
    ("mcp-change-risk", "tests/test_change_risk.py"),
    ("mcp-dbwatch", "tests/test_dbwatch.py"),
    ("mcp-migration-diff", "tests/test_migration_diff.py"),
    ("mcp-data-semantics", "tests/test_data_semantics.py"),
    ("mcp-profile-hub", "tests/test_profile_hub.py"),
    ("dbt-orchestrator", "tests/test_dbt_orchestrator.py"),
    ("(integration)", "tests/test_integration.py"),
]


def run_one(repo: str, rel: str) -> tuple[bool, str, float]:
    path = ROOT / repo / rel if repo != "(integration)" else ROOT / rel
    if not path.exists():
        return False, f"找不到 {path}", 0.0
    t0 = time.time()
    proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                          cwd=str(path.parent.parent), encoding="utf-8", errors="replace")
    took = time.time() - t0
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = [ln for ln in out.strip().splitlines() if ln.strip()]
    summary = ""
    for ln in reversed(tail):
        if "通过" in ln or "失败" in ln or "Error" in ln or "Traceback" in ln:
            summary = ln.strip()
            break
    return proc.returncode == 0, summary or (tail[-1] if tail else ""), took


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑名字包含该子串的套件")
    ap.add_argument("--fast", action="store_true", help="跳过 orchestrator")
    args = ap.parse_args()

    suites = SUITES
    if args.only:
        suites = [s for s in suites if args.only in s[0]]
    if args.fast:
        suites = [s for s in suites if s[0] != "dbt-orchestrator"]
    if not suites:
        print("没有匹配的套件", file=sys.stderr)
        return 2

    print(f"运行 {len(suites)} 个测试套件（Python {sys.version.split()[0]}）\n")
    bad: list[str] = []
    total_took = 0.0
    for repo, rel in suites:
        ok, summary, took = run_one(repo, rel)
        total_took += took
        mark = "✓" if ok else "✗"
        print(f"  {mark} {repo:22s} {took:5.1f}s  {summary}")
        if not ok:
            bad.append(repo)

    print(f"\n{'=' * 62}")
    if bad:
        print(f"失败 {len(bad)} 个：{', '.join(bad)}")
        print("单独复现：python " + " ".join(f"{b}/tests/test_*.py" if b != "(integration)" else "tests/test_integration.py" for b in bad))
        return 1
    print(f"全部通过（{len(suites)} 套件，{total_took:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
