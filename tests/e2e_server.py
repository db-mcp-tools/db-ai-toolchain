"""接线验证：确认 MCP 服务层把 dbcore 工具正确注册进来。

与 tests/e2e_mysql.py（连库功能）互补：这里验证「服务接线」——
FastMCP 实例成功加载、12 个 @mcp.tool 全部注册、且一个纯逻辑
工具能直接被调用返回正确结论（无需连库）。

运行（需先 pip install mcp）：
    PYTHONPATH=. packages python tests/e2e_server.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # 让 servers 包可导入
sys.path.insert(0, str(ROOT / "packages"))  # 让 dbcore 可导入

try:
    from servers.sql_review import server
except ImportError as e:  # noqa: BLE001
    print(f"SKIP: 无法导入 MCP 服务（是否 pip install mcp？）：{e}")
    sys.exit(0)

EXPECTED = {
    "review_sql_tool", "assess_change_tool", "generate_rollback_tool",
    "precheck_change_window_tool", "explain_sql_tool", "instance_history_tool",
    "suggest_fixes_tool", "verify_fix_tool", "plan_compare_tool",
    "equivalence_check_tool", "advise_indexes_tool", "batch_review_tool",
}

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + str(detail)) if (detail and not cond) else ''}")
    if not cond:
        FAILED.append(name)


def _list_tool_names() -> list[str]:
    mcp = server.mcp
    try:
        tools = mcp._tool_manager.list_tools()
    except Exception:  # noqa: BLE001
        res = mcp.list_tools()
        tools = asyncio.get_event_loop().run_until_complete(res) if asyncio.iscoroutine(res) else res
    return sorted(getattr(t, "name", None) or t for t in tools)


def main() -> None:
    names = _list_tool_names()

    print("== 1. FastMCP 实例与工具注册 ==")
    check("FastMCP 实例已创建", server.mcp is not None)
    check("注册工具数 == 12", len(names) == 12, f"实际 {len(names)}: {names}")
    missing = EXPECTED - set(names)
    check("无缺失工具", not missing, f"缺失 {missing}")
    extra = set(names) - EXPECTED
    check("无多余工具", not extra, f"多余 {extra}")

    print("== 2. 纯逻辑工具可直接调用（接线可用，无需连库）==")
    r = server.review_sql_tool("UPDATE user SET name='x'")
    check("review_sql_tool 返回 verdict", r.get("verdict") == "禁止上线", r.get("verdict"))
    check("review_sql_tool 识别 blocker", r.get("max_severity") == "blocker", r.get("max_severity"))

    print("== 3. 批量体检工具 ==")
    # 第二条无 WHERE 的 UPDATE 必触发 blocker，CI 门禁应 FAIL
    b = server.batch_review_tool(["SELECT id FROM t", "UPDATE user SET name='x'"])
    check("batch_review ci_verdict=FAIL", b.get("ci_verdict") == "FAIL", b.get("ci_verdict"))

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        sys.exit(1)
    print("ALL SERVER WIRING PASSED")


if __name__ == "__main__":
    main()
