"""规则分册：子查询风险（SUBQ）。"""

from __future__ import annotations

from sqlglot import exp

from dbcore.rulebook.core import _mk, table_of


def rule_select_subquery(ast, ctx) -> list:
    """SELECT 列表里的相关子查询 —— 每行执行一次，行数放大即为灾难。"""
    if not isinstance(ast, exp.Select):
        return []
    out = []
    for e in ast.expressions or []:
        if isinstance(e, exp.Subquery) or e.find(exp.Subquery) and isinstance(e, exp.Alias):
            out.append(_mk("SUBQ001", "high",
                           "SELECT 列表中包含子查询：相关子查询会对每一行执行一次，复杂度是 O(结果行数)",
                           "改写为 JOIN 或 LEFT JOIN 聚合后的派生表", e, table_of(ast)))
            break
    return out


def rule_cross_join_subquery(ast, ctx) -> list:
    """WHERE 里的 IN (子查询) 在老版本 MySQL 上性能极差。"""
    if not isinstance(ast, exp.Select):
        return []
    d = ctx.get("_dialect", "")
    if d not in ("mysql", "mariadb"):
        return []
    where = ast.args.get("where")
    if where is None:
        return []
    for node in where.walk():
        if isinstance(node, exp.In) and node.find(exp.Subquery) is not None:
            return [_mk("SUBQ002", "medium",
                        "WHERE ... IN (子查询)：MySQL 5.x 会把它优化为 semi-join 但可能选择 DEPENDENT SUBQUERY，"
                        "外层每行都要重跑一次子查询",
                        "改写为 JOIN；或确认 EXPLAIN 里出现 MATERIALIZED / FirstMatch 而非 DEPENDENT SUBQUERY",
                        node, table_of(ast))]
    return []


META: dict[str, dict] = {
    "SUBQ001": {"name": "SELECT 列表中的子查询", "category": "subquery", "severity": "high",
                "why": "相关子查询每行执行一次，复杂度随结果行数线性增长",
                "false_positive": "子查询已是非相关子查询（注意 DEPENDENT 标记）"},
    "SUBQ002": {"name": "IN (子查询)", "category": "subquery", "severity": "medium",
                "why": "MySQL 可能优化为 DEPENDENT SUBQUERY，性能极差",
                "false_positive": "EXPLAIN 显示 MATERIALIZED 或 8.0 的 semi-join 优化生效时",
                "engines": ["mysql", "mariadb"]},
}

RULES: list[tuple[str, object]] = [
    ("SUBQ001", rule_select_subquery),
    ("SUBQ002", rule_cross_join_subquery),
]
