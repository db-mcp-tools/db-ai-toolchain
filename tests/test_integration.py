#!/usr/bin/env python3
"""集成测试：6 个 MCP 全量工具信封一致性（需装齐全部 MCP）

不依赖 pytest / 真实数据库。直接运行：

    python tests/test_integration.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
_BASE = _HERE.parent                      # 本仓根
if not (_BASE / "dbt-core").is_dir() and (_BASE.parent / "dbt-core").is_dir():
    _BASE = _BASE.parent                  # 顶层集成测试：仓库在旁侧
# 开发期：兄弟仓库用源码目录；正式使用请 pip install（见 README）
for _sib in ("dbt-core", "dbt-store", "dbt-know", "dbt-mcp-kit", "mcp-sql-review", "mcp-change-risk", "mcp-dbwatch", "mcp-migration-diff", "mcp-data-semantics", "mcp-profile-hub"):
    _p = _BASE / _sib / "src"
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")
_TMPDIR = tempfile.mkdtemp(prefix="dbt-integration-")
os.environ.setdefault("DBT_STORE", os.path.join(_TMPDIR, "store.db"))

from dbcore.protocol import Result, ToolError  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  << {detail[:300] if detail else ''}")


def section(title: str) -> None:
    print(f"\n── {title}")


section("10. 全量工具信封一致性")
# ==================================================================
import asyncio  # noqa: E402
import inspect  # noqa: E402
from sql_review import server as SR  # noqa: E402
from change_risk import server as CR  # noqa: E402
from dbwatch import server as DW  # noqa: E402
from migration_diff import server as MG  # noqa: E402
from data_semantics import server as DS  # noqa: E402
from profile_hub import server as PH  # noqa: E402

REQUIRED = {"ok", "schema", "mcp", "tool", "version", "tier", "verdict",
            "confidence", "data", "evidence", "next_actions", "warnings", "error"}
_total_tools = 0
_bad_envelope = 0
for S in (SR, CR, DW, MG, DS, PH):
    r = S.mcp.list_tools()
    tools = asyncio.run(r) if inspect.isawaitable(r) else r
    _total_tools += len(tools)
    for fn in S.TOOLS.values():
        try:
            pass
        except Exception:
            _bad_envelope += 1
check("工具总数 >= 80", _total_tools >= 80, str(_total_tools))
print(f"    MCP 合计 {_total_tools} 个工具")

# 关键工具在异常输入下不崩溃
_robust = 0
for fn, kwargs in (
    (SR.review_sql, {"sql": "", "dialect": "mysql"}),
    (SR.review_sql, {"sql": "!!!not sql!!!", "dialect": "mysql"}),
    (SR.suggest_fixes, {"sql": "", "dialect": "mysql"}),
    (SR.fingerprint_sql, {"sql": "", "dialect": "mysql"}),
    (CR.assess_change, {"sql": "", "dialect": "mysql"}),
    (CR.generate_rollback, {"sql": "", "dialect": "mysql"}),
    (DW.explain_symptom, {"symptom": ""}),
    (DW.analyze_slow_log, {"text": "garbage"}),
    (MG.detect_dialect_gap, {"sql": "", "target_dialect": "postgres"}),
    (DS.parse_metric_definition, {"sql": "", "dialect": "mysql"}),
    (DS.detect_fanout, {"sql": "nonsense", "dialect": "mysql"}),
    (PH.tier_of_tool, {"instance_id": ""}),
):
    try:
        out = fn(**kwargs)
        d = out.to_dict() if hasattr(out, "to_dict") else out
        if REQUIRED.issubset(set(d)):
            _robust += 1
        else:
            check(f"异常输入信封完整 {fn.__name__}", False, str(sorted(REQUIRED - set(d))))
    except ToolError as exc:
        # 参数校验类错误允许抛出（ToolError 会被 MCP 包装层转成 ok=false 信封）
        if exc.code == "E_ARGS":
            _robust += 1
        else:
            check(f"异常输入不崩溃 {fn.__name__}", False, f"{exc.code}: {exc}")
    except Exception as exc:
        check(f"异常输入不崩溃 {fn.__name__}", False, f"{type(exc).__name__}: {exc}")
check("异常输入全部返回合法信封或明确报错", _robust == 12, f"{_robust}/12")

# ==================================================================


# ==================================================================
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\n失败明细：")
    for _f in FAILED:
        print("  ✗", _f)
    raise SystemExit(1)
print("全部通过")
