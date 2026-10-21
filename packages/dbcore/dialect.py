"""多方言归一：所有上层能力都吃 sqlglot 的 AST，方言差异在这一层吃干净。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

_ALIASES = {
    "pg": "postgres",
    "postgresql": "postgres",
    "postgres": "postgres",
    "pgsql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "tidb": "mysql",
    "oracle": "oracle",
    "ora": "oracle",
    "clickhouse": "clickhouse",
    "ck": "clickhouse",
    "sqlite": "sqlite",
    "dameng": "oracle",
    "dm": "oracle",
    "oceanbase": "mysql",
    "ob": "mysql",
    "polardb": "mysql",
    "goldendb": "mysql",
    "gaussdb": "postgres",
    "opengauss": "postgres",
    "kingbase": "postgres",
}

SUPPORTED = ("mysql", "postgres", "oracle", "clickhouse", "sqlite")


def normalize_dialect(name: str | None) -> str:
    key = (name or "mysql").strip().lower()
    resolved = _ALIASES.get(key)
    if resolved is None:
        raise ValueError(f"unsupported dialect: {name!r}, supported: {sorted(SUPPORTED)}")
    return resolved


@dataclass
class Statement:
    index: int
    sql: str
    dialect: str
    ast: Any | None
    parse_error: str | None
    kind: str  # select / insert / update / delete / ddl / replace / other / unknown

    @property
    def parsable(self) -> bool:
        return self.ast is not None


_KIND_MAP = (
    (exp.Select, "select"),
    (exp.Union, "select"),
    (exp.Insert, "insert"),
    (exp.Update, "update"),
    (exp.Delete, "delete"),
    (exp.Create, "ddl"),
    (exp.Alter, "ddl"),
    (exp.Command, "other"),
)


def _kind_of(node: exp.Expression) -> str:
    for cls, kind in _KIND_MAP:
        if isinstance(node, cls):
            return kind
    if isinstance(node, exp.Drop):
        return "ddl"
    return "other"


def _naive_split(sql: str) -> list[str]:
    """遇到整段无法 tokenize 时的兜底：按分号切分，跳过字符串内的分号。"""
    out, buf = [], []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(sql):
                buf.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return out


def split_sql(sql: str, dialect: str | None = None) -> list[str]:
    """按方言切成多条语句。整段不可解析时降级按分号切，绝不抛异常。"""
    d = normalize_dialect(dialect)
    try:
        nodes = sqlglot.parse(sql, read=d)
    except Exception:
        return [s.strip() for s in _naive_split(sql) if s.strip()]
    out: list[str] = []
    for node in nodes:
        if node is None:
            continue
        try:
            text = node.sql(dialect=d).strip()
        except Exception:
            continue
        if text:
            out.append(text)
    return out


def parse_statements(sql: str, dialect: str | None = None) -> list[Statement]:
    """解析为 AST。保留原始 AST 节点以拿到源码行号；整段失败时降级为 unknown 而不是崩溃。"""
    d = normalize_dialect(dialect)
    try:
        nodes = sqlglot.parse(sql, read=d)
    except Exception:
        nodes = []
        statements = [
            Statement(index=i, sql=s, dialect=d, ast=None, parse_error="whole input tokenize failed", kind="unknown")
            for i, s in enumerate([x for x in _naive_split(sql) if x.strip()])
        ]
        return statements

    statements: list[Statement] = []
    for node in nodes:
        if node is None:
            continue
        try:
            text = node.sql(dialect=d).strip()
        except Exception:
            text = sql.strip()
        statements.append(
            Statement(index=len(statements), sql=text, dialect=d, ast=node, parse_error=None, kind=_kind_of(node))
        )
    return statements


def line_of(node: exp.Expression | None) -> int | None:
    """取源码行号：sqlglot 只给部分节点打 meta，缺失时向下找最近的带 meta 的子节点。"""
    if node is None:
        return None
    meta = getattr(node, "meta", None) or {}
    line = meta.get("line")
    if isinstance(line, int):
        return line
    for child in node.walk():
        meta = getattr(child, "meta", None) or {}
        line = meta.get("line")
        if isinstance(line, int):
            return line
    return None


def table_of(node: exp.Expression) -> str | None:
    tbl = node.find(exp.Table)
    if tbl is None:
        return None
    db = tbl.db
    return f"{db}.{tbl.name}" if db else tbl.name

def unwrap_parens(node):
    """去掉包裹的 Paren 节点，取到真正的表达式。

    AST 遍历中经常遇到 Paren(this=EQ(...)) 这种包装层，
    不展开会导致类型判断失败（例如 WHERE (a = 1) 里的 EQ 找不到）。
    """
    seen = 0
    while node is not None and isinstance(node, exp.Paren) and seen < 20:
        node = node.this
        seen += 1
    return node
