"""规则分册：引擎专属（MY / PG）。

每条规则通过 engines 字段声明"只在哪些方言下生效"，注册表据此在调用前过滤，
无需在函数体内反复判方言（但仍保留内部判读作为双保险）。
每个函数只对应一个规则 ID。
"""

from __future__ import annotations

import re

from sqlglot import exp

from dbcore.rulebook.core import _mk, table_of


def rule_pg_index(ast, ctx) -> list:
    d = ctx.get("_dialect", "")
    if d not in ("postgres", "gaussdb", "kingbase"):
        return []
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", sql_text, re.I) and \
            not re.search(r"\bCONCURRENTLY\b", sql_text, re.I):
        return [_mk("PG001", "high",
                    "PG 中 CREATE INDEX 默认持有 SHARE 锁，期间该表无法写入",
                    "改为 CREATE INDEX CONCURRENTLY（注意：它不能放在事务块里，"
                    "且失败后会留下 INVALID 索引需要手动清理）", ast, table_of(ast))]
    return []


def rule_pg_vacuum(ast, ctx) -> list:
    d = ctx.get("_dialect", "")
    if d not in ("postgres", "gaussdb", "kingbase"):
        return []
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bVACUUM\s+FULL\b", sql_text, re.I):
        return [_mk("PG002", "high",
                    "VACUUM FULL 会持有 ACCESS EXCLUSIVE 锁重建整表，期间完全不可读写",
                    "用 pg_repack 在线重整；仅在维护窗口内使用 VACUUM FULL",
                    ast, table_of(ast))]
    return []


def rule_mysql_alter(ast, ctx) -> list:
    d = ctx.get("_dialect", "")
    if d not in ("mysql", "mariadb", "oceanbase", "tidb"):
        return []
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bALTER\s+TABLE\b", sql_text, re.I) and \
            not re.search(r"\bALGORITHM\s*=|INSTANT|INPLACE", sql_text, re.I):
        return [_mk("MY001", "medium",
                    "ALTER TABLE 未指定 ALGORITHM/LOCK：不同版本默认行为差异很大，"
                    "5.6 上很多操作会 COPY 整表",
                    "显式指定 ALGORITHM=INSTANT/INPLACE, LOCK=NONE；"
                    "不支持时用 gh-ost / pt-osc", ast, table_of(ast))]
    return []


RULES: list[tuple[str, object]] = [
    ("PG001", rule_pg_index),
    ("PG002", rule_pg_vacuum),
    ("MY001", rule_mysql_alter),
]

META: dict[str, dict] = {
    "PG001": {"name": "PG 建索引未用 CONCURRENTLY", "category": "postgres", "severity": "high",
              "why": "默认 CREATE INDEX 持 SHARE 锁，期间无法写入",
              "false_positive": "建索引的同时该表本来就不允许写入",
              "engines": ["postgres", "gaussdb", "kingbase"]},
    "PG002": {"name": "VACUUM FULL", "category": "postgres", "severity": "high",
              "why": "ACCESS EXCLUSIVE 锁重建整表，期间完全不可读写",
              "false_positive": "维护窗口内的小表",
              "engines": ["postgres", "gaussdb", "kingbase"]},
    "MY001": {"name": "MySQL ALTER 未指定 ALGORITHM", "category": "mysql", "severity": "medium",
              "why": "不同版本默认行为差异大，5.6 上很多操作会 COPY 整表",
              "false_positive": "8.0 且确认支持 INSTANT",
              "engines": ["mysql", "mariadb", "oceanbase", "tidb"]},
}
