"""验证层：改完之后，用目标库的真实执行计划和行数证明「改对了」而不是「改了」。

只有两条能提高置信度的事实：
1. 执行计划变好了（全表扫描消失 / 预计扫描行数下降 / filesort 消失）；
2. 行数一致（必要条件，不是充分条件）。
"""

from __future__ import annotations

import re

from dbcore.connectors import explain
from dbcore.dialect import normalize_dialect

_ALL_TABLE_ACCESS = ("ALL", "index")


def _mysql_metrics(result: dict) -> dict:
    cols = [c.lower() for c in result["columns"]]
    rows = result["rows"]
    idx = {c: i for i, c in enumerate(cols)}
    steps = []
    total_rows = 0
    for r in rows:
        step = {
            "table": r[idx.get("table", -1)] if "table" in idx else None,
            "access": r[idx["type"]] if "type" in idx else None,
            "key": r[idx["key"]] if "key" in idx else None,
            "rows": int(r[idx["rows"]]) if "rows" in idx and isinstance(r[idx["rows"]], (int, float)) else None,
            "extra": r[idx["extra"]] if "extra" in idx else None,
        }
        steps.append(step)
        if step["rows"]:
            total_rows += step["rows"]
    return {
        "engine": "mysql",
        "steps": steps,
        "estimated_rows": total_rows,
        "full_scan": any(s["access"] in _ALL_TABLE_ACCESS for s in steps),
        "filesort": any(s["extra"] and "filesort" in str(s["extra"]).lower() for s in steps),
        "temporary": any(s["extra"] and "temporary" in str(s["extra"]).lower() for s in steps),
        "plan_text": result.get("plan_text", ""),
    }


def _pg_metrics(result: dict) -> dict:
    text = result.get("plan_text", "")
    low = text.lower()
    rows = [int(n.replace(",", "")) for n in re.findall(r"rows=(\d+)", low)]
    return {
        "engine": "postgres",
        "steps": [],
        "estimated_rows": max(rows) if rows else 0,
        "full_scan": "seq scan" in low,
        "filesort": "sort" in low and "sort key" in low,
        "temporary": "temporary" in low or "external merge" in low,
        "plan_text": text,
    }


def plan_metrics(url: str, sql: str, dialect: str | None = None, analyze: bool = False) -> dict:
    d = normalize_dialect(dialect)
    if d not in ("mysql", "postgres"):
        d = "mysql"
    result = explain(url, sql, d, analyze=analyze)
    return _mysql_metrics(result) if d == "mysql" else _pg_metrics(result)


def plan_compare(url: str, before_sql: str, after_sql: str, dialect: str | None = None, analyze: bool = False) -> dict:
    """对比改写前后的执行计划，输出「是否真的变好了」的结论。"""
    d = normalize_dialect(dialect)
    before = plan_metrics(url, before_sql, d, analyze=analyze)
    after = plan_metrics(url, after_sql, d, analyze=analyze)

    gains: list[str] = []
    if before["full_scan"] and not after["full_scan"]:
        gains.append("全表扫描已消除")
    if before["filesort"] and not after["filesort"]:
        gains.append("filesort 已消除")
    if before["temporary"] and not after["temporary"]:
        gains.append("临时表已消除")
    if before["estimated_rows"] and after["estimated_rows"]:
        ratio = 1 - after["estimated_rows"] / max(before["estimated_rows"], 1)
        if ratio > 0.1:
            gains.append(f"预计扫描行数 {before['estimated_rows']:,} → {after['estimated_rows']:,}（-{ratio:.0%}）")
        elif ratio < -0.1:
            gains.append(f"⚠ 预计扫描行数反而上升 {before['estimated_rows']:,} → {after['estimated_rows']:,}")

    return {
        "before": before,
        "after": after,
        "gains": gains,
        "verdict": "改写确实有效" if gains and not any(g.startswith("⚠") for g in gains)
                   else ("需要重新评估改写方案" if any(g.startswith("⚠") for g in gains) else "执行计划层面未见明显变化"),
    }


def equivalence_check(url: str, before_sql: str, after_sql: str, dialect: str | None = None) -> dict:
    """行数一致性校验：把两条 SQL 包成 COUNT(*) 分别执行（只读会话）。

    注意：行数一致只能排除「数据放多/放少」，不能证明语义完全等价。
    """
    from dbcore.connectors import run_readonly

    d = normalize_dialect(dialect)

    def count_of(sql: str) -> dict:
        wrapped = f"SELECT COUNT(*) AS cnt FROM ({sql.rstrip().rstrip(';')}) AS _dbt_probe"
        try:
            r = run_readonly(url, wrapped, d, max_rows=1)
            return {"ok": True, "count": int(r["rows"][0][0]) if r["rows"] else None}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    b, a = count_of(before_sql), count_of(after_sql)
    equal = None
    if b.get("ok") and a.get("ok") and b["count"] is not None and a["count"] is not None:
        equal = b["count"] == a["count"]
    return {
        "before": b,
        "after": a,
        "row_count_equal": equal,
        "verdict": {"True": "行数一致（语义仍需人工确认）", "False": "行数不一致，禁止采用该改写", "None": "校验未完成"}[str(equal)],
    }
