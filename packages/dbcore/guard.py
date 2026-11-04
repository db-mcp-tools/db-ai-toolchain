"""只读护栏。

任何"要打到目标库上执行"的 SQL 必须先过这里。
原则：不认意图，只认语法；不放行的(nolist)就是不放行。
"""

from __future__ import annotations

from sqlglot import exp

from dbcore.dialect import normalize_dialect, parse_statements

READONLY_KINDS = {"select", "other"}  # other = SHOW/EXPLAIN/DESC 等
READONLY_PREFIX = ("select", "show", "explain", "desc", "describe", "with", "analyze")


class GuardError(Exception):
    """语句不允许在只读会话中执行。"""


def ensure_single_statement(sql: str, dialect: str | None = None) -> None:
    stmts = parse_statements(sql, dialect)
    if len(stmts) == 0:
        raise GuardError("empty statement")
    if len(stmts) > 1:
        raise GuardError(f"只允许单条语句，检测到 {len(stmts)} 条")


def assert_readonly(sql: str, dialect: str | None = None) -> None:
    """确认语句只读。写操作、DDL、多行注入都会被拦下。"""
    d = normalize_dialect(dialect)
    stmts = parse_statements(sql, d)
    if not stmts:
        raise GuardError("empty statement")
    if len(stmts) > 1:
        raise GuardError(f"只允许单条语句，检测到 {len(stmts)} 条（疑似注入堆叠）")

    stmt = stmts[0]
    text = stmt.sql.strip().lower().rstrip(";").strip()

    if stmt.parsable:
        if stmt.kind not in READONLY_KINDS:
            raise GuardError(
                f"只读护栏拦截：{stmt.kind.upper()} 语句不允许在巡检会话执行 -> {stmt.sql[:120]}"
            )
        for node in stmt.ast.walk():
            if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Alter, exp.Drop, exp.Command)):
                cmd = node.key.upper() if isinstance(node, exp.Command) else type(node).__name__.upper()
                # Command 里 explain/show 是允许的
                if isinstance(node, exp.Command) and text.startswith(READONLY_PREFIX):
                    continue
                if isinstance(node, exp.Select):
                    continue
                raise GuardError(f"只读护栏拦截：语句内检测到 {cmd}")
        return

    # 解析失败时回退到前缀判断，宁可漏也不放行明显写操作
    if not text.startswith(READONLY_PREFIX):
        raise GuardError(f"该方言无法解析且不以只读关键字开头，拒绝执行 -> {text[:80]}")
