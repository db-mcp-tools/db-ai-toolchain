"""SQL 静态体检规则。

只做静态分析 —— 不需要连库、零风险、可被 CI 直接调用。
每条规则产出 Finding(rule_id, severity, line, message, suggestion)。
severity: blocker > high > medium > low > info
"""

from __future__ import annotations

import re
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




# ==================================================================
# 规则元数据 —— list_rules / explain_rule 的数据来源
# 每条规则必须讲清：为什么禁、什么情况会误报、怎么改
# ==================================================================

RULE_META: dict[str, dict] = {
    "DML001": {"name": "写操作缺少 WHERE", "category": "dml", "severity": "blocker",
               "why": "UPDATE/DELETE 没有 WHERE 会作用于整张表，误操作即为数据事故",
               "false_positive": "TRUNCATE 意图的重命名类操作；但即便如此也应显式确认"},
    "SEL001": {"name": "SELECT *", "category": "select", "severity": "low",
               "why": "列变更导致结果不稳定、无法用覆盖索引、网络传输放大",
               "false_positive": "临时排查用的 ad-hoc 查询、ETL 全量同步可以豁免"},
    "SEL002": {"name": "前导通配符 LIKE", "category": "select", "severity": "high",
               "why": "LIKE '%x' 无法使用 B+Tree 索引前缀匹配，必然全表扫描",
               "false_positive": "数据量极小（几千行）的字典表可接受"},
    "SEL003": {"name": "字段上套函数", "category": "select", "severity": "high",
               "why": "WHERE DATE(col)='...' 使 col 上的索引完全失效",
               "false_positive": "如果本来就没有该列的索引，这条规则只是潜在建议"},
    "SEL004": {"name": "ORDER BY RAND()", "category": "select", "severity": "high",
               "why": "为每行计算随机值后排序，需要全量扫描 + 排序，行数一上去必然慢",
               "false_positive": "结果集本身很小且已过滤到几十行内"},
    "SEL005": {"name": "深度分页", "category": "select", "severity": "medium",
               "why": "OFFSET n 需要先扫描并丢弃前 n 行，成本随页码线性增长",
               "false_positive": "后台导出任务可接受"},
    "SEL006": {"name": "LIMIT 无 ORDER BY", "category": "select", "severity": "medium",
               "why": "没有排序保证时，每次返回的行不确定，翻页会重复或丢数据",
               "false_positive": "只取一行做存在性判断"},
    "JOIN001": {"name": "逗号隐式连接", "category": "join", "severity": "medium",
                "why": "漏写连接条件会静默变成笛卡尔积",
                "false_positive": "确实是 CROSS JOIN 的场景应显式写 CROSS JOIN"},
    "JOIN002": {"name": "笛卡尔积", "category": "join", "severity": "blocker",
                "why": "多表无连接条件 = 行数相乘，是生产事故级别的问题",
                "false_positive": "极少数需要生成组合的 dim×dim 场景"},
    "JOIN003": {"name": "JOIN 层数过多", "category": "join", "severity": "medium",
                "why": "超过 6~7 张表时优化器搜索空间不足，容易选错驱动顺序",
                "false_positive": "每张表都极小且索引完备时未必有问题"},
    "TYPE001": {"name": "隐式类型转换", "category": "type", "severity": "high",
                "why": "数值列与字符串比较，数据库会对列做 CAST，索引直接失效",
                "false_positive": "列本身是字符类型且存的就是数字串（设计问题，应改类型）"},
    "TYPE002": {"name": "否定条件", "category": "type", "severity": "low",
                "why": "!= / NOT IN 的选择性通常很差，索引收益有限",
                "false_positive": "列只有极少数值且分布极度倾斜时"},
    "NULL001": {"name": "NULL 语义陷阱", "category": "semantic", "severity": "medium",
                "why": "col <> 'x' 不会匹配 col IS NULL 的行，导致数据静默丢失",
                "false_positive": "明确知道该列 NOT NULL 时可忽略"},
    "AGG001": {"name": "COUNT(列) 的 NULL 语义", "category": "aggregate", "severity": "low",
               "why": "COUNT(col) 不统计 NULL，与 COUNT(*) 结果不同，常被误当行数",
               "false_positive": "就是要统计非空值数量"},
    "AGG002": {"name": "大结果集无 LIMIT", "category": "aggregate", "severity": "medium",
               "why": "多表查询全量返回会打爆应用内存与网络",
               "false_positive": "导出任务、批处理可接受"},
    "AGG003": {"name": "DISTINCT 与 GROUP BY 同时使用", "category": "aggregate", "severity": "low",
               "why": "GROUP BY 已去重，DISTINCT 是纯浪费",
               "false_positive": "无"},
    "AGG004": {"name": "UNION 而非 UNION ALL", "category": "aggregate", "severity": "low",
               "why": "UNION 额外做一次去重排序，两段结果不重叠时白付代价",
               "false_positive": "确实需要去重"},
    "SUBQ001": {"name": "SELECT 列表中的子查询", "category": "subquery", "severity": "high",
                "why": "相关子查询每行执行一次，复杂度随结果行数线性增长",
                "false_positive": "子查询已是非相关子查询（注意 DEPENDENT 标记）"},
    "SUBQ002": {"name": "IN (子查询)", "category": "subquery", "severity": "medium",
                "why": "MySQL 可能优化为 DEPENDENT SUBQUERY，性能极差",
                "false_positive": "EXPLAIN 显示 MATERIALIZED 或 8.0 的 semi-join 优化生效时"},
    "DML002": {"name": "批量写操作无分批", "category": "dml", "severity": "medium",
               "why": "单次影响行数不可控，产生大事务：长锁、undo 膨胀、主从延迟",
               "false_positive": "已确认影响行数很少"},
    "DML003": {"name": "INSERT 缺少列清单", "category": "dml", "severity": "medium",
               "why": "表结构变更后 INSERT 会错位或失败",
               "false_positive": "无"},
    "DML004": {"name": "REPLACE INTO", "category": "dml", "severity": "medium",
               "why": "先删后插：改变自增 ID、双份 binlog、未指定列被重置",
               "false_positive": "确实就是想整行替换（极少见）"},
    "DML005": {"name": "INSERT IGNORE", "category": "dml", "severity": "low",
               "why": "静默吞掉所有错误，包括本应暴露的数据截断",
               "false_positive": "明确告知这是一种容错策略时"},
    "DDL001": {"name": "应用 SQL 中出现 DDL", "category": "ddl", "severity": "high",
               "why": "DDL 不应随业务代码发布，需要独立的变更流程与回滚方案",
               "false_positive": "迁移脚本/初始化脚本（应走另外的通道）"},
    "DDL004": {"name": "TRUNCATE", "category": "ddl", "severity": "blocker",
               "why": "不可回滚、不记录明细行、重置自增计数器",
               "false_positive": "临时表/中间表可接受"},
    "DDL005": {"name": "DROP", "category": "ddl", "severity": "blocker",
               "why": "不可回滚，且会连带影响依赖它的对象",
               "false_positive": "临时对象可接受"},
    "DDL006": {"name": "修改列类型", "category": "ddl", "severity": "high",
               "why": "重建整表并长时持锁，可能截断数据",
               "false_positive": "空表或小表"},
    "ORDER001": {"name": "排序键不唯一的分页", "category": "select", "severity": "medium",
                 "why": "排序不稳定导致翻页重复/丢行，用户能直接感知",
                 "false_positive": "排序键本身是唯一键"},
    "PART001": {"name": "分区表未命中分区键", "category": "partition", "severity": "high",
                "why": "分区裁剪失效时会扫描所有分区，比不分区更慢",
                "false_positive": "确实需要跨分区聚合"},
    "LOCK003": {"name": "FOR UPDATE 缺等值条件", "category": "lock", "severity": "high",
                "why": "会锁住扫描过的所有行，等价于锁表",
                "false_positive": "需要锁全表的批处理且业务可接受"},
    "TX002": {"name": "应用层事务控制语句", "category": "transaction", "severity": "low",
              "why": "事务边界应由连接池/框架管理，混入业务 SQL 会导致连接归还时状态不确定",
              "false_positive": "脚本批量执行"},
    "TX003": {"name": "关闭 autocommit", "category": "transaction", "severity": "high",
              "why": "异常退出时留下长事务，长期持有锁并阻塞 vacuum/DDL",
              "false_positive": "显式管理的批处理脚本"},
    "PG001": {"name": "PG 建索引未用 CONCURRENTLY", "category": "postgres", "severity": "high",
              "why": "默认 CREATE INDEX 持 SHARE 锁，期间无法写入",
              "false_positive": "建索引的同时该表本来就不允许写入"},
    "PG002": {"name": "VACUUM FULL", "category": "postgres", "severity": "high",
              "why": "ACCESS EXCLUSIVE 锁重建整表，期间完全不可读写",
              "false_positive": "维护窗口内的小表"},
    "MY001": {"name": "MySQL ALTER 未指定 ALGORITHM", "category": "mysql", "severity": "medium",
              "why": "不同版本默认行为差异大，5.6 上很多操作会 COPY 整表",
              "false_positive": "8.0 且确认支持 INSTANT"},
    "PARSE000": {"name": "语句无法解析", "category": "parse", "severity": "info",
                 "why": "方言写法不在 sqlglot 覆盖范围内，静态检查已跳过",
                 "false_positive": "本来就是需要人工复核的场景"},
}


def list_rules(category: str | None = None, severity: str | None = None,
               dialect: str | None = None) -> list[dict]:
    out = []
    for rid, meta in RULE_META.items():
        if category and meta["category"] != category:
            continue
        if severity and meta["severity"] != severity:
            continue
        if dialect:
            cat = meta["category"]
            if cat in ("postgres", "gaussdb", "kingbase") and dialect not in ("postgres", "gaussdb", "kingbase"):
                continue
            if cat == "mysql" and dialect not in ("mysql", "mariadb", "oceanbase", "tidb"):
                continue
        out.append({"rule_id": rid, **meta})
    return sorted(out, key=lambda r: (SEVERITY_ORDER[r["severity"]], r["rule_id"]))


def explain_rule(rule_id: str) -> dict | None:
    meta = RULE_META.get(rule_id.upper())
    if not meta:
        return None
    return {"rule_id": rule_id.upper(), **meta}


def rule_categories() -> list[str]:
    return sorted({m["category"] for m in RULE_META.values()})


# ==================================================================
# 第二批规则：谓词 / 类型 / 聚合 / JOIN / 分区 / 引擎专属
# 这批规则解决"只查 SELECT * 和缺索引"那种浅层体检覆盖不到的问题
# ==================================================================


def rule_implicit_join(ast, ctx) -> list[Finding]:
    """逗号连接 / 无 ON 的 JOIN：一不小心就变成笛卡尔积。

    判定依据是"多数据源 + 没有显式 ON/USING"。
    即使 WHERE 里有关联条件（语义等价），也仍然提示改写 —— 但级别降为 low。
    """
    if not isinstance(ast, exp.Select):
        return []
    joins = list(ast.find_all(exp.Join))
    if not joins:
        return []
    has_on = any(j.args.get("on") is not None or j.args.get("using") is not None for j in joins)
    sources = len(list(ast.find_all(exp.Table)))
    if has_on or sources < 2:
        return []
    severity = "low" if _eq_pairs(ast) else "medium"
    return [_mk("JOIN001", severity,
                f"使用了隐式连接（{sources} 张数据源且没有显式 ON），缺少关联条件时会产生笛卡尔积",
                "改写为显式 JOIN ... ON，让连接条件不可能被漏写", ast, table_of(ast))]


def rule_join_without_on(ast, ctx) -> list[Finding]:
    """显式 JOIN 缺 ON/USING 且没有任何跨表约束 → 笛卡尔积（block 级）。

    注意：如果 WHERE 里存在跨表等值条件，语义上等价于 JOIN ON，不在这里重复报，
    由 JOIN001 以低级别提示改写风格即可。
    """
    out = []
    has_cross_cond = bool(_eq_pairs(ast))
    where = ast.args.get("where")
    has_semi = False
    if where is not None:
        has_semi = any(isinstance(n, (exp.In, exp.Exists)) and n.find(exp.Subquery) is not None
                       for n in where.walk())
    for join in ast.find_all(exp.Join):
        if join.args.get("on") is not None or join.args.get("using") is not None:
            continue
        explicit = bool(join.args.get("kind")) or bool(join.args.get("side"))
        if not explicit and (has_cross_cond or has_semi):
            continue                       # 逗号写法 + WHERE 里有关联条件 = 合法的旧风格
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


def rule_cartesian(ast, ctx) -> list[Finding]:
    """多表 JOIN 但没有任何跨表关联条件 → 笛卡尔积。"""
    if not isinstance(ast, exp.Select):
        return []
    # 子查询/派生表内部的表与本层无 JOIN 关系，不能算进来
    top_tables: list[str] = []
    for t in ast.find_all(exp.Table):
        in_sub = False
        parent = t.parent
        while parent is not None:
            if isinstance(parent, exp.Subquery):
                in_sub = True
                break
            parent = parent.parent
        if not in_sub:
            top_tables.append(t.name)
    tables = top_tables
    if len(tables) < 2:
        return []
    cond_cols: set[str] = set()
    for j in ast.find_all(exp.Join):
        on = j.args.get("on")
        if on is not None:
            for c in on.find_all(exp.Column):
                cond_cols.add(c.name)
    has_cross = bool(_eq_pairs(ast))
    # IN / EXISTS 等半连接也是合法的跨表约束
    where = ast.args.get("where")
    has_semi_join = False
    if where is not None:
        for node in where.walk():
            if isinstance(node, (exp.In, exp.Exists)) and node.find(exp.Subquery) is not None:
                has_semi_join = True
    if cond_cols or has_cross or has_semi_join:
        return []
    return [_mk("JOIN002", "blocker",
                f"查询涉及 {len(tables)} 张表（{'、'.join(tables[:5])}）但没有任何跨表关联条件，结果是笛卡尔积",
                "补充 JOIN 条件。这种语句一旦上生产，行数 = 各表行数乘积，必然拖垮实例",
                ast, table_of(ast))]


def _eq_pairs(ast) -> list[list[str]]:
    out = []
    for eq in ast.find_all(exp.EQ):
        pair = []
        for side in (eq.this, eq.expression):
            c = side.find(exp.Column) if side is not None else None
            if isinstance(c, exp.Column):
                pair.append(c.table or "")
        if len(pair) == 2 and pair[0] and pair[1] and pair[0] != pair[1]:
            out.append(pair)
    return out


def rule_join_depth(ast, ctx) -> list[Finding]:
    joins = list(ast.find_all(exp.Join)) if isinstance(ast, exp.Select) else []
    if len(joins) > 6:
        return [_mk("JOIN003", "medium",
                    f"单条语句包含 {len(joins)} 个 JOIN，超过优化器可靠工作的经验范围",
                    "拆分为多个步骤并用临时表/CTE 承接中间结果；同时确认每个 JOIN 列都有索引",
                    ast, table_of(ast))]
    return []


_NUMERIC_COL_RE = re.compile(r"(^|_)(id|no|num|count|qty|amount|total|seq|score|coin|codes?)$", re.I)


def rule_implicit_cast(ast, ctx) -> list[Finding]:
    """数字型字段与字符串字面量比较 —— 会导致整列隐式转换，索引失效。"""
    out: list[Finding] = []
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


def rule_negation(ast, ctx) -> list[Finding]:
    out: list[Finding] = []
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
                out.append(_mk("TYPE002", "medium",
                               "使用了 NOT IN：优化器通常无法走索引，且子查询返回 NULL 时结果为空集",
                               "改写为 NOT EXISTS，并显式处理 NULL 语义", node, table_of(ast)))
                break
    return out


def rule_not_null_safe(ast, ctx) -> list[Finding]:
    """NULL 语义陷阱：col <> 'x' 会漏掉 col IS NULL 的行。"""
    out: list[Finding] = []
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


def rule_count_star_vs_col(ast, ctx) -> list[Finding]:
    """聚合函数语义差异。"""
    if not isinstance(ast, exp.Select):
        return []
    out = []
    for f in ast.find_all(exp.Count):
        inner = f.this
        if isinstance(inner, exp.Column):
            out.append(_mk("AGG001", "low",
                           f"使用了 COUNT({inner.name})：该写法不统计 NULL 值",
                           "确认业务意图：统计行数用 COUNT(*)，统计非空值才用 COUNT(col)",
                           f, table_of(ast)))
    return out[:1]


def rule_distinct_and_groupby(ast, ctx) -> list[Finding]:
    if not isinstance(ast, exp.Select):
        return []
    if ast.args.get("distinct") and ast.args.get("group"):
        return [_mk("AGG003", "low",
                    "同时使用 DISTINCT 与 GROUP BY：GROUP BY 本身就去重，DISTINCT 是多余的开销",
                    "去掉 DISTINCT", ast, table_of(ast))]
    return []


def rule_big_result_no_limit(ast, ctx) -> list[Finding]:
    """大结果集无 LIMIT —— 应用侧最常见的内存杀手。"""
    if not isinstance(ast, exp.Select):
        return []
    if ast.args.get("limit") is not None:
        return []
    has_agg = any(isinstance(f, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max))
                  for f in ast.find_all(exp.Func))
    if has_agg:
        return []
    joins = len(list(ast.find_all(exp.Join)))
    if joins < 2:
        return []
    return [_mk("AGG002", "medium",
                f"多表查询（{joins + 1} 张表）既没有聚合也没有 LIMIT，可能返回海量结果",
                "确认是否需要全量：翻页用游标分页，导出走专用通道，不要在应用请求里全表拉取",
                ast, table_of(ast))]


def rule_select_subquery(ast, ctx) -> list[Finding]:
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


def rule_batch_dml(ast, ctx) -> list[Finding]:
    """批量 UPDATE/DELETE 没有 LIMIT —— 大事务风险。"""
    if not isinstance(ast, (exp.Update, exp.Delete)):
        return []
    if ast.args.get("limit") is not None:
        return []
    where = ast.args.get("where")
    if where is None:
        return []                                       # 无 WHERE 由 DML001 拦截，避免重复报
    return [_mk("DML002", "medium",
                f"批量 {type(ast).__name__.upper()} 没有 LIMIT：影响行数不可控，"
                f"可能产生大事务（长锁持有、undo 膨胀、主从延迟）",
                "改为分批执行：每批 <= 5000 行，批间 sleep 50~200ms，且必须能安全重试",
                ast, table_of(ast))]


def rule_replace_into(ast, ctx) -> list[Finding]:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bREPLACE\s+INTO\b", sql_text, re.I):
        return [_mk("DML004", "medium",
                    "REPLACE INTO 是先删后插：会改变自增 ID、触发两次 binlog、"
                    "并且在没有完整 WHERE 覆盖时会误删未指定的列",
                    "改用 INSERT ... ON DUPLICATE KEY UPDATE（MySQL）或 "
                    "INSERT ... ON CONFLICT DO UPDATE（PG）", ast, table_of(ast))]
    if re.search(r"\bINSERT\s+IGNORE\b", sql_text, re.I):
        return [_mk("DML005", "low",
                    "INSERT IGNORE 会静默吞掉所有错误（不只是重复键），包括数据截断",
                    "改用显式去重 + 普通 INSERT，让真正的错误暴露出来", ast, table_of(ast))]
    return []


def rule_destructive_ddl(ast, ctx) -> list[Finding]:
    sql_text = ctx.get("_sql_text", "") or ""
    out = []
    if re.search(r"\bTRUNCATE\b", sql_text, re.I):
        out.append(_mk("DDL004", "blocker",
                       "TRUNCATE 不可回滚、不走 binlog row 模式的历史明细、会重置自增计数器",
                       "确认已有可用备份；批量清除数据优先用分批 DELETE", ast, table_of(ast)))
    if re.search(r"\bDROP\s+(TABLE|DATABASE)\b", sql_text, re.I):
        out.append(_mk("DDL005", "blocker",
                       "DROP 操作不可回滚且会连带删除依赖它的视图/存储过程",
                       "上线前必须有备份与回滚验证；建议先改名观察一段时间再真正删除",
                       ast, table_of(ast)))
    if re.search(r"\bMODIFY\s+(COLUMN)?\b|\bCHANGE\s+COLUMN\b|\bALTER\s+COLUMN\b.*TYPE\b", sql_text, re.I):
        out.append(_mk("DDL006", "high",
                       "修改列类型会重建整张表并长时间持锁，且可能导致数据截断",
                       "用在线改表工具；先在新列上双写验证，再切换", ast, table_of(ast)))
    return out


def rule_unstable_order_limit(ast, ctx) -> list[Finding]:
    """ORDER BY 的排序键不唯一 → 分页会重复/丢行。"""
    if not isinstance(ast, exp.Select):
        return []
    if ast.args.get("limit") is None or ast.args.get("order") is None:
        return []
    order = ast.args["order"]
    cols = []
    for e in order.expressions:
        this = e.this if isinstance(e, exp.Ordered) else e
        if isinstance(this, exp.Column):
            cols.append(this.name)
    if not cols:
        return []
    if "id" in [c.lower() for c in cols]:
        return []
    return [_mk("ORDER001", "medium",
                f"ORDER BY {'、'.join(cols)} 存在 LIMIT：若排序键不唯一，"
                f"相邻页之间会出现重复行或丢失行",
                "在排序键后追加唯一列（通常是主键）保证排序稳定，例如 ORDER BY created_at, id",
                ast, table_of(ast))]


def rule_partition_key_missing(ast, ctx) -> list[Finding]:
    """分区表未带分区键过滤 → 扫描所有分区。"""
    partition_keys = ctx.get("partition_keys") or {}
    if not partition_keys:
        return []
    out = []
    primary = table_of(ast)
    key = partition_keys.get(primary)
    if not key:
        return out
    where = ast.args.get("where")
    used = {c.name for c in (where.find_all(exp.Column) if where else [])}
    if key not in used:
        out.append(_mk("PART001", "high",
                       f"表 `{primary}` 的分区键 `{key}` 未出现在过滤条件中，查询会扫描全部分区",
                       f"补充 `{key}` 的范围条件；分区裁剪失效时分区表比普通表更慢",
                       ast, primary))
    return out


def rule_select_for_update(ast, ctx) -> list[Finding]:
    sql_text = ctx.get("_sql_text", "") or ""
    if not re.search(r"\bFOR\s+UPDATE\b", sql_text, re.I):
        return []
    where = ast.args.get("where") if isinstance(ast, exp.Select) else None
    has_indexed = False
    if where is not None:
        for node in where.walk():
            if isinstance(node, exp.EQ):
                if node.find(exp.Column) is not None:
                    has_indexed = True
    if has_indexed:
        return []
    return [_mk("LOCK003", "high",
                "SELECT ... FOR UPDATE 缺少等值条件：会把扫描过的所有行都加上排他锁",
                "确保锁定的是明确的索引行（通常是主键），否则等价于锁表", ast, table_of(ast))]


def rule_transaction_in_app(ast, ctx) -> list[Finding]:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|ROLLBACK)\b", sql_text, re.I):
        return [_mk("TX002", "low",
                    "应用层 SQL 中出现事务控制语句：事务边界应由连接池/框架统一管理",
                    "移除显式 BEGIN/COMMIT，避免连接归还给池时事务状态不确定",
                    ast, table_of(ast))]
    if re.search(r"\bSET\s+autocommit\s*=\s*0\b", sql_text, re.I):
        return [_mk("TX003", "high",
                    "关闭 autocommit 后如果异常退出未提交，会产生长事务并一直持有锁",
                    "保持 autocommit=1；确需事务时显式 begin/commit 并保证 rollback 分支",
                    ast, table_of(ast))]
    return []


def rule_engine_specific(ast, ctx) -> list[Finding]:
    """引擎专属问题：MySQL 与 PG 完全不同的处理方式。"""
    d = ctx.get("_dialect", "")
    sql_text = ctx.get("_sql_text", "") or ""
    out = []
    if d in ("postgres", "gaussdb", "kingbase"):
        if re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", sql_text, re.I) and \
                not re.search(r"\bCONCURRENTLY\b", sql_text, re.I):
            out.append(_mk("PG001", "high",
                           "PG 中 CREATE INDEX 默认持有 SHARE 锁，期间该表无法写入",
                           "改为 CREATE INDEX CONCURRENTLY（注意：它不能放在事务块里，"
                           "且失败后会留下 INVALID 索引需要手动清理）", ast, table_of(ast)))
        if re.search(r"\bVACUUM\s+FULL\b", sql_text, re.I):
            out.append(_mk("PG002", "high",
                           "VACUUM FULL 会持有 ACCESS EXCLUSIVE 锁重建整表，期间完全不可读写",
                           "用 pg_repack 在线重整；仅在维护窗口内使用 VACUUM FULL",
                           ast, table_of(ast)))
        if re.search(r"\bSELECT\s+\*\s+FROM\s+pg_stat_activity", sql_text, re.I):
            pass
    if d in ("mysql", "mariadb", "oceanbase", "tidb"):
        if re.search(r"\bALTER\s+TABLE\b", sql_text, re.I) and \
                not re.search(r"\bALGORITHM\s*=|INSTANT|INPLACE", sql_text, re.I):
            out.append(_mk("MY001", "medium",
                           "ALTER TABLE 未指定 ALGORITHM/LOCK：不同版本默认行为差异很大，"
                           "5.6 上很多操作会 COPY 整表",
                           "显式指定 ALGORITHM=INSTANT/INPLACE, LOCK=NONE；"
                           "不支持时用 gh-ost / pt-osc", ast, table_of(ast)))
        if re.search(r"\bselect\s+\*\s+from\s+\w+\s+where\s+\w+\s+=\s+\d+\s+for\s+update\b", sql_text, re.I):
            pass
    if d == "oracle":
        if re.search(r"\bSELECT\s+\*\s+FROM\s+(\w+)\s+WHERE\s+ROWNUM\s*<=", sql_text, re.I):
            pass
    return out


def rule_cross_join_subquery(ast, ctx) -> list[Finding]:
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


def rule_union_all_vs_union(ast, ctx) -> list[Finding]:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bUNION\s+(?!ALL)", sql_text, re.I):
        return [_mk("AGG004", "low",
                    "UNION 会做去重排序；如果两段结果本来就不重叠，这是白白付出的代价",
                    "确认业务上无需去重时用 UNION ALL，通常能省掉一次排序 + 去重",
                    ast, table_of(ast))]
    return []


def review_sql(sql: str, dialect: str | None = None, **opts) -> ReviewResult:
    """对一段 SQL（可多句）做静态体检。"""
    d = normalize_dialect(dialect)
    result = ReviewResult(dialect=d)
    stmts = parse_statements(sql, d)
    result.statement_count = len(stmts)
    ctx = dict(opts)
    ctx["_dialect"] = d

    for stmt in stmts:
        ctx["_sql_text"] = stmt.sql
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
            result.findings.extend(fn(stmt.ast, ctx))

    result.findings = _dedup_findings(result.findings)
    result.findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return result


def _dedup_findings(findings: list[Finding]) -> list[Finding]:
    """同一规则在同一行重复报没有意义。"""
    seen: set[tuple] = set()
    out = []
    for f in findings:
        key = (f.rule_id, f.line, f.table)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


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
        # 第二批：谓词/类型/聚合/JOIN/锁/事务/引擎专属
        rule_implicit_join,
        rule_cartesian,
        rule_join_depth,
        rule_implicit_cast,
        rule_negation,
        rule_not_null_safe,
        rule_count_star_vs_col,
        rule_distinct_and_groupby,
        rule_big_result_no_limit,
        rule_select_subquery,
        rule_batch_dml,
        rule_replace_into,
        rule_destructive_ddl,
        rule_unstable_order_limit,
        rule_partition_key_missing,
        rule_select_for_update,
        rule_transaction_in_app,
        rule_engine_specific,
        rule_cross_join_subquery,
        rule_union_all_vs_union,
    )
]
