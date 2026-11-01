"""接线验证：确认 MCP 服务层把工具正确注册进来。

与 tests/e2e_mysql.py（连库功能）互补：这里验证「服务接线」——
make_server 实例成功创建、server.TOOLS 中全部工具完成注册、且纯逻辑
工具能直接被调用返回正确结论（无需连库）。

运行（需先 pip install -e 全部兄弟仓，mcp-sql-review 依赖 sqltk-store 等）：
    python tests/e2e_server.py

CI 内由 .github/workflows/e2e.yml 负责安装与运行。
"""

from __future__ import annotations

import asyncio
import os
import sys

try:
    from sql_review import server
except ImportError as e:  # noqa: BLE001
    # CI 必须能导入（说明兄弟仓没装齐），应直接失败暴露问题；
    # 本地开发若未装齐兄弟仓，则跳过，不误导。
    if os.environ.get("CI"):
        raise
    print(f"SKIP: 无法导入 sql_review.server（需先 pip install -e 全部兄弟仓）：{e}")
    sys.exit(0)

# canonical 注册的核心工具（来自 server.TOOLS）。
# 不强制总数，避免仓库新增工具时误伤 CI。
CORE_TOOLS = {
    "review_sql", "review_batch", "suggest_fixes", "verify_fix",
    "compare_plans", "advise_indexes", "explain_sql", "list_rules",
    "fingerprint_sql", "extract_objects",
}

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + str(detail)) if (detail and not cond) else ''}")
    if not cond:
        FAILED.append(name)


def _pick(r, *names, default=None):
    """从 Result 对象或 dict 里取首个存在的字段。"""
    for n in names:
        v = getattr(r, n, None)
        if v is not None:
            return v
    if hasattr(r, "to_dict"):
        d = r.to_dict()
        if isinstance(d, dict):
            for n in names:
                if n in d:
                    return d[n]
    if isinstance(r, dict):
        for n in names:
            if n in r:
                return r[n]
    return default


def _tool_names() -> list[str]:
    try:
        return sorted(server.TOOLS.keys())
    except AttributeError:
        pass
    mcp = server.mcp
    try:
        tools = mcp._tool_manager.list_tools()
    except Exception:  # noqa: BLE001
        res = mcp.list_tools()
        tools = asyncio.get_event_loop().run_until_complete(res) if asyncio.iscoroutine(res) else res
    return sorted(getattr(t, "name", None) or t for t in tools)


def main() -> None:
    print("== 1. MCP 实例与工具注册 ==")
    check("make_server 实例已创建", server.mcp is not None)

    names = _tool_names()
    check("注册工具数 > 0", len(names) > 0, f"实际 {len(names)}")
    print(f"  已注册工具：{names}")
    missing = CORE_TOOLS - set(names)
    check("核心工具全部注册", not missing, f"缺失 {missing}")

    print("== 2. 纯逻辑工具可直接调用（接线可用，无需连库）==")
    r = server.review_sql("UPDATE user SET name='x'")
    v = _pick(r, "verdict")
    check("review_sql 返回『禁止上线』verdict", v is not None and "禁止" in str(v), v)

    print("== 3. 批量门禁 ==")
    b = server.review_batch(["SELECT id FROM t", "UPDATE user SET name='x'"])
    verdict = _pick(b, "verdict")
    data = _pick(b, "data")
    gate = (data or {}).get("gate") if isinstance(data, dict) else None
    failed = str(verdict).lower() == "fail" or str(gate).upper() == "FAIL"
    check("review_batch 门禁判定 FAIL", failed, f"verdict={verdict} gate={gate}")

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        sys.exit(1)
    print("ALL SERVER WIRING PASSED")


if __name__ == "__main__":
    main()
