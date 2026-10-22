"""规则分册：查询结构与结果集风险（SEL / JOIN / AGG / ORDER / PART）。

只做静态分析，不需要连库。每条规则产出 Finding 列表。
"""

from __future__ import annotations

import re

from sqlglot import exp

from dbcore.rulebook.core import (_eq_pairs, _literal_of, _mk, table_of)


# ================================================================== SELECT


def rule_select_star(ast, ctx) -> list:
    stars = list(ast.find_all(exp.Star)) if isinstance(ast, exp.Select) else []
    return [
        _mk("SEL001", "low", "使用了 SELECT *",
            "显式列出字段：减少网络/内存开销，避免表结构变更导致的隐式破坏", s, table_of(ast))
        for s in stars[:1]
    ]


def rule_leading_wildcard_like(ast, ctx) -> list:
    out = []
    for like in ast.find_all(exp.Like):
        pattern = like.args.get("expression")
        val = getattr(pattern, "this", None)
        if isinstance(val, str) and val.startswith(("%", "_")):
            out.append(
                _mk("SEL002", "high",
                    "LIKE 以通配符开头，普通索引无法用于前缀匹配，必然全表扫描",
                    "改为后缀匹配、接入倒排索引/全文检索，或增加生成列 + 反向索引",
                    like, table_of(ast))
            )
    return out


def rule_function_on_column(ast, ctx) -> list:
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
                    _mk("SEL003", "high",
                        f"WHERE 中对字段 {c} 施加了函数 {node.key.upper()}()，该字段上的索引将失效",
                        "改写成字段裸列的范围条件（把函数移到常量侧），或建立函数索引/表达式索引",
                        node, table_of(ast))
                )
    return out


def rule_random_order(ast, ctx) -> list:
    order = ast.args.get("order") if isinstance(ast, exp.Select) else None
    if order is None:
        return []
    if any(isinstance(n, exp.Rand) for n in order.walk()):
        return [
            _mk("SEL004", "high",
                "ORDER BY RAND()/RANDOM() 需要为全表结果集排序",
                "改用主键区间随机取样或业务层抽样，避免随数据量劣化",
                order, table_of(ast))
        ]
    return []


def rule_deep_offset(ast, ctx) -> list:
    if not isinstance(ast, exp.Select):
        return []
    if ast.args.get("limit") is None:
        return []
    n = _literal_of(ast.args.get("offset"))
    if n is None or n < int(ctx.get("deep_offset_threshold", 1000)):
        return []
    return [
        _mk("SEL005", "medium",
            f"深度分页 OFFSET {n}，数据库需扫描并丢弃前 {n} 行后才返回结果",
            "改为游标分页（WHERE id > last_id ORDER BY id LIMIT n），或限制最大翻页深度",
            ast.args.get("limit"), table_of(ast))
    ]


def rule_limit_without_orderby(ast, ctx) -> list:
    if not isinstance(ast, exp.Select):
        return []
    if ast.args.get("limit") is not None and ast.args.get("order") is None:
        return [
            _mk("SEL006", "medium",
                "LIMIT 未配合 ORDER BY，返回结果集顺序不稳定",
                "加上确定性排序，否则分页会出现重复/漏数据",
                ast, table_of(ast))
        ]
    return []


def rule_or_chain(ast, ctx) -> list:
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return []
    if any(isinstance(n, exp.Or) for n in where.walk()):
        return [
            _mk("SEL008", "low",
                "WHERE 中存在 OR，优化器常常放弃索引而走全表扫描",
                "拆成 UNION ALL 两条走索引的查询，或评估是否满足索引合并条件",
                where, table_of(ast))
        ]
    return []


def rule_join_without_on(ast, ctx) -> list:
    """显式 JOIN 缺 ON/USING 且没有任何跨表约束 → 笛卡尔积（high）。

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
            _mk("SEL007", "high",
                "JOIN 缺少关联条件，会产生笛卡尔积",
                "补 ON/USING 条件；确需笛卡尔积请显式写 CROSS JOIN 并确认结果集体量",
                join, table_of(ast))
        )
    return out


# ================================================================== JOIN


def rule_implicit_join(ast, ctx) -> list:
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


def rule_cartesian(ast, ctx) -> list:
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


def rule_join_depth(ast, ctx) -> list:
    joins = list(ast.find_all(exp.Join)) if isinstance(ast, exp.Select) else []
    if len(joins) > 6:
        return [_mk("JOIN003", "medium",
                    f"单条语句包含 {len(joins)} 个 JOIN，超过优化器可靠工作的经验范围",
                    "拆分为多个步骤并用临时表/CTE 承接中间结果；同时确认每个 JOIN 列都有索引",
                    ast, table_of(ast))]
    return []


# ================================================================== AGG / ORDER / PART


def rule_count_star_vs_col(ast, ctx) -> list:
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


def rule_distinct_and_groupby(ast, ctx) -> list:
    if not isinstance(ast, exp.Select):
        return []
    if ast.args.get("distinct") and ast.args.get("group"):
        return [_mk("AGG003", "low",
                    "同时使用 DISTINCT 与 GROUP BY：GROUP BY 本身就去重，DISTINCT 是多余的开销",
                    "去掉 DISTINCT", ast, table_of(ast))]
    return []


def rule_big_result_no_limit(ast, ctx) -> list:
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


def rule_union_all_vs_union(ast, ctx) -> list:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bUNION\s+(?!ALL)", sql_text, re.I):
        return [_mk("AGG004", "low",
                    "UNION 会做去重排序；如果两段结果本来就不重叠，这是白白付出的代价",
                    "确认业务上无需去重时用 UNION ALL，通常能省掉一次排序 + 去重",
                    ast, table_of(ast))]
    return []


def rule_unstable_order_limit(ast, ctx) -> list:
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


def rule_partition_key_missing(ast, ctx) -> list:
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


META: dict[str, dict] = {
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
    "SEL007": {"name": "JOIN 缺少关联条件", "category": "select", "severity": "high",
               "why": "显式 JOIN 缺 ON/USING 直接产生笛卡尔积",
               "false_positive": "逗号写法且 WHERE 内有跨表等值条件时为旧风格合法写法"},
    "SEL008": {"name": "WHERE 存在 OR", "category": "select", "severity": "low",
               "why": "OR 分支常使优化器放弃索引走全表扫描",
               "false_positive": "满足索引合并（index merge）条件时"},
    "JOIN001": {"name": "逗号隐式连接", "category": "join", "severity": "medium",
                "why": "漏写连接条件会静默变成笛卡尔积",
                "false_positive": "确实是 CROSS JOIN 的场景应显式写 CROSS JOIN"},
    "JOIN002": {"name": "笛卡尔积", "category": "join", "severity": "blocker",
                "why": "多表无连接条件 = 行数相乘，是生产事故级别的问题",
                "false_positive": "极少数需要生成组合的 dim×dim 场景"},
    "JOIN003": {"name": "JOIN 层数过多", "category": "join", "severity": "medium",
                "why": "超过 6~7 张表时优化器搜索空间不足，容易选错驱动顺序",
                "false_positive": "每张表都极小且索引完备时未必有问题"},
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
    "ORDER001": {"name": "排序键不唯一的分页", "category": "select", "severity": "medium",
                 "why": "排序不稳定导致翻页重复/丢行，用户能直接感知",
                 "false_positive": "排序键本身是唯一键"},
    "PART001": {"name": "分区表未命中分区键", "category": "partition", "severity": "high",
                "why": "分区裁剪失效时会扫描所有分区，比不分区更慢",
                "false_positive": "确实需要跨分区聚合"},
}

# 每个函数只对应一个规则 ID，供注册表精确启停
RULES: list[tuple[str, object]] = [
    ("SEL001", rule_select_star),
    ("SEL002", rule_leading_wildcard_like),
    ("SEL003", rule_function_on_column),
    ("SEL004", rule_random_order),
    ("SEL005", rule_deep_offset),
    ("SEL006", rule_limit_without_orderby),
    ("SEL008", rule_or_chain),
    ("SEL007", rule_join_without_on),
    ("JOIN001", rule_implicit_join),
    ("JOIN002", rule_cartesian),
    ("JOIN003", rule_join_depth),
    ("AGG001", rule_count_star_vs_col),
    ("AGG003", rule_distinct_and_groupby),
    ("AGG002", rule_big_result_no_limit),
    ("AGG004", rule_union_all_vs_union),
    ("ORDER001", rule_unstable_order_limit),
    ("PART001", rule_partition_key_missing),
]
