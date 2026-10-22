"""规则分册：数据类型与 NULL 语义（TYPE / NULL）。"""

from __future__ import annotations

import re

from sqlglot import exp

from dbcore.rulebook.core import (_NUMERIC_COL_RE, _literal_of, _mk, table_of)


def rule_implicit_cast(ast, ctx) -> list:
    """数字型字段与字符串字面量比较 —— 会导致整列隐式转换，索引失效。"""
    out: list = []
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return out
    seen: set[str] = set()
    for node in where.walk():
        if isinstance(node, (exp.EQ, exp.In, exp.GTE, exp.LTE, exp.GT, exp.LT)):
            col = node.find(exp.Column)
            lit = node.find(exp.Literal)
            if col is None or lit is None:
                continue
            # ★ 只有字符串字面量才可能是隐式转换；数字字面量发改我们要自己学会分辨
            if not getattr(lit, "is_string", False):
                continue
            raw = getattr(lit, "this", None)
            if not isinstance(raw, str):
                continue
            # 疑似数值列：列名的常见数值后缀，或字面量是纯数字
            colname = col.name or ""
            looks_numeric = raw.isdigit() and bool(
                re.search(r"(^|_)(id|_id|no|num|count|qty|amount|total|seq|coin|score)$", colname, re.I)
            ) or (raw.isdigit() and re.fullmatch(r"\d+", raw) and colname.lower() in
                  ("id", "uid", "user_id", "order_id"))
            # 只有列名带数值特征时才敢判 high（我们不知道真实列类型）；
            # 否则降到 low，作为"需要确认"的提示，避免对字符列乱报
            colname = col.name or ""
            numeric_name = bool(_NUMERIC_COL_RE.search(colname))
            out.append(_mk("TYPE001", "high" if numeric_name else "low",
                           f"列 `{colname}` 与字符串字面量 '{raw}' 比较，"
                           f"若该列是数值类型会触发隐式转换使索引失效",
                           "去掉引号：让字面量类型与列类型一致；MySQL/PG 都会对列做 CAST 而非对常量做 CAST",
                           node, table_of(ast)))
    return out


def rule_negation(ast, ctx) -> list:
    out: list = []
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return out
    for node in where.walk():
        if isinstance(node, exp.NEQ):
            col = node.find(exp.Column)
            if col is None:
                continue
            out.append(_mk("TYPE002", "low",
                           f"`{col.name} != 值` 这类否定条件通常无法有效利用索引（选择性差）",
                           "如果绝大多数行都满足该否定条件，考虑改写为正向枚举或用覆盖索引",
                           node, table_of(ast)))
            break
        if isinstance(node, exp.Not):
            inner = node.this
            if isinstance(inner, exp.In):
                out.append(_mk("TYPE002", "low",
                               "使用了 NOT IN：优化器通常无法走索引，且子查询返回 NULL 时结果为空集",
                               "改写为 NOT EXISTS，并显式处理 NULL 语义", node, table_of(ast)))
                break
    return out


def rule_not_null_safe(ast, ctx) -> list:
    """NULL 语义陷阱：col <> 'x' 会漏掉 col IS NULL 的行。"""
    out: list = []
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return out
    for node in where.walk():
        if isinstance(node, exp.NEQ):
            rhs = node.expression
            if isinstance(rhs, exp.Literal):
                col = node.find(exp.Column)
                if col is not None:
                    out.append(_mk("NULL001", "medium",
                                   f"条件 `{col.name} <> ...` 不会包含 `{col.name} IS NULL` 的行",
                                   "如果你希望 NULL 行也被包含，必须显式写 `OR col IS NULL`",
                                   node, table_of(ast)))
                    break
    return out


# 本分册的 type 规则依赖 _NUMERIC_COL_RE
META: dict[str, dict] = {
    "TYPE001": {"name": "隐式类型转换", "category": "type", "severity": "high",
                "why": "数值列与字符串比较，数据库会对列做 CAST，索引直接失效",
                "false_positive": "列本身是字符类型且存的就是数字串（设计问题，应改类型）"},
    "TYPE002": {"name": "否定条件", "category": "type", "severity": "low",
                "why": "!= / NOT IN 的选择性通常很差，索引收益有限",
                "false_positive": "列只有极少数值且分布极度倾斜时"},
    "NULL001": {"name": "NULL 语义陷阱", "category": "semantic", "severity": "medium",
                "why": "col <> 'x' 不会匹配 col IS NULL 的行，导致数据静默丢失",
                "false_positive": "明确知道该列 NOT NULL 时可忽略"},
}

RULES: list[tuple[str, object]] = [
    ("TYPE001", rule_implicit_cast),
    ("TYPE002", rule_negation),
    ("NULL001", rule_not_null_safe),
]
