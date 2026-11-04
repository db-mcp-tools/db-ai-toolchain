"""修正引擎：不只是指出问题，而是给出改好的 SQL、配套 DDL、以及验证方法。

三条自律：
1. 能自动改写的，直接给出可执行 SQL；
2. 改写有语义风险的，给出模板 + 必须执行的验证 SQL，confidence 标低；
3. 无法安全改写的（如需要 schema 信息），不编造，而是给出「查 schema 的 SQL」或者「先数一下会死多少行」这类真正能推动决策的动作。

每条修正都会做「改写闭环验证」：把改好的 SQL 重新体检一遍，确认原规则确实不再触发。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlglot import exp

from dbcore.dialect import normalize_dialect, parse_statements, table_of
from dbcore.rules import Finding, review_sql

EXECUTABLE = "executable"
TEMPLATE = "template"
NEEDS_METADATA = "needs_metadata"
MANUAL = "manual"


@dataclass
class Fix:
    rule_id: str
    problem: str
    action: str
    fixed_sql: str | None = None
    sql_kind: str = MANUAL
    confidence: str = "medium"  # high / medium / low
    side_ddl: list[str] = field(default_factory=list)
    follow_up_sql: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    recheck: dict | None = None
    composable: bool = False  # 模板类改写是否允许并入最终 SQL（需要先建索引的一律为 False）

    def to_dict(self) -> dict:
        return asdict(self)


def _unwrap(node: exp.Expression | None) -> exp.Expression | None:
    """去掉括号包装，拿到真实谓词。"""
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _from_of(ast: exp.Expression) -> exp.From | None:
    """sqlglot 各版本里 from 的 arg key 不一致（30.x 是 from_）。"""
    return ast.args.get("from") or ast.args.get("from_")


def _pk_of(ast: exp.Expression, dialect: str, pk_column: str | None) -> str:
    return pk_column or "id"


# --------------------------------------------------------------------- 改写器


def _fix_date_range(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    """DATE(col)='2026-01-01' → col >= '2026-01-01' AND col < '2026-01-02'（索引可用）。"""
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    where = ast.args.get("where")
    if where is None:
        return None

    hit = None
    for node in where.walk():
        node = _unwrap(node)
        if isinstance(node, exp.EQ) and isinstance(_unwrap(node.this), exp.Func):
            func = _unwrap(node.this)
            # sqlglot 会把 DATE(col) 规范化成 TsOrDsToDate，兼容类名与 key 两种判断
            is_date_fn = isinstance(func, exp.TsOrDsToDate) or func.key.upper() in ("DATE", "TO_DATE", "TSORDSTODATE")
            if not is_date_fn:
                continue
            col = func.this if isinstance(func.this, exp.Column) else func.find(exp.Column)
            rhs = _unwrap(node.expression)
            val = getattr(rhs, "this", None) if isinstance(rhs, exp.Literal) else None
            if col is None or not isinstance(val, str):
                continue
            hit = (node, col, val)
            break
    if hit is None:
        return None

    node, col, raw = hit
    try:
        day = dt.date.fromisoformat(raw.strip())
    except ValueError:
        try:
            day = dt.datetime.fromisoformat(raw.strip()).date()
        except ValueError:
            return None

    start = day.isoformat()
    end = (day + dt.timedelta(days=1)).isoformat()
    replacement = exp.and_(
        exp.GTE(this=col.copy(), expression=exp.Literal.string(start)),
        exp.LT(this=col.copy(), expression=exp.Literal.string(end)),
    )
    node.replace(replacement)
    new_sql = ast.sql(dialect=dialect)
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action=f"把函数谓词改写成半开区间范围查询（[{start}, {end})）",
        fixed_sql=new_sql,
        sql_kind=EXECUTABLE,
        confidence="high",
        caveats=["确认该字段是 DATETIME/TIMESTAMP 类型；若为字符串日期则不适用"],
        follow_up_sql=[f"SELECT COUNT(*) FROM {table_of(ast)} WHERE {col.sql(dialect=dialect)} >= '{start}' AND {col.sql(dialect=dialect)} < '{end}';"],
    )


def _fix_year_range(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    where = ast.args.get("where")
    if where is None:
        return None

    hit = None
    for node in where.walk():
        node = _unwrap(node)
        if isinstance(node, exp.EQ) and isinstance(_unwrap(node.this), exp.Func):
            func = _unwrap(node.this)
            if func.key.upper() != "YEAR":
                continue
            col = func.find(exp.Column)
            rhs = _unwrap(node.expression)
            val = getattr(rhs, "this", None) if isinstance(rhs, exp.Literal) else None
            if col is None or not isinstance(val, str) or not val.strip().isdigit():
                continue
            hit = (node, col, int(val.strip()))
            break
    if hit is None:
        return None

    node, col, year = hit
    start, end = f"{year}-01-01", f"{year + 1}-01-01"
    node.replace(
        exp.and_(
            exp.GTE(this=col.copy(), expression=exp.Literal.string(start)),
            exp.LT(this=col.copy(), expression=exp.Literal.string(end)),
        )
    )
    new_sql = ast.sql(dialect=dialect)
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action=f"YEAR(col)={year} 改写成范围条件 [{start}, {end})",
        fixed_sql=new_sql,
        sql_kind=EXECUTABLE,
        confidence="high",
    )


def _fix_substr_prefix(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    """SUBSTR(col,1,3)='A1' → col LIKE 'A1%'（前缀可走索引）。"""
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    where = ast.args.get("where")
    if where is None:
        return None

    hit = None
    for node in where.walk():
        node = _unwrap(node)
        if not isinstance(node, exp.EQ):
            continue
        fn = _unwrap(node.this)
        if not isinstance(fn, (exp.Func, exp.Substring)) or fn.key.upper() not in ("SUBSTR", "SUBSTRING", "LEFT"):
            continue
        # sqlglot 里 SUBSTRING 的参数在 this/start/length，不是 expressions
        col = fn.this if isinstance(fn.this, exp.Column) else fn.find(exp.Column)
        if col is None:
            continue
        start_node = _unwrap(fn.args.get("start") or (fn.expressions[0] if fn.expressions else None))
        if getattr(start_node, "this", None) != "1":
            continue
        rhs = _unwrap(node.expression)
        val = getattr(rhs, "this", None) if isinstance(rhs, exp.Literal) else None
        if not isinstance(val, str):
            continue
        hit = (node, col, val)
        break
    if hit is None:
        return None

    node, col, val = hit
    node.replace(exp.Like(this=col.copy(), expression=exp.Literal.string(f"{val}%")))
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action=f"截断取前缀改写为 LIKE '{val}%'，让前缀索引可用",
        fixed_sql=ast.sql(dialect=dialect),
        sql_kind=EXECUTABLE,
        confidence="high",
        caveats=["确认原语义是「以前缀匹配」而非「精确等于该长度子串」"],
    )


def _fix_select_star(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    cols = opts.get("columns") or []
    if not cols:
        return Fix(
            rule_id=finding.rule_id,
            problem=finding.message,
            action="展开 SELECT * 为显式字段列表（需要表字段清单，工具没有你的 schema，先查出来）",
            sql_kind=NEEDS_METADATA,
            confidence="medium",
            follow_up_sql=[
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '<表名>' ORDER BY ORDINAL_POSITION;"
                if dialect == "mysql"
                else "SELECT column_name FROM information_schema.columns "
                     "WHERE table_schema = 'public' AND table_name = '<表名>' ORDER BY ordinal_position;"
            ],
            caveats=["拿到字段清单后重新调用本工具并传入 columns 参数，可直接得到改写后的 SQL"],
        )
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    ast.set("expressions", [exp.column(c) for c in cols])
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action="展开为显式字段列表",
        fixed_sql=ast.sql(dialect=dialect),
        sql_kind=EXECUTABLE,
        confidence="high",
    )


def _fix_leading_like(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    where = ast.args.get("where")
    if where is None:
        return None

    hit = None
    for like in where.find_all(exp.Like):
        pattern = like.args.get("expression")
        val = getattr(pattern, "this", None)
        if isinstance(val, str) and val.startswith(("%", "_")):
            col = like.this
            if isinstance(col, exp.Column):
                hit = (like, col, val)
                break
    if hit is None:
        return None

    like, col, val = hit
    colname = col.name
    table = table_of(ast) or "<table>"
    stripped = val.lstrip("%_")
    reversed_val = stripped[::-1]

    if dialect == "mysql":
        ddl = [
            f"ALTER TABLE {table} ADD COLUMN {colname}_rev VARCHAR(255) "
            f"GENERATED ALWAYS AS (REVERSE({colname})) STORED;",
            f"ALTER TABLE {table} ADD INDEX idx_{colname}_rev ({colname}_rev);",
        ]
    else:
        ddl = [f"CREATE INDEX idx_{colname}_rev ON {table} (REVERSE({colname}));"]

    like.replace(exp.Like(this=exp.column(f"{colname}_rev"), expression=exp.Literal.string(f"{reversed_val}%")))
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action="无法靠原索引解决：改造成「倒序冗余列 + 倒序前缀索引」，把后缀匹配变成前缀匹配",
        fixed_sql=ast.sql(dialect=dialect),
        sql_kind=TEMPLATE,
        confidence="medium",
        side_ddl=ddl,
        caveats=[
            "改写后的 SQL 依赖新加的冗余列，必须先建好索引再上线查询",
            "生成器/ORM 若直接写 SQL，需要同步维护该列的写入",
            "另一个选项是接入全文检索/ES，数据量或模糊需求更大时优先选这条",
        ],
    )


def _fix_deep_offset(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    order = ast.args.get("order")
    # 游标字段必须是真实字段（见下方判定），占位符统一用 :last_val
    if order is None:
        return Fix(
            rule_id=finding.rule_id,
            problem=finding.message,
            action="改游标分页：先用 LIMIT 无 ORDER BY 那条修正确定排序字段，再套用此模板",
            sql_kind=TEMPLATE,
            confidence="low",
            caveats=["没有 ORDER BY 的深分页没有确定性语义，必须先补齐排序字段"],
        )
    # 游标字段必须是一个真实字段；ORDER BY 里写的是表达式（如 RAND()）时不能拿来当游标
    cursor_col = None
    if order is not None and isinstance(order.expressions[0].this, exp.Column):
        cursor_col = order.expressions[0].this.name
    if not cursor_col and opts.get("cursor_column"):
        cursor_col = opts["cursor_column"]
    if not cursor_col:
        return Fix(
            rule_id=finding.rule_id,
            problem=finding.message,
            action="改游标分页：需要一个真实排序字段做游标（ORDER BY 里当前不是字段，无法自动确定）",
            sql_kind=NEEDS_METADATA,
            confidence="low",
            caveats=["先修正确定排序字段（补 ORDER BY <字段>），或传入 cursor_column 参数",
                     "没有确定性排序的深分页本身就没有稳定语义"],
        )
    cursor = exp.GT(this=exp.column(cursor_col), expression=exp.var(":last_val"))
    existing = _unwrap(ast.args["where"].this) if ast.args.get("where") else None
    if existing is not None:
        ast.args["where"].this.replace(exp.and_(existing.copy(), cursor))
    else:
        ast.set("where", exp.Where(this=cursor))
    # 游标分页的核心就是把 OFFSET 去掉，留着等于没改
    if ast.args.get("offset") is not None:
        ast.set("offset", None)
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action=f"深分页改游标分页：以 {cursor_col} 作为游标，记住上一页最后一个值",
        fixed_sql=ast.sql(dialect=dialect),
        sql_kind=TEMPLATE,
        confidence="medium",
        composable=True,
        caveats=[
            f"首次查询去掉 :last_val 条件，后续用上一页最后一条的 {cursor_col} 传入",
            "只支持「下一页」，跳页需要额外实现；这也是刻意的设计约束",
            "必须保证该字段上有合适索引",
        ],
    )


def _fix_limit_without_order(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    order_column = opts.get("order_column")
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    if not order_column:
        return Fix(
            rule_id=finding.rule_id,
            problem=finding.message,
            action="补确定性排序：传入 order_column 参数后可直接生成可执行 SQL",
            sql_kind=NEEDS_METADATA,
            confidence="low",
            caveats=["优先用主键或唯一键排序，否则仍可能出现顺序不稳定"],
        )
    ast.set("order", exp.Order(expressions=[exp.Ordered(this=exp.column(order_column))]))
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action=f"补上 ORDER BY {order_column}",
        fixed_sql=ast.sql(dialect=dialect),
        sql_kind=EXECUTABLE,
        confidence="high",
    )


def _fix_random_order(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    table = table_of(ast)
    if not table:
        return None
    pk = _pk_of(ast, dialect, opts.get("pk_column"))
    from_node = _from_of(ast)
    if from_node is None:
        return None
    cols = ", ".join(e.sql(dialect=dialect) for e in ast.expressions) or "*"
    from_txt = from_node.sql(dialect=dialect)
    where_txt = f" WHERE {_unwrap(ast.args['where'].this).sql(dialect=dialect)}" if ast.args.get("where") else ""
    alias = None
    tbl_node = from_node.this
    if isinstance(from_node, exp.Table):
        alias = from_node.alias
    ref = f"{alias}.{pk}" if alias else pk
    limit_n = _literal(ast.args.get("limit")) or 1
    new_sql = (
        f"SELECT {cols} FROM {from_txt} "
        f"JOIN (SELECT CEIL(RAND() * (SELECT MAX({pk}) FROM {table})) AS rid) AS __r "
        f"ON {ref} >= __r.rid{where_txt} ORDER BY {ref} LIMIT {limit_n}"
    )
    probe = parse_statements(new_sql, dialect)
    if not probe or not probe[0].parsable:
        return Fix(
            rule_id=finding.rule_id,
            problem=finding.message,
            action=f"随机取样改写为按主键区间取：ON {ref} >= 随机主键",
            sql_kind=TEMPLATE,
            confidence="low",
            caveats=["自动构造的语句未能通过解析，请按此思路人工改写"],
        )
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action="ORDER BY RAND() 改为「随机主键锚点 + 主键有序 LIMIT」，避免全表排序",
        fixed_sql=new_sql,
        sql_kind=TEMPLATE,
        confidence="medium",
        caveats=[
            "分布并非完全均匀（主键有空洞时会略偏向大 id），多数业务场景可接受",
            f"假设主键为 {pk}，若实际主键不同请传入 pk_column",
            "结果集仍应验证业务可接受性，不要直接替换抽奖/采样等强公平性场景",
        ],
    )


def _fix_or_chain(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    where = ast.args.get("where")
    if where is None or not isinstance(_unwrap(where.this), exp.Or):
        return None
    left = _unwrap(where.this).this
    right = _unwrap(where.this).expression
    a, b = ast.copy(), ast.copy()
    a.set("where", exp.Where(this=left.copy()))
    b.set("where", exp.Where(this=right.copy()))
    union = exp.union(a, b, distinct=False)
    new_sql = union.sql(dialect=dialect)
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action="OR 拆成 UNION ALL，各自走各自的索引后再合并",
        fixed_sql=new_sql,
        sql_kind=TEMPLATE,
        confidence="low",
        caveats=[
            "UNION ALL 不做去重：两个条件都命中的行会出现两次，必须满足业务可接受",
            "上线前务必用 equivalence_check 对比改写前后的行数是否一致",
            "若两侧条件有交集且要求精确去重，这条改写不适用",
        ],
        follow_up_sql=[],
    )


def _fix_dml_without_where(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    table = table_of(ast) or "<table>"
    if opts.get("predicate"):
        try:
            ast.set("where", exp.Where(this=sqlglot_condition(opts["predicate"], dialect)))
            return Fix(
                rule_id=finding.rule_id,
                problem=finding.message,
                action="补上 WHERE 条件后再执行",
                fixed_sql=ast.sql(dialect=dialect),
                sql_kind=EXECUTABLE,
                confidence="high",
                follow_up_sql=[f"SELECT COUNT(*) FROM {table} WHERE {opts['predicate']};"],
                caveats=["先跑上面这条 COUNT，确认影响行数在预期内再执行"],
            )
        except Exception:
            return None
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action="先数清楚会波及多少行，再决定要不要加条件或改分批执行",
        sql_kind=NEEDS_METADATA,
        confidence="medium",
        follow_up_sql=[
            f"SELECT COUNT(*) FROM {table};",
            f"-- 分批方案示例（每批 1000，sleep 由应用层控制）\n"
            f"DELETE FROM {table} WHERE <条件> LIMIT 1000;",
        ],
        caveats=[
            "缺条件的 DML 在生产环境一律禁止直接执行",
            "确需全表操作时，改为限定主键范围的分批循环，并保留回滚数据",
        ],
    )


def _fix_join_without_on(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    tables: list[str] = []
    for t in ast.find_all(exp.Table):
        tables.append(t.name)
    uniq = list(dict.fromkeys(tables))
    fk_sql = (
        "SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
        "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE() "
        f"AND (TABLE_NAME IN ({','.join(chr(39)+t+chr(39) for t in uniq)})) AND REFERENCED_TABLE_NAME IS NOT NULL;"
        if dialect == "mysql"
        else "SELECT conname, conrelid::regclass AS table_name, a.attname AS column_name, "
             "confrelid::regclass AS referenced_table, af.attname AS referenced_column "
             "FROM pg_constraint c JOIN pg_attribute a ON a.attnum = ANY(c.conkey) AND a.attrelid = c.conrelid "
             "JOIN pg_attribute af ON af.attnum = ANY(c.confkey) AND af.attrelid = c.confrelid "
             "WHERE c.contype = 'f';"
    )
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action="笛卡尔积无法自动修复（工具不知道你的关联语义）。先查出这两张表之间的外键/候选关联键",
        sql_kind=NEEDS_METADATA,
        confidence="low",
        follow_up_sql=[fk_sql, f"-- 确认结果体量后再决定是否真的是笛卡尔积：\nSELECT COUNT(*) 预估应改为分表 COUNT 相乘"],
        caveats=["若业务确实需要笛卡尔积，请显式写 CROSS JOIN 并确认结果集规模可接受"],
    )


def _fix_insert_columns(stmt_sql: str, dialect: str, finding: Finding, opts: dict) -> Fix | None:
    cols = opts.get("columns") or []
    stmts = parse_statements(stmt_sql, dialect)
    if not stmts or not stmts[0].parsable:
        return None
    ast = stmts[0].ast
    table = table_of(ast)
    if not cols:
        return Fix(
            rule_id=finding.rule_id,
            problem=finding.message,
            action="INSERT 补字段名：传入 columns 参数可直接生成可执行 SQL",
            sql_kind=NEEDS_METADATA,
            confidence="low",
            follow_up_sql=[
                f"SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                f"AND TABLE_NAME = '{table}' ORDER BY ORDINAL_POSITION;"
                if dialect == "mysql"
                else f"SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                     f"AND table_name='{table}' ORDER BY ordinal_position;"
            ],
        )
    ast.set("this", exp.Schema(this=ast.this.copy(), expressions=[exp.column(c) for c in cols]))
    return Fix(
        rule_id=finding.rule_id,
        problem=finding.message,
        action="INSERT 指明字段名",
        fixed_sql=ast.sql(dialect=dialect),
        sql_kind=EXECUTABLE,
        confidence="high",
    )


def sqlglot_condition(predicate: str, dialect: str) -> exp.Expression:
    from sqlglot import parse_one

    return parse_one(f"SELECT 1 WHERE {predicate}", read=dialect).args["where"].this


def _literal(node: exp.Expression | None) -> int | None:
    if node is None:
        return None
    for holder in (getattr(node, "expression", None), getattr(node, "this", None)):
        val = getattr(holder, "this", None)
        if val is not None:
            try:
                return int(str(val))
            except (TypeError, ValueError):
                return None
    return None


FIXERS = {
    "SEL001": _fix_select_star,
    "SEL002": _fix_leading_like,
    "SEL003": _fix_date_range,
    "SEL003_YEAR": _fix_year_range,
    "SEL003_SUBSTR": _fix_substr_prefix,
    "SEL004": _fix_random_order,
    "SEL005": _fix_deep_offset,
    "SEL006": _fix_limit_without_order,
    "SEL007": _fix_join_without_on,
    "SEL008": _fix_or_chain,
    "DML001": _fix_dml_without_where,
    "INS001": _fix_insert_columns,
}


def suggest_fixes(sql: str, dialect: str = "mysql", **opts: Any) -> dict:
    """给一段 SQL 的每一个问题给出修法 + 验证办法，并对改写结果重新体检闭环。

    多条修正会按顺序叠加：可执行(executable)的改法会作用到下一条的输入上，
    最终给出一条把所有能自动改的问题都改掉的 composed_sql，避免用户手动拼装。
    """
    d = normalize_dialect(dialect)
    review = review_sql(sql, d, deep_offset_threshold=opts.get("deep_offset_threshold", 1000))
    stmts = parse_statements(sql, d)
    working_sql = stmts[0].sql if stmts else sql

    def _maybe_compose(made: Fix | None) -> None:
        """可执行的改写、以及标注为可叠加的模板，都会成为下一条修正的输入。"""
        nonlocal working_sql
        if made is not None and made.fixed_sql and (made.sql_kind == EXECUTABLE or made.composable):
            working_sql = made.fixed_sql

    fixes: list[Fix] = []
    for f in review.findings:
        if f.rule_id == "SEL003":
            # 字段上套函数的形态很多，逐个尝试可安全改写的几种，都不匹配时降级为人工指引
            made = None
            for rid in ("SEL003", "SEL003_YEAR", "SEL003_SUBSTR"):
                made = FIXERS[rid](working_sql, d, f, opts)
                if made is not None:
                    break
            if made is None:
                made = Fix(
                    rule_id=f.rule_id,
                    problem=f.message,
                    action="把函数从字段上挪走（改成常量侧计算）或建立函数索引/表达式索引",
                    sql_kind=NEEDS_METADATA,
                    confidence="low",
                    side_ddl=[f"-- MySQL 8 / PG：CREATE INDEX idx_expr ON <table> ((你用到的表达式));"],
                    caveats=["该形态暂不支持自动改写，请按 action 手动调整"],
                )
            fixes.append(made)
            _maybe_compose(made)
            continue

        fixer = FIXERS.get(f.rule_id)
        if fixer is None:
            fixes.append(
                Fix(rule_id=f.rule_id, problem=f.message, action=f.suggestion, sql_kind=MANUAL, confidence="low")
            )
            continue
        try:
            made = fixer(working_sql, d, f, opts)
        except Exception as exc:  # 单个改写失败不影响其它规则
            made = Fix(
                rule_id=f.rule_id, problem=f.message, action=f"自动改写失败：{type(exc).__name__}",
                sql_kind=MANUAL, confidence="low",
            )
        if made is not None:
            fixes.append(made)
        _maybe_compose(made)

    # 闭环验证：把改写后的 SQL 重新体检一遍
    for fix in fixes:
        if fix.fixed_sql and fix.sql_kind != MANUAL:
            again = review_sql(fix.fixed_sql, d)
            still = [x.rule_id for x in again.findings if x.rule_id == fix.rule_id]
            fix.recheck = {
                "rule_resolved": not still,
                "verdict_after": again.verdict,
                "remaining_rules": sorted({x.rule_id for x in again.findings}),
            }
            if still and fix.confidence == "high":
                fix.confidence = "medium"

    composed = working_sql != (stmts[0].sql if stmts else sql)
    final_review = review_sql(working_sql, d) if composed else None

    return {
        "dialect": d,
        "verdict_before": review.verdict,
        "composed_sql": working_sql if composed else None,
        "verdict_after_all": final_review.verdict if final_review else review.verdict,
        "resolved_all": sorted({f.rule_id for f in review.findings} - {f.rule_id for f in (final_review.findings if final_review else review.findings)}),
        "remaining_all": sorted({f.rule_id for f in (final_review.findings if final_review else review.findings)}),
        "items": [f.to_dict() for f in fixes],
        "actionable": sum(1 for f in fixes if f.fixed_sql),
        "total": len(fixes),
    }
