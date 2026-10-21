"""异构迁移工具箱 —— 国产化替换的正确性验证。

为什么 Oracle→PG/达梦 不能只靠"翻译一遍能跑"就算完：
  * 语义陷阱：Oracle 空串 = NULL；MySQL 隐式转换；日期精度
  * 类型陷阱：NUMBER → numeric/decimal 的精度丢失；VARCHAR 字节 vs 字符语义
  * 排序陷阱：字符集与 collation 不同导致 ORDER BY 结果顺序不同
  * 对象陷阱：序列、同义词、存储过程、包、触发器不会被自动搬迁

本模块不搬运数据（那不该由工具做），只负责：
  1. 扫出所有不被目标库支持的写法
  2. 给出等价改写（不能自动改的明确指出风险）
  3. 生成客户可在自己环境执行的校验 SQL 批次
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dbcore.dialect import normalize_dialect

_PLATFORMS = ("mysql", "postgres", "oracle", "clickhouse", "sqlite",
              "dameng", "oceanbase", "gaussdb", "kingbase")


@dataclass(frozen=True)
class Gap:
    rule_id: str
    category: str              # syntax | function | type | semantic | object
    source: str                # 命中的写法
    target_platform: str
    severity: str              # critical | high | medium | low
    description: str
    rewrite: str | None
    auto_translatable: bool
    note: str = ""


# ------------------------------------------------------------------
# 1. 类型映射
# ------------------------------------------------------------------

TYPE_MAP: dict[str, dict[str, str]] = {
    "postgres": {
        "NUMBER": "numeric", "NUMBER(p,s)": "numeric(p,s)", "INTEGER": "integer",
        "INT": "integer", "SMALLINT": "smallint", "BIGINT": "bigint",
        "VARCHAR2": "varchar", "NVARCHAR2": "varchar", "CHAR": "char", "NCHAR": "char",
        "CLOB": "text", "NCLOB": "text", "LONG": "text", "BLOB": "bytea", "RAW": "bytea",
        "DATE": "timestamp(0)", "TIMESTAMP": "timestamp", "TIMESTAMP(6)": "timestamp(6)",
        "FLOAT": "double precision", "BINARY_FLOAT": "real", "BINARY_DOUBLE": "double precision",
        "BOOLEAN": "boolean",
        "DATETIME": "timestamp", "TINYINT": "smallint", "TEXT": "text", "DOUBLE": "double precision",
    },
    "dameng": {
        "VARCHAR2": "VARCHAR", "NVARCHAR2": "NVARCHAR", "NUMBER": "DEC",
        "CLOB": "TEXT", "BLOB": "BLOB", "DATE": "TIMESTAMP", "LONG": "TEXT",
    },
    "oceanbase": {
        "VARCHAR2": "VARCHAR", "NUMBER": "DECIMAL", "CLOB": "TEXT",
        "DATE": "DATETIME", "BLOB": "BLOB",
    },
    "gaussdb": {
        "VARCHAR2": "VARCHAR", "NVARCHAR2": "NVARCHAR2", "NUMBER": "NUMERIC",
        "CLOB": "TEXT", "DATE": "TIMESTAMP", "LONG": "TEXT",
    },
    "kingbase": {
        "VARCHAR2": "VARCHAR", "NUMBER": "NUMERIC", "CLOB": "TEXT", "DATE": "TIMESTAMP",
    },
    "mysql": {
        "NUMBER": "DECIMAL", "VARCHAR2": "VARCHAR", "CLOB": "LONGTEXT", "NVARCHAR2": "NVARCHAR",
        "BLOB": "BLOB", "TIMESTAMP": "DATETIME", "SEQUENCE": "AUTO_INCREMENT",
    },
}

# 精度/容量需要人工复核的组合
TYPE_RISKS = [
    ("NUMBER", "postgres", "high",
     "Oracle NUMBER 不指定精度时会接受任意精度；映射到 numeric 后若列上没有小数位约束，"
     "round-trip 可能出现Scientific计数或精度截断"),
    ("VARCHAR2", "postgres", "medium",
     "Oracle 默认 BYTE 语义、PG 默认 CHARACTER 语义；中文场景同样的长度定义能存的字符数不同"),
    ("DATE", "postgres", "high",
     "Oracle DATE 含时分秒，PG date 不含；必须映射为 timestamp(0)，否则时间信息丢失"),
    ("CHAR", "postgres", "low", "PG 的 char 会去除尾部空格，Oracle 会补齐（比较语义不同）"),
    ("CLOB", "dameng", "medium", "达梦 TEXT 有长度上限差异，超大字段需确认页大小"),
    ("DATETIME", "oceanbase", "medium", "OB MySQL 模式 DATETIME 精度默认 0，Oracle TIMESTAMP(6) 会丢精度"),
]


def map_type(source_type: str, target_dialect: str) -> dict:
    t = source_type.strip().upper()
    mapping = TYPE_MAP.get(target_dialect, {})
    # NUMBER(10,2) 这类带精度的先做规范化匹配
    base = re.sub(r"\(.*\)", "", t).strip()
    precision = re.search(r"\(([^)]*)\)", source_type)
    if t in mapping:
        target = mapping[t]
    elif base in mapping:
        target = mapping[base] + (f"({precision.group(1)})" if precision else "")
    elif source_type.upper() in mapping:
        target = mapping[source_type.upper()]
    else:
        target = None
    risks = [r for r in TYPE_RISKS if r[0] == base and r[1] == target_dialect]
    return {
        "source": source_type,
        "target": target,
        "target_dialect": target_dialect,
        "auto": target is not None,
        "risks": [{"severity": r[2], "description": r[3]} for r in risks],
    }


# ------------------------------------------------------------------
# 2. 语法/函数差异规则
# ------------------------------------------------------------------

# (rule_id, category, 正则, severity, 说明, 改写到 MySQL/PG 模板, 平台限制)
GAP_RULES: list[tuple[str, str, str, str, str, dict[str, str] | None, tuple[str, ...]]] = [
    ("MG001", "syntax",
     r"\bROWNUM\b\s*(<=|<|=)", "high", "ROWNUM 分页/限量",
     {"postgres": "LIMIT n", "mysql": "LIMIT n", "dameng": "ROWNUM（原生支持）"}, ()),
    ("MG002", "syntax",
     r"\bSELECT\s+.*\bFROM\s+dual\b", "low", "DUAL 伪表",
     {"postgres": "去掉 FROM dual（PG 允许 SELECT 无 FROM）",
      "mysql": "去掉 FROM dual 或保留（MySQL 兼容）"}, ()),
    ("MG003", "function",
     r"\bNVL\s*\(", "medium", "NVL 空值替换",
     {"postgres": "COALESCE(...)", "mysql": "IFNULL(...) 或 COALESCE(...)",
      "dameng": "NVL（原生支持）", "gaussdb": "NVL（原生支持）"}, ()),
    ("MG004", "function",
     r"\bDECODE\s*\(", "medium", "DECODE 分支判断",
     {"postgres": "CASE WHEN ... THEN ... ELSE ... END", "mysql": "CASE WHEN ... END"}, ()),
    ("MG005", "function",
     r"\bTO_CHAR\s*\(", "medium", "TO_CHAR 格式化",
     {"postgres": "to_char(col, 'YYYY-MM-DD')；注意 PG 的格式串与 Oracle 不完全一致",
      "mysql": "DATE_FORMAT(col, '%Y-%m-%d')"}, ()),
    ("MG006", "function",
     r"\bTO_DATE\s*\(", "high", "TO_DATE 字符串转日期",
     {"postgres": "to_date(col, 'fmt') 或 col::date",
      "mysql": "STR_TO_DATE(col, '%Y-%m-%d')"}, ()),
    ("MG007", "function",
     r"\bSYSDATE\b|\bSYSTIMESTAMP\b", "low", "系统时间",
     {"postgres": "now() / current_timestamp", "mysql": "NOW()", "dameng": "SYSDATE（原生支持）"}, ()),
    ("MG008", "syntax",
     r"\|\|", "medium", "字符串连接运算符",
     {"mysql": "CONCAT(a, b)（MySQL 的 || 默认是逻辑或，除非开启 PIPES_AS_CONCAT）",
      "postgres": "|| 原生支持"}, ()),
    ("MG009", "function",
     r"\bSUBSTR\s*\(", "low", "SUBSTR",
     {"postgres": "substr / substring（原生支持）", "mysql": "SUBSTRING / SUBSTR（兼容）"}, ()),
    ("MG010", "syntax",
     r"\bSTART\s+WITH\b.*\bCONNECT\s+BY\b", "high", "层次递归查询",
     {"postgres": "WITH RECURSIVE ... UNION ALL", "mysql": "WITH RECURSIVE（8.0+）",
      "dameng": "CONNECT BY（原生支持）"}, ()),
    ("MG011", "syntax",
     r"\bMERGE\s+INTO\b", "high", "MERGE INTO upsert",
     {"postgres": "INSERT ... ON CONFLICT (key) DO UPDATE",
      "mysql": "INSERT ... ON DUPLICATE KEY UPDATE"}, ()),
    ("MG012", "syntax",
     r"\b(\w+)\.NEXTVAL\b|\bSEQ\w*\.NEXTVAL\b", "high", "序列取下一个值",
     {"mysql": "无序列概念：改用 AUTO_INCREMENT + LAST_INSERT_ID() 或专用序列表",
      "postgres": "nextval('seq_name')"}, ()),
    ("MG013", "function",
     r"\bLISTAGG\s*\(", "medium", "行转列聚合",
     {"postgres": "string_agg(col, ',' ORDER BY ...)", "mysql": "GROUP_CONCAT(col ORDER BY ...)"}, ()),
    ("MG014", "syntax",
     r"\bMINUS\b", "medium", "集合差",
     {"postgres": "EXCEPT", "mysql": "NOT EXISTS 子查询 / LEFT JOIN ... IS NULL"}, ()),
    ("MG015", "syntax",
     r"\bINTERSECT\b", "low", "集合交",
     {"mysql": "MySQL 8.0.31+ 支持；老版本需改写为 INNER JOIN"}, ()),
    ("MG016", "function",
     r"\bADD_MONTHS\s*\(", "medium", "月份加减",
     {"postgres": "(col + interval '1 month')", "mysql": "DATE_ADD(col, INTERVAL 1 MONTH)"}, ()),
    ("MG017", "function",
     r"\bTRUNC\s*\(", "medium", "TRUNC 截断日期或数值",
     {"postgres": "date_trunc('day', col) / trunc(numeric)",
      "mysql": "DATE(col) / FLOOR(n)"}, ()),
    ("MG018", "syntax",
     r"\bFROM\s+(\w+)\s+(\w+)\s*,\s*(\w+)\s+(\w+)\s+WHERE", "low", "逗号连接（隐式 JOIN）",
     {"postgres": "改写为显式 JOIN ... ON", "mysql": "改写为显式 JOIN ... ON"}, ()),
    ("MG019", "semantic",
     r"=\s*''", "high", "空字符串比较",
     {"postgres": "PG 中 '' ≠ NULL，Oracle 中 '' = NULL —— 必须逐个改写，这是最容易漏的语义差异"}, ()),
    ("MG020", "type",
     r"\bTINYINT\s*\(\s*1\s*\)", "medium", "TINYINT(1) 布尔",
     {"postgres": "改为 boolean；ORM 层面也需同步调整"}, ()),
    ("MG021", "syntax",
     r"\bEXPLAIN\s+PLAN\s+FOR\b", "low", "Oracle 执行计划语法",
     {"postgres": "EXPLAIN (ANALYZE, BUFFERS)", "mysql": "EXPLAIN / EXPLAIN ANALYZE"}, ()),
    ("MG022", "function",
     r"\bIFNULL\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)", "low", "IFNULL",
     {"postgres": "COALESCE(col, N)", "dameng": "NVL(col, N)"}, ()),
    ("MG023", "syntax",
     r"\bLIMIT\s+\d+\s*,\s*\d+", "medium", "MySQL LIMIT offset,size",
     {"postgres": "LIMIT n OFFSET m", "oracle": "必须用子查询包裹 + ROWNUM"}, ()),
    ("MG024", "object",
     r"\bCREATE\s+(OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|SYNONYM)\b",
     "high", "存储过程/包/触发器/同义词",
     {"postgres": "需要用 PL/pgSQL 重写；包（PACKAGE）无对应概念，需拆成 schema + function",
      "dameng": "达梦兼容大部分 Oracle PL/SQL，但仍有函数需要逐个验证"}, ()),
    ("MG025", "semantic",
     r"\bGROUP\s+BY\b", "low", "GROUP BY 严格性",
     {"postgres": "PG 要求 SELECT 中非聚合列必须出现在 GROUP BY（除非函数依赖同一主键），比 MySQL 严格",
      "mysql": "ONLY_FULL_GROUP_BY 开启后同样严格"}, ()),
    ("MG026", "semantic",
     r"\bORDER\s+BY\b", "low", "排序结果的稳定性与 collation",
     {"postgres": "注意 collation；中文排序常用 zh_CN 而非常见的 en_US",
      "mysql": "注意 utf8mb4_0900_ai_ci 与 utf8mb4_general_ci 的差异"}, ()),
    ("MG027", "type",
     r"\bUUID\s*\(", "low", "UUID 生成",
     {"postgres": "gen_random_uuid()（PG13+ 内置，旧版需 uuid-ossp 扩展）",
      "mysql": "UUID() 返回带连字符的字符串，通常建议存 BINARY(16)"}, ()),
    ("MG028", "syntax",
     r"\bUPDATE\s+\w+\s+SET\s+.*\bFROM\b", "high", "UPDATE ... FROM 语法",
     {"mysql": "改写为 UPDATE a JOIN b ON ... SET a.x = b.y",
      "postgres": "原生支持 UPDATE ... FROM"}, ()),
    ("MG029", "function",
     r"\bGROUP_CONCAT\s*\(", "low", "GROUP_CONCAT",
     {"postgres": "string_agg(col, ',')", "dameng": "WM_CONCAT / LISTAGG"}, ()),
    ("MG030", "semantic",
     r"\bDELETE\s+FROM\s+(\w+)\s+(\w+)\s+WHERE", "medium", "带别名的 DELETE",
     {"mysql": "DELETE a FROM t a WHERE ...", "postgres": "不支持别名，需 DELETE FROM t WHERE ..."}, ()),
]


def detect_gaps(sql: str, target_dialect: str, source_dialect: str | None = None) -> list[dict]:
    """扫描 SQL，列出目标方言下的所有不兼容写法。"""
    out: list[dict] = []
    tgt = normalize_dialect(target_dialect)
    for rid, cat, pattern, sev, desc, rewrite_map, restricts in GAP_RULES:
        if restricts and tgt not in restricts:
            pass  # 保留：restricts 目前未启用全部规则，仅作占位
        for m in re.finditer(pattern, sql, re.I | re.S):
            rewrite = (rewrite_map or {}).get(tgt)
            # 改写一切都用 case-insensitive 匹配，取原文片段作为证据
            snippet = m.group(0)
            if len(snippet) > 120:
                snippet = snippet[:120] + "..."
            out.append({
                "rule_id": rid,
                "category": cat,
                "severity": sev,
                "source": snippet,
                "position": m.start(),
                "description": desc,
                "target_platform": tgt,
                "rewrite": rewrite,
                "auto_translatable": rewrite is not None and "必须" not in (rewrite or ""),
                "note": "" if rewrite else "该写法无法自动改写，需要人工处理",
            })
    return _dedup(out)


def _dedup(items: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for it in items:
        key = (it["rule_id"], it["source"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def translate_sql(sql: str, source_dialect: str, target_dialect: str) -> dict:
    """按规则做确定性改写。做不到自动改的返回 alert 而不是改动原文。

    顺序很重要：先改结构（分页），再改函数（NVL 等）。
    """
    import sqlglot

    src = normalize_dialect(source_dialect)
    tgt = normalize_dialect(target_dialect)
    if tgt in _PLATFORMS and src in _PLATFORMS:
        for attempt in ("transpile", None):
            try:
                fn = getattr(sqlglot, attempt) if attempt else sqlglot.parse_one
                if attempt == "transpile":
                    parts = fn(sql, read=src, write=tgt)
                    if parts:
                        translated = parts[0] if isinstance(parts, list) else parts
                        return {
                            "source": sql, "target": translated,
                            "source_dialect": src, "target_dialect": tgt,
                            "method": "sqlglot_transpile",
                            "warnings": ["sqlglot 的方言转换覆盖大部分语法，但**不保证语义等价**，"
                                         "尤其是 NULL 语义、排序规则、隐式转换，必须做数据比对"],
                            "unresolved": detect_gaps(translated, tgt),
                        }
                else:
                    node = fn(sql, read=src)
                    return {
                        "source": sql, "target": node.sql(dialect=tgt),
                        "source_dialect": src, "target_dialect": tgt,
                        "method": "sqlglot_parse",
                        "warnings": ["基于解析树的转换，建议用小样本数据验证结果一致"],
                        "unresolved": detect_gaps(node.sql(dialect=tgt), tgt),
                    }
            except Exception:
                continue
    return {
        "source": sql, "target": None, "source_dialect": src, "target_dialect": tgt,
        "method": "none",
        "warnings": [f"无法从 {src} 自动转换到 {tgt}，需人工改写"],
        "unresolved": detect_gaps(sql, tgt),
    }


# ------------------------------------------------------------------
# 3. 一致性校验计划
# ------------------------------------------------------------------


def consistency_plan(
    tables: list[str],
    pk: dict[str, str] | None = None,
    source_dialect: str = "oracle",
    target_dialect: str = "postgres",
    sample_ratio: float = 0.05,
    batch_size: int = 10_000,
) -> list[dict]:
    """生成可执行的比对 SQL 批次。

    分两层：
      L1 计数与校验和（快，全表）
      L2 明细抽样比对（慢，只在 L1 不通过时对差异区间下钻）
    """
    pk = pk or {}
    plan = []
    for i, t in enumerate(tables, 1):
        key = pk.get(t, "id")
        plan.append({
            "ordinal": i * 2 - 1,
            "layer": "L1",
            "table": t,
            "name": f"L1 计数与聚合校验 - {t}",
            "source_sql": f"SELECT COUNT(*) AS cnt, SUM(ORA_HASH({key})) AS agg FROM {t}",
            "target_sql": f"SELECT COUNT(*) AS cnt, SUM(hashtext({key}::text)) AS agg FROM {t}",
            "expect": "两边 cnt 与 agg 完全一致",
            "on_mismatch": "进入 L2 抽样下钻",
            "batch_size": batch_size,
        })
        plan.append({
            "ordinal": i * 2,
            "layer": "L2",
            "table": t,
            "name": f"L2 抽样明细比对 - {t}",
            "source_sql": f"SELECT * FROM {t} WHERE MOD({key}, {int(1/sample_ratio)}) = 0 ORDER BY {key}",
            "target_sql": f"SELECT * FROM {t} WHERE MOD({key}, {int(1/sample_ratio)}) = 0 ORDER BY {key}",
            "expect": "逐行比对完全一致（含 NULL 位置）",
            "on_mismatch": "定位到具体主键区间，人工复核该区间全部数据",
            "note": "NULL 与空串的语义差异在这一层最容易暴露",
        })
    return plan


def object_parity_checks(source_dialect: str, target_dialect: str) -> list[dict]:
    """对象数量与定义比对的 SQL 清单。"""
    if source_dialect in ("mysql", "mariadb", "oceanbase"):
        src_sql = ("SELECT table_name FROM information_schema.tables "
                   "WHERE table_schema = DATABASE() AND table_type='BASE TABLE'")
    else:
        src_sql = "SELECT table_name FROM user_tables"
    if target_dialect in ("postgres", "gaussdb", "kingbase"):
        tgt_sql = "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    else:
        tgt_sql = ("SELECT table_name FROM information_schema.tables "
                   "WHERE table_type='BASE TABLE'")
    return [
        {"object": "table", "source_sql": src_sql, "target_sql": tgt_sql,
         "expect": "集合完全一致（注意大小写：Oracle/达梦默认大写，PG 默认小写）"},
        {"object": "view",
         "source_sql": "SELECT view_name FROM user_views" if source_dialect == "oracle"
                       else "SELECT table_name FROM information_schema.views",
         "target_sql": "SELECT viewname FROM pg_views WHERE schemaname = current_schema()"
                       if target_dialect in ("postgres", "gaussdb", "kingbase")
                       else "SELECT table_name FROM information_schema.views",
         "expect": "视图数量一致，且每个视图的执行结果一致（定义对了不代表结果对了）"},
        {"object": "index", "source_sql": "SELECT index_name FROM user_indexes"
            if source_dialect == "oracle" else "SELECT index_name FROM information_schema.statistics",
         "target_sql": "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            if target_dialect in ("postgres", "gaussdb", "kingbase")
            else "SELECT index_name FROM information_schema.statistics",
         "expect": "索引名可能因长度限制被截断，需按 (表, 列序列) 而非名称比对"},
        {"object": "constraint", "source_sql": "SELECT constraint_name FROM user_constraints",
         "target_sql": "SELECT conname FROM pg_constraint",
         "expect": "外键/唯一/检查约束齐全；注意 NOT NULL 两边表达一致"},
        {"object": "sequence", "source_sql": "SELECT sequence_name FROM user_sequences",
         "target_sql": "SELECT sequencename FROM pg_sequences",
         "expect": "★ 起始值必须设为迁移后的 MAX(id)+1，否则上线后主键冲突"},
        {"object": "routine", "source_sql": "SELECT object_name FROM user_procedures",
         "target_sql": "SELECT proname FROM pg_proc",
         "expect": "存储过程/函数需逐个验证行为，语法通过不等于逻辑等价"},
        {"object": "trigger", "source_sql": "SELECT trigger_name FROM user_triggers",
         "target_sql": "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal",
         "expect": "触发器数量与触发时机一致"},
    ]


CHARSET_RISKS = [
    ("utf8mb4_general_ci", "zh_CN", "中文排序不规范，拼音排序场景需注意"),
    ("AL32UTF8", "utf8mb4", "Oracle UTF8 的中文占 3 字节，MySQL utf8mb4 表情符号占 4 字节，"
                            "VARCHAR(n) 的 n 在不同库含义可能是字符数或字节数"),
    ("ZHS16GBK", "GB18030", "GBK 到 UTF8 转换会有无法映射的字符，需先做字符集扫描"),
]


def check_charset(source: str, target: str) -> dict:
    risks = [r for r in CHARSET_RISKS if r[0].lower() == source.lower()]
    return {
        "source": source, "target": target,
        "needs_attention": bool(risks),
        "risks": [{"pair": f"{r[0]} -> {r[1]}", "description": r[2]} for r in risks],
        "check_sql": (
            "SELECT column_name, data_type, character_maximum_length, character_octet_length "
            "FROM information_schema.columns WHERE data_type IN ('varchar','char')"
        ),
        "note": "character_maximum_length 与 character_octet_length 不一致即说明是多字节字符集，"
                "迁移时需按字节长度重新核算",
    }


def estimate_effort(sqls: list[str], target_dialect: str) -> dict:
    """按命中规则估算改造工作量。粗粒度但比拍脑袋好。"""
    from dbcore.dialect import normalize_dialect
    counts: dict[str, int] = {}
    total_gaps = 0
    auto = 0
    for s in sqls:
        for g in detect_gaps(s, target_dialect):
            counts[g["rule_id"]] = counts.get(g["rule_id"], 0) + 1
            total_gaps += 1
            if g["auto_translatable"]:
                auto += 1
    # 人工处理的成本远高于自动改写
    manual = total_gaps - auto
    hours = round(auto * 0.05 + manual * 0.5 + len(sqls) * 0.05, 1)
    return {
        "sql_count": len(sqls),
        "gap_count": total_gaps,
        "auto_translatable": auto,
        "manual": manual,
        "estimated_hours": hours,
        "by_rule": sorted(counts.items(), key=lambda x: -x[1]),
        "assumption": "自动改写按 3 分钟/处、人工改写按 30 分钟/处估算，仅供参考",
    }
