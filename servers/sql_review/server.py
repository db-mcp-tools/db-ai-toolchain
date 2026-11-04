"""sql-review MCP —— 数据库工具链的第一个能力原子。

工具清单（都能被任意 Agent 直接调用）：
  - review_sql               SQL 静态体检，零风险，可直接接 CI
  - assess_change            DDL 变更风险评级 + 锁影响 + 推荐工具
  - generate_rollback        反向 DDL（不可逆操作会明说，不编造）
  - precheck_change_window   执行窗口前置检查 go/no-go（需连库）
  - explain_sql              取执行计划并给出可疑点（需连库）
  - instance_history         读实例累计档案 —— 所有 MCP 共享的那份资产

运行： python -m servers.sql_review.server
"""

from __future__ import annotations

import json
from typing import Any

try:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:  # mcp >= 2.0：FastMCP 已更名 MCPServer
    from mcp.server.mcpserver import MCPServer as _Server  # type: ignore[assignment]

from dbcore.advisor import advise_indexes
from dbcore.connectors import explain as explain_query
from dbcore.connectors import precheck_change_window
from dbcore.dialect import normalize_dialect
from dbcore.fix import suggest_fixes
from dbcore.guard import GuardError
from dbcore.profile_store import get_history, instance_summary, record_audit, record_observation
from dbcore.risk import assess_change, generate_rollback
from dbcore.rules import review_sql
from dbcore.verify import equivalence_check, plan_compare

SERVER_NAME = "db-sql-review"

mcp = _Server(SERVER_NAME)


def _audit(tool: str, instance_id: str | None, summary: str) -> None:
    try:
        record_audit(tool, instance_id, summary)
    except Exception:
        pass


def _fail(msg: str) -> dict:
    return {"error": msg}


@mcp.tool()
def review_sql_tool(sql: str, dialect: str = "mysql", deep_offset_threshold: int = 1000,
                    instance_id: str | None = None, remember: bool = True) -> dict:
    """对 SQL 做上线前静态体检。返回一个 verdict（禁止上线/建议修改/可上线）和逐条 Findings。

    severity 语义：blocker=必须拦下，high=强烈建议改，medium=建议改，low/info=放行。
    """
    try:
        d = normalize_dialect(dialect)
    except ValueError as e:
        return _fail(str(e))

    result = review_sql(sql, d, deep_offset_threshold=deep_offset_threshold)
    payload = result.to_dict()

    if instance_id and remember:
        record_observation(instance_id, "sql_review", {
            "verdict": payload["verdict"],
            "max_severity": payload["max_severity"],
            "summary": payload["summary"],
            "sample": sql[:300],
        })
    _audit("review_sql", instance_id, f"{payload['verdict']} / {payload['summary']}")
    return payload


@mcp.tool()
def assess_change_tool(sql: str, dialect: str = "mysql", table_rows: int | None = None,
                       during_peak: bool = False, instance_id: str | None = None) -> dict:
    """评估 DDL 的变更风险。给出 L0-L3 风险等级、锁影响说明、推荐工具(gh-ost/pt-osc/CONCURRENTLY)、
    前置条件清单和回滚语句。table_rows 强烈建议传入 —— 大表和小表完全是两种方案。
    """
    try:
        d = normalize_dialect(dialect)
    except ValueError as e:
        return _fail(str(e))

    plan = assess_change(sql, d, table_rows=table_rows, during_peak=during_peak)
    payload = plan.to_dict()

    if instance_id:
        record_observation(instance_id, "change_risk", {
            "overall_risk": payload["overall_risk"],
            "ops": [{"op": o["op_type"], "table": o["table"], "risk": o["risk"], "tool": o["recommended_tool"]} for o in payload["ops"]],
            "sample": sql[:300],
        })
    _audit("assess_change", instance_id, f"{payload['overall_risk']}")
    return payload


@mcp.tool()
def generate_rollback_tool(sql: str, dialect: str = "mysql") -> dict:
    """生成 DDL 的反向回滚语句。不可逆操作（DROP COLUMN/TRUNCATE/改类型）不会编造回滚语句，
    而是明确告知需要的事前备份动作。
    """
    try:
        d = normalize_dialect(dialect)
    except ValueError as e:
        return _fail(str(e))
    return {"dialect": d, "rollback": generate_rollback(sql, d)}


@mcp.tool()
def precheck_change_window_tool(url: str, dialect: str | None = None, long_tx_seconds: int = 60) -> dict:
    """连库执行变更窗口前置检查：长事务、复制延迟、online alter 参数等，输出 go/no-go。
    url 形如 mysql://user:pass@host:3306/db。会话强制只读 + 语句超时。
    """
    try:
        result = precheck_change_window(url, dialect, long_tx_seconds=long_tx_seconds)
    except GuardError as e:
        return _fail(f"只读护栏拦截：{e}")
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")
    record_observation(result["instance_id"], "change_window", {c["name"]: c["value"] for c in result["checks"]})
    _audit("precheck_change_window", result["instance_id"], result["verdict"])
    return result


@mcp.tool()
def explain_sql_tool(url: str, sql: str, dialect: str | None = None, analyze: bool = False,
                     instance_id: str | None = None) -> dict:
    """取目标库的执行计划并标注可疑点（全表扫描 / filesort / 临时表 / 统计信息过期）。
    analyze=True 会真实执行语句，生产库慎用。
    """
    try:
        plan = explain_query(url, sql, dialect, analyze=analyze)
    except GuardError as e:
        return _fail(f"只读护栏拦截：{e}")
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")
    if instance_id:
        record_observation(instance_id, "explain", {"issues": plan["issues"], "plan_text": plan["plan_text"][:600], "sql": sql[:300]})
    _audit("explain_sql", plan.get("instance_id"), f"issues={len(plan['issues'])}")
    return plan


@mcp.tool()
def instance_history_tool(instance_id: str, kind: str | None = None, limit: int = 20) -> dict:
    """读取该数据库实例的累计观测档案（历次体检、风险评估、窗口检查、执行计划）。
    这是跨 Agent 复用的"记忆"——别的 Agent 第一天只看到一条 SQL，你看到的是这张表三个月的历史。
    """
    return {
        "summary": instance_summary(instance_id),
        "history": get_history(instance_id, kind, limit=limit),
    }


@mcp.tool()
def suggest_fixes_tool(sql: str, dialect: str = "mysql", columns: list[str] | None = None,
                       order_column: str | None = None, pk_column: str | None = None,
                       predicate: str | None = None, deep_offset_threshold: int = 1000,
                       instance_id: str | None = None) -> dict:
    """指出问题并给出解决办法：返回重写后的 SQL、配套 DDL、验证用 SQL、以及改写后再体检的闭环结果。

    fixed_sql 的 sql_kind 语义：
      executable      可直接拿去跑
      template        需要补占位符/先建索引才能跑，caveats 里写清了
      needs_metadata  缺少 schema 信息，follow_up_sql 里给了「查字段清单」的 SQL
      manual          无法安全自动改写，已给出人工改法
    columns 传了，SELECT * 和 INSERT 无字段名这两类就能直接改写列表。
    """
    try:
        d = normalize_dialect(dialect)
    except ValueError as e:
        return _fail(str(e))

    payload = suggest_fixes(
        sql, d, columns=columns, order_column=order_column, pk_column=pk_column,
        predicate=predicate, deep_offset_threshold=deep_offset_threshold,
    )
    if instance_id:
        record_observation(instance_id, "fix_suggestion", {
            "total": payload["total"], "actionable": payload["actionable"],
            "resolved": [i["rule_id"] for i in payload["items"] if (i.get("recheck") or {}).get("rule_resolved")],
        })
    _audit("suggest_fixes", instance_id, f"total={payload['total']} actionable={payload['actionable']}")
    return payload


@mcp.tool()
def verify_fix_tool(before_sql: str, after_sql: str, dialect: str = "mysql",
                    url: str | None = None, analyze: bool = False) -> dict:
    """改写合法性验证。三层证据一次跑完：
    1) 静态：改写前后各体检一次，确认原问题消失且没引入新的高危问题；
    2) 执行计划（传 url 时）：对比 type/key/rows/Extra，验证是否真的变快；
    3) 行数一致（传 url 时）：把两条 SQL 包成 COUNT(*) 对比，排除「数据放多了/放少了」。
    """
    try:
        d = normalize_dialect(dialect)
    except ValueError as e:
        return _fail(str(e))

    before, after = review_sql(before_sql, d), review_sql(after_sql, d)
    before_rules = {f.rule_id for f in before.findings}
    after_rules = {f.rule_id for f in after.findings}
    resolved = sorted(before_rules - after_rules)
    introduced = sorted(after_rules - before_rules)
    high_introduced = [f.to_dict() for f in after.findings if f.rule_id in introduced and f.severity in ("blocker", "high")]

    report: dict = {
        "dialect": d,
        "static": {
            "before_verdict": before.verdict,
            "after_verdict": after.verdict,
            "resolved_rules": resolved,
            "introduced_rules": introduced,
            "introduced_high_risk": high_introduced,
        },
        "plan": None,
        "equivalence": None,
    }

    if url:
        try:
            report["plan"] = plan_compare(url, before_sql, after_sql, d, analyze=analyze)
        except Exception as e:
            report["plan"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            report["equivalence"] = equivalence_check(url, before_sql, after_sql, d)
        except Exception as e:
            report["equivalence"] = {"error": f"{type(e).__name__}: {e}"}

    plan = report["plan"] or {}
    eq = report["equivalence"] or {}
    ok = bool(resolved) and not high_introduced
    if isinstance(plan, dict) and plan.get("verdict") and "有效" not in str(plan.get("verdict")):
        ok = False
    if isinstance(eq, dict) and eq.get("row_count_equal") is False:
        ok = False
    report["verdict"] = "改写通过验证" if ok else "改写存在风险，禁止直接上线"
    _audit("verify_fix", None, report["verdict"])
    return report


@mcp.tool()
def plan_compare_tool(url: str, before_sql: str, after_sql: str, dialect: str | None = None,
                      analyze: bool = False, instance_id: str | None = None) -> dict:
    """只对比执行计划：抽取出 access type、命中索引、预计行数、是否 filesort/临时表，
    并给出「预计扫描行数下降多少」这类可量化结论。会话只读。
    """
    try:
        result = plan_compare(url, before_sql, after_sql, dialect, analyze=analyze)
    except GuardError as e:
        return _fail(f"只读护栏拦截：{e}")
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")
    if instance_id:
        record_observation(instance_id, "plan_compare", {"gains": result["gains"], "verdict": result["verdict"]})
    _audit("plan_compare", instance_id, result["verdict"])
    return result


@mcp.tool()
def equivalence_check_tool(url: str, before_sql: str, after_sql: str, dialect: str | None = None) -> dict:
    """行数一致性校验：把改写前后的 SQL 各包一层 COUNT(*) 执行比对。只读、安全。
    行数一致不等于语义等价，但行数不一致一定不能用。
    """
    try:
        result = equivalence_check(url, before_sql, after_sql, dialect)
    except GuardError as e:
        return _fail(f"只读护栏拦截：{e}")
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")
    _audit("equivalence_check", None, result["verdict"])
    return result


@mcp.tool()
def advise_indexes_tool(url: str, table: str, candidates: list[str], dialect: str | None = None,
                        order_column: str | None = None) -> dict:
    """索引方案建议：先查该表现有哪些索引，判断要不要新建——重复索引和前缀已覆盖的情况会直接劝退，
    然后按选择性排序给出可执行的建索引 DDL（MySQL 用 ALTER，PG 用 CREATE INDEX CONCURRENTLY）。
    """
    try:
        result = advise_indexes(url, table, candidates, dialect, order_column=order_column)
    except MissingDriver as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")
    record_observation(result["instance_id"], "index_advice",
                       {"table": table, "create": result["proposal"]["should_create"], "ddl": result["proposal"]["ddl"]})
    _audit("advise_indexes", result["instance_id"], result["proposal"]["verdict"])
    return result


@mcp.tool()
def batch_review_tool(sqls: list[str], dialect: str = "mysql") -> dict:
    """批量体检（CI 场景用）。输入多条 SQL，输出逐条结论 + 汇总，便于在流水线里卡门禁。"""
    try:
        d = normalize_dialect(dialect)
    except ValueError as e:
        return _fail(str(e))

    items: list[dict[str, Any]] = []
    for s in sqls:
        payload = review_sql(s, d).to_dict()
        items.append({"sql": s[:200], "verdict": payload["verdict"], "max_severity": payload["max_severity"],
                      "findings": payload["findings"]})
    gate_failed = [i for i in items if i["max_severity"] in ("blocker", "high")]
    return {
        "dialect": d,
        "total": len(items),
        "ci_verdict": "FAIL" if gate_failed else "PASS",
        "blocking": len(gate_failed),
        "items": items,
    }


if __name__ == "__main__":
    mcp.run()
