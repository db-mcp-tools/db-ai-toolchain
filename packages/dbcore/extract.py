"""AST → 结构化对象提取。

多个 MCP 都需要同一份理解：表是谁、谓词有哪些、哪些列被套了函数、JOIN 条件如何、
排序是否走索引、分页是否深翻页。这里统一提取一次，避免每个 MCP 各写一套、各错一半。

产出结构是纯 dict（JSON 可序列化），可以直接进 profile-hub、可以跨 MCP 传输。
"""

from __future__ import annotations

from typing import Any

from sqlglot import exp

from dbcore.dialect import normalize_dialect, parse_statements, table_of


def _name(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, exp.Column):
        return node.name
    if isinstance(node, exp.Identifier):
        return node.this
    if isinstance(node, exp.Table):
        return node.name
    return getattr(node, "name", None) or None


def _tables(ast: exp.Expression) -> list[dict]:
    out = []
    for t in ast.find_all(exp.Table):
        db = t.db or ""
        alias = t.alias or ""
        out.append({
            "db": db,
            "name": t.name,
            "alias": alias,
            "qualified": f"{db}.{t.name}" if db else t.name,
            "is_cte": bool(t.db) and t.db.lower() in {"cte"},
            "derived": isinstance(t.this, exp.Subquery) if t.this else False,
        })
    return out


def _columns(ast: exp.Expression) -> list[dict]:
    seen, out = set(), []
    for c in ast.find_all(exp.Column):
        key = f"{c.table or ''}.{c.name}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"table": c.table or "", "column": c.name, "qualified": key})
    return out


def _row(ast: exp.Expression) -> dict | None:
    limit_node = ast.args.get("limit") if isinstance(ast, exp.Select) else None
    if limit_node is None:
        return None
    from dbcore.dialect import limit_value, offset_value  # 局部导入避免循环
    return {"limit": limit_value(ast), "offset": offset_value(ast)}


def _funcs_on_columns(ast: exp.Expression) -> list[dict]:
    """返回所有『形如 FUNC(col) =/>/< 常量』的节点，这类写法会让索引失效。"""
    from dbcore.dialect import unwrap_parens

    res = []
    where = ast.args.get("where")
    if where is None:
        return res
    for node in where.walk():
        n = unwrap_parens(node)
        if not isinstance(n, (exp.EQ, exp.GT, exp.LT, exp.GTE, exp.LTE, exp.NEQ, exp.In, exp.Between)):
            continue
        side = None
        for cand in (getattr(n, "this", None), getattr(n, "expression", None)):
            c = unwrap_parens(cand) if cand is not None else None
            if isinstance(c, exp.Func) and not isinstance(c, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
                side = c
                break
        if side is None:
            continue
        col = side.find(exp.Column)
        res.append({
            "func": side.key.upper(),
            "column": _name(col),
            "operator": type(n).__name__.upper(),
            "expression": side.sql(dialect=None)[:160],
        })
    return res


def _joins(ast: exp.Expression) -> list[dict]:
    out = []
    for j in ast.find_all(exp.Join):
        kinds = [k for k in ("side", "kind") if j.args.get(k)]
        on = j.args.get("on")
        using = j.args.get("using")
        cond_cols = []
        if on is not None:
            cond_cols = [c.name for c in on.find_all(exp.Column)]
        elif using is not None:
            cond_cols = [c.name for c in using.find_all(exp.Column)] if hasattr(using, "find_all") else []
        out.append({
            "type": " ".join(str(j.args.get(k) or "").upper() for k in kinds) or "INNER",
            "source": _name(j.this) if isinstance(j.this, exp.Table) else j.this.key.upper(),
            "on_sql": on.sql(dialect=None)[:200] if on is not None else None,
            "columns": cond_cols,
        })
    return out


def _order(ast: exp.Expression) -> list[dict]:
    order = ast.args.get("order") if isinstance(ast, exp.Select) else None
    if order is None:
        return []
    out = []
    for e in order.expressions:
        desc = bool(e.args.get("desc")) if isinstance(e, exp.Ordered) else False
        nulls = e.args.get("nulls") if isinstance(e, exp.Ordered) else None
        this = e.this if isinstance(e, exp.Ordered) else e
        if isinstance(this, exp.Func):
            out.append({"expr": this.sql(dialect=None)[:120], "kind": "func", "desc": desc, "nulls": nulls})
        elif isinstance(this, exp.Column):
            out.append({"expr": this.name, "kind": "column", "desc": desc, "nulls": nulls})
        else:
            out.append({"expr": this.sql(dialect=None)[:120], "kind": "expr", "desc": desc, "nulls": nulls})
    return out


def _group(ast: exp.Expression) -> list[str]:
    g = ast.args.get("group")
    if g is None:
        return []
    return [e.sql(dialect=None)[:80] for e in g.expressions]


def _aggs(ast: exp.Expression) -> list[dict]:
    out = []
    for f in ast.find_all(exp.Func):
        if isinstance(f, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            inner = f.this
            distinct = bool(f.args.get("distinct"))
            # COUNT(DISTINCT uid) 里 inner 是 Distinct(Column)，必须先展开
            if isinstance(inner, exp.Distinct):
                distinct = True
                inner = inner.expressions[0] if inner.expressions else inner.this
            col = None
            if isinstance(inner, exp.Column):
                col = inner.name
            elif isinstance(inner, exp.Star):
                col = "*"
            elif inner is None:
                col = "*"
            out.append({
                "func": f.key.upper(), "column": col, "distinct": distinct,
                "sql": f.sql(dialect=None)[:120],
            })
    return out


def _predicates(ast: exp.Expression) -> list[dict]:
    where = ast.args.get("where")
    if where is None:
        return []
    out = []
    cond = where.this

    # sqlglot 30 的 And/Or 是 Binary（this/expression），不是 expressions 列表
    def flatten(node) -> list:
        if node is None:
            return []
        if isinstance(node, (exp.And, exp.Or)):
            return flatten(getattr(node, "this", None)) + flatten(getattr(node, "expression", None))
        return [node]

    parts = flatten(cond)
    for p in parts:
        cols = [c.name for c in p.find_all(exp.Column)]
        lit = [l.this for l in p.find_all(exp.Literal)]
        out.append({
            "operator": type(p).__name__.upper(),
            "columns": cols,
            "literals": [str(x)[:60] for x in lit],
            "sql": p.sql(dialect=None)[:200],
            "sargable": not any(isinstance(x, exp.Func) for x in [getattr(p, "this", None)]
                                if x is not None and not isinstance(x, exp.Column)),
        })
    return out


def _subqueries(ast: exp.Expression) -> list[dict]:
    out = []
    for s in ast.find_all(exp.Subquery):
        alias = s.alias_or_name
        out.append({
            "alias": alias,
            "is_uncorrelated_correlated": bool(
                [c for c in s.find_all(exp.Column)
                 if c.table not in ("", alias) and c.table]
            ),
            "sql": s.sql(dialect=None)[:200],
        })
    return out


def extract_objects(sql: str, dialect: str | None = None) -> dict:
    """把一段 SQL 提取为结构化对象。多语句时，返回每条语句各自的结构。"""
    d = normalize_dialect(dialect)
    stmts = parse_statements(sql, d)
    items = []
    for i, st in enumerate(stmts):
        ast = st.ast
        item: dict[str, Any] = {
            "index": i,
            "kind": st.kind,
            "dialect": d,
            "parse_error": st.parse_error,
            "sql": st.sql[:2000],
            "line": getattr(st, "line", None),
        }
        if ast is None:
            items.append(item)
            continue
        item.update({
            "tables": _tables(ast),
            "table_count": len(_tables(ast)),
            "columns": _columns(ast),
            "select_star": bool(ast.expressions) and any(isinstance(e, exp.Star) for e in ast.expressions)
                if isinstance(ast, exp.Select) else False,
            "predicates": _predicates(ast),
            "functions_on_columns": _funcs_on_columns(ast),
            "joins": _joins(ast),
            "order_by": _order(ast),
            "group_by": _group(ast),
            "aggregates": _aggs(ast),
            "limit_offset": _row(ast),
            "subqueries": _subqueries(ast),
            "has_distinct": bool(ast.args.get("distinct")) if isinstance(ast, exp.Select) else False,
            "primary_table": table_of(ast),
        })
        items.append(item)
    return {
        "dialect": d,
        "statement_count": len(items),
        "statements": items,
        "all_tables": sorted({t["qualified"] for it in items for t in (it.get("tables") or [])}),
    }
