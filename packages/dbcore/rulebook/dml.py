"""规则分册：写操作与结构变更风险（DML / INS / DDL / LOCK）。

约定：一个函数只对应一个规则 ID，便于按 ID 精确启停。
依赖 ctx["_sql_text"]（正则扫描原始文本），因为 sqlglot 对
REPLACE/TRUNCATE/DROP/MODIFY 等语句的 AST 不够规整。
"""

from __future__ import annotations

import re

from sqlglot import exp

from dbcore.rulebook.core import _mk, table_of


def rule_dml_without_where(ast, ctx) -> list:
    if not isinstance(ast, (exp.Update, exp.Delete)):
        return []
    if ast.args.get("where") is None:
        return [
            _mk("DML001", "blocker",
                f"{type(ast).__name__.upper()} 缺少 WHERE 条件，会作用于全表",
                "补齐 WHERE；确需全表操作时改为分批 LIMIT 执行并预留回滚数据",
                ast, table_of(ast))
        ]
    return []


def rule_insert_without_columns(ast, ctx) -> list:
    if isinstance(ast, exp.Insert):
        target = ast.this
        if target is not None and target.expressions == []:
            return [
                _mk("DML003", "medium",
                    "INSERT 未显式指定列列表，依赖表字段顺序",
                    "写明列名，否则任何一次加字段/改字段顺序都会让这条 SQL 写错位置",
                    ast, table_of(ast))
            ]
    return []


def rule_batch_dml(ast, ctx) -> list:
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


def rule_replace_into(ast, ctx) -> list:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bREPLACE\s+INTO\b", sql_text, re.I):
        return [_mk("DML004", "medium",
                    "REPLACE INTO 是先删后插：会改变自增 ID、触发两次 binlog、"
                    "并且在没有完整 WHERE 覆盖时会误删未指定的列",
                    "改用 INSERT ... ON DUPLICATE KEY UPDATE（MySQL）或 "
                    "INSERT ... ON CONFLICT DO UPDATE（PG）", ast, table_of(ast))]
    return []


def rule_insert_ignore(ast, ctx) -> list:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bINSERT\s+IGNORE\b", sql_text, re.I):
        return [_mk("DML005", "low",
                    "INSERT IGNORE 会静默吞掉所有错误（不只是重复键），包括数据截断",
                    "改用显式去重 + 普通 INSERT，让真正的错误暴露出来", ast, table_of(ast))]
    return []


def rule_ddl_in_app_sql(ast, ctx) -> list:
    if isinstance(ast, (exp.Create, exp.Alter, exp.Drop)):
        return [
            _mk("DDL000", "high",
                "检测到 DDL 语句，请走变更管控流程并做风险评估",
                "调用 change-risk Agent 做锁级别、影响面与回滚方案评估后再执行",
                None, table_of(ast) if hasattr(ast, "find") else None)
        ]
    return []


def rule_truncate(ast, ctx) -> list:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bTRUNCATE\b", sql_text, re.I):
        return [_mk("DDL004", "blocker",
                    "TRUNCATE 不可回滚、不走 binlog row 模式的历史明细、会重置自增计数器",
                    "确认已有可用备份；批量清除数据优先用分批 DELETE", ast, table_of(ast))]
    return []


def rule_drop(ast, ctx) -> list:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bDROP\s+(TABLE|DATABASE)\b", sql_text, re.I):
        return [_mk("DDL005", "blocker",
                    "DROP 操作不可回滚且会连带删除依赖它的视图/存储过程",
                    "上线前必须有备份与回滚验证；建议先改名观察一段时间再真正删除",
                    ast, table_of(ast))]
    return []


def rule_modify_column(ast, ctx) -> list:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bMODIFY\s+(COLUMN)?\b|\bCHANGE\s+COLUMN\b|\bALTER\s+COLUMN\b.*TYPE\b", sql_text, re.I):
        return [_mk("DDL006", "high",
                    "修改列类型会重建整张表并长时间持锁，且可能导致数据截断",
                    "用在线改表工具；先在新列上双写验证，再切换", ast, table_of(ast))]
    return []


def rule_select_for_update(ast, ctx) -> list:
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


# 每个函数只对应一个规则 ID，供注册表精确启停
RULES: list[tuple[str, object]] = [
    ("DML001", rule_dml_without_where),
    ("DML002", rule_batch_dml),
    ("DML003", rule_insert_without_columns),
    ("DML004", rule_replace_into),
    ("DML005", rule_insert_ignore),
    ("DDL000", rule_ddl_in_app_sql),
    ("DDL004", rule_truncate),
    ("DDL005", rule_drop),
    ("DDL006", rule_modify_column),
    ("LOCK003", rule_select_for_update),
]

META: dict[str, dict] = {
    "DML001": {"name": "写操作缺少 WHERE", "category": "dml", "severity": "blocker",
               "why": "UPDATE/DELETE 没有 WHERE 会作用于整张表，误操作即为数据事故",
               "false_positive": "TRUNCATE 意图的重命名类操作；但即便如此也应显式确认"},
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
    "DDL000": {"name": "应用 SQL 中出现 DDL", "category": "ddl", "severity": "high",
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
    "LOCK003": {"name": "FOR UPDATE 缺等值条件", "category": "lock", "severity": "high",
                "why": "会锁住扫描过的所有行，等价于锁表",
                "false_positive": "需要锁全表的批处理且业务可接受"},
}
