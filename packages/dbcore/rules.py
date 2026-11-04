"""SQL 静态体检规则。

只做静态分析 —— 不需要连库、零风险、可被 CI 直接调用。
每条规则产出 Finding(rule_id, severity, line, message, suggestion)。
severity: blocker > high > medium > low > info
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

from sqlglot import exp

from dbcore.dialect import line_of, normalize_dialect, parse_statements, table_of

SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class Finding:
    rule_id: str
    severity: str
    message: str
    suggestion: str
    line: int | None = None
    table: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewResult:
    dialect: str
    statement_count: int = 0
    unparsable: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def max_severity(self) -> str | None:
        if not self.findings:
            return None
        return sorted(self.findings, key=lambda f: SEVERITY_ORDER[f.severity])[0].severity

    @property
    def verdict(self) -> str:
        sev = self.max_severity
        return {"blocker": "禁止上线", "high": "强烈建议修改", "medium": "建议修改", "low": "可上线", "info": "通过", None: "通过"}[  # type: ignore[index]
            sev
        ]

    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "statement_count": self.statement_count,
            "verdict": self.verdict,
            "max_severity": self.max_severity,
            "summary": {s: sum(1 for f in self.findings if f.severity == s) for s in SEVERITY_ORDER},
            "unparsable": self.unparsable,
            "findings": [f.to_dict() for f in self.findings],
        }


RuleFn = Callable[[object, dict], list[Finding]]


def _mk(rule_id: str, severity: str, message: str, suggestion: str, node: exp.Expression | None, table: str | None = None) -> Finding:
    return Finding(rule_id=rule_id, severity=severity, message=message, suggestion=suggestion, line=line_of(node), table=table)


# --------------------------------------------------------------------------- 规则


def rule_dml_without_where(ast, ctx) -> list[Finding]:
    if not isinstance(ast, (exp.Update, exp.Delete)):
        return []
    if ast.args.get("where") is None:
        return [
            _mk(
                "DML001",
                "blocker",
                f"{type(ast).__name__.upper()} 缺少 WHERE 条件，会作用于全表",
                "补齐 WHERE；确需全表操作时改为分批 LIMIT 执行并预留回滚数据",
                ast,
                table_of(ast),
            )
        ]
    return []


def rule_select_star(ast, ctx) -> list[Finding]:
    stars = list(ast.find_all(exp.Star)) if isinstance(ast, exp.Select) else []
    return [
        _mk("SEL001", "low", "使用了 SELECT *", "显式列出字段：减少网络/内存开销，避免表结构变更导致的隐式破坏", s, table_of(ast))
        for s in stars[:1]
    ]


def rule_leading_wildcard_like(ast, ctx) -> list[Finding]:
    out = []
    for like in ast.find_all(exp.Like):
        pattern = like.args.get("expression")
        val = getattr(pattern, "this", None)
        if isinstance(val, str) and val.startswith(("%", "_")):
            out.append(
                _mk(
                    "SEL002",
                    "medium",
                    "LIKE 以通配符开头，普通索引无法用于前缀匹配，必然全表扫描",
                    "改为后缀匹配、接入倒排索引/全文检索，或增加生成列 + 反向索引",
                    like,
                    table_of(ast),
                )
            )
    return out


def rule_function_on_column(ast, ctx) -> list[Finding]:
    out = []
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return out
    # sqlglot 里 AND/OR/NOT 这类 Connector 也是 Func 的子类，必须先排除，否则整句都被误判
    logical = (exp.Connector, exp.Predicate, exp.Binary)
    seen: set[str] = set()
    for node in where.walk():
        if isinstance(node, logical):
            continue
        if isinstance(node, exp.Func) and not isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            cols = [c.name for c in node.find_all(exp.Column)]
            for c in cols:
                if c in seen:
                    continue
                seen.add(c)
                out.append(
                    _mk(
                        "SEL003",
                        "medium",
                        f"WHERE 中对字段 {c} 施加了函数 {node.key.upper()}()，该字段上的索引将失效",
                        "改写成字段裸列的范围条件（把函数移到常量侧），或建立函数索引/表达式索引",
                        node,
                        table_of(ast),
                    )
                )
    return out


def rule_random_order(ast, ctx) -> list[Finding]:
    order = ast.args.get("order") if isinstance(ast, exp.Select) else None
    if order is None:
        return []
    if any(isinstance(n, exp.Rand) for n in order.walk()):
        return [
            _mk(
                "SEL004",
                "medium",
                "ORDER BY RAND()/RANDOM() 需要为全表结果集排序",
                "改用主键区间随机取样或业务层抽样，避免随数据量劣化",
                order,
                table_of(ast),
            )
        ]
    return []


def _literal_of(node: exp.Expression | None) -> int | None:
    """兼容 sqlglot 各版本的取值：容器节点的 this / expression 都试一遍。"""
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


def rule_deep_offset(ast, ctx) -> list[Finding]:
    if not isinstance(ast, exp.Select):
        return []
    if ast.args.get("limit") is None:
        return []
    n = _literal_of(ast.args.get("offset"))
    if n is None or n < int(ctx.get("deep_offset_threshold", 1000)):
        return []
    return [
        _mk(
            "SEL005",
            "medium",
            f"深度分页 OFFSET {n}，数据库需扫描并丢弃前 {n} 行后才返回结果",
            "改为游标分页（WHERE id > last_id ORDER BY id LIMIT n），或限制最大翻页深度",
            ast.args.get("limit"),
            table_of(ast),
        )
    ]


def rule_limit_without_orderby(ast, ctx) -> list[Finding]:
    if not isinstance(ast, exp.Select):
        return []
    if ast.args.get("limit") is not None and ast.args.get("order") is None:
        return [
            _mk(
                "SEL006",
                "low",
                "LIMIT 未配合 ORDER BY，返回结果集顺序不稳定",
                "加上确定性排序，否则分页会出现重复/漏数据",
                ast,
                table_of(ast),
            )
        ]
    return []


def rule_join_without_on(ast, ctx) -> list[Finding]:
    out = []
    for join in ast.find_all(exp.Join):
        if join.args.get("on") is None and join.args.get("using") is None:
            out.append(
                _mk(
                    "SEL007",
                    "high",
                    "JOIN 缺少关联条件，会产生笛卡尔积",
                    "补 ON/USING 条件；确需笛卡尔积请显式写 CROSS JOIN 并确认结果集体量",
                    join,
                    table_of(ast),
                )
            )
    return out


def rule_or_chain(ast, ctx) -> list[Finding]:
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return []
    if any(isinstance(n, exp.Or) for n in where.walk()):
        return [
            _mk(
                "SEL008",
                "low",
                "WHERE 中存在 OR，优化器常常放弃索引而走全表扫描",
                "拆成 UNION ALL 两条走索引的查询，或评估是否满足索引合并条件",
                where,
                table_of(ast),
            )
        ]
    return []


def rule_insert_without_columns(ast, ctx) -> list[Finding]:
    if isinstance(ast, exp.Insert):
        target = ast.this
        if target is not None and target.expressions == []:
            return [
                _mk(
                    "INS001",
                    "low",
                    "INSERT 未显式指定列列表，依赖表字段顺序",
                    "写明列名，否则任何一次加字段/改字段顺序都会让这条 SQL 写错位置",
                    ast,
                    table_of(ast),
                )
            ]
    return []


def rule_ddl_in_app_sql(ast, ctx) -> list[Finding]:
    if isinstance(ast, (exp.Create, exp.Alter, exp.Drop)):
        return [
            _mk(
                "DDL000",
                "info",
                "检测到 DDL 语句，请走变更管控流程并做风险评估",
                "调用 change-risk Agent 做锁级别、影响面与回滚方案评估后再执行",
                None,
                table_of(ast) if hasattr(ast, "find") else None,
            )
        ]
    return []


RULES: list[tuple[str, RuleFn]] = [
    (r.__name__, r)
    for r in (
        rule_dml_without_where,
        rule_select_star,
        rule_leading_wildcard_like,
        rule_function_on_column,
        rule_random_order,
        rule_deep_offset,
        rule_limit_without_orderby,
        rule_join_without_on,
        rule_or_chain,
        rule_insert_without_columns,
        rule_ddl_in_app_sql,
    )
]


def review_sql(sql: str, dialect: str | None = None, **opts) -> ReviewResult:
    """对一段 SQL（可多句）做静态体检。"""
    d = normalize_dialect(dialect)
    result = ReviewResult(dialect=d)
    stmts = parse_statements(sql, d)
    result.statement_count = len(stmts)

    for stmt in stmts:
        if not stmt.parsable:
            result.unparsable.append(stmt.sql[:200])
            result.findings.append(
                Finding(
                    rule_id="PARSE000",
                    severity="info",
                    message="该语句无法被解析器识别，跳过静态检查",
                    suggestion="人工复核；若确为合法方言写法，请提 issue 补充方言支持",
                )
            )
            continue
        for _name, fn in RULES:
            result.findings.extend(fn(stmt.ast, opts))

    result.findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return result
