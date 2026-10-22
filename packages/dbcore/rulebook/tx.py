"""规则分册：事务与锁边界（TX）。一个函数对应一个规则 ID。"""

from __future__ import annotations

import re

from sqlglot import exp

from dbcore.rulebook.core import _mk, table_of


def rule_begin_commit(ast, ctx) -> list:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|ROLLBACK)\b", sql_text, re.I):
        return [_mk("TX002", "low",
                    "应用层 SQL 中出现事务控制语句：事务边界应由连接池/框架统一管理",
                    "移除显式 BEGIN/COMMIT，避免连接归还给池时事务状态不确定",
                    ast, table_of(ast))]
    return []


def rule_set_autocommit(ast, ctx) -> list:
    sql_text = ctx.get("_sql_text", "") or ""
    if re.search(r"\bSET\s+autocommit\s*=\s*0\b", sql_text, re.I):
        return [_mk("TX003", "high",
                    "关闭 autocommit 后如果异常退出未提交，会产生长事务并一直持有锁",
                    "保持 autocommit=1；确需事务时显式 begin/commit 并保证 rollback 分支",
                    ast, table_of(ast))]
    return []


RULES: list[tuple[str, object]] = [
    ("TX002", rule_begin_commit),
    ("TX003", rule_set_autocommit),
]

META: dict[str, dict] = {
    "TX002": {"name": "应用层事务控制语句", "category": "transaction", "severity": "low",
              "why": "事务边界应由连接池/框架管理，混入业务 SQL 会导致连接归还时状态不确定",
              "false_positive": "脚本批量执行"},
    "TX003": {"name": "关闭 autocommit", "category": "transaction", "severity": "high",
              "why": "异常退出时留下长事务，长期持有锁并阻塞 vacuum/DDL",
              "false_positive": "显式管理的批处理脚本"},
}
