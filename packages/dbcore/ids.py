"""标识符派生 —— 跨 MCP 关联的三件套之一。

统一使用这三个 ID，保证 sql-review / change-risk / dbwatch / profile-hub 能把同一条 SQL、
同一个实例、同一次调用链串起来。编排层依赖这个前提。

安全约定：派生过程绝不落盘明文密码，instance_id 只由主机/端口/引擎/库名参与哈希。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse


def _sha(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def instance_id_from_parts(host: str, port: int | str, engine: str, database: str = "") -> str:
    key = f"{engine}|{host}|{port}|{database}"
    return f"sha256:{_sha(key, 16)}"


def instance_id_from_dsn(dsn: str) -> str:
    """从 DSN / 连接串派生实例 ID。

    支持的写法：
      mysql://user:pass@host:3306/db
      postgres://user@host:5432/db
      host=127.0.0.1 port=5432 dbname=x user=y   (KV 风格)
      jdbc:mysql://host:3306/db
    密码一定不参与哈希（也不出现在任何返回值里）。
    """
    raw = (dsn or "").strip()
    if not raw:
        return "sha256:" + _sha("unknown", 16)

    if "=" in raw and "://" not in raw:                      # KV 风格
        kv = dict(
            p.split("=", 1)
            for p in raw.split()
            if "=" in p and not p.lower().startswith(("password=", "pwd="))
        )
        return instance_id_from_parts(
            kv.get("host", ""), kv.get("port", ""), "postgres", kv.get("dbname", "")
        )

    scheme = ""
    rest = raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    engine = (scheme or "").lower()
    if engine.startswith("jdbc:"):
        engine = engine[5:]
    engine = engine.split("+")[0] or guess_engine(raw)

    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    netloc_and_path = rest.split("?", 1)[0].split("#", 1)[0]
    if "/" in netloc_and_path:
        netloc, database = netloc_and_path.split("/", 1)
    else:
        netloc, database = netloc_and_path, ""
    if "," in netloc:                                        # RAC/多主机列表，取第一个即可保证稳定
        netloc = netloc.split(",")[0]
    if ":" in netloc and not netloc.startswith("["):
        host, port = netloc.rsplit(":", 1)
    else:
        host, port = netloc, ""

    default_ports = {"mysql": "3306", "postgres": "5432", "postgresql": "5432",
                     "oracle": "1521", "clickhouse": "8123", "sqlserver": "1433",
                     "dameng": "5236", "kingbase": "54321", "oceanbase": "2883"}
    port = port or default_ports.get(engine, "")
    host = host.strip("[]") or "localhost"
    return instance_id_from_parts(host, port, engine, database)


def guess_engine(text: str) -> str:
    low = (text or "").lower()
    for k in ("clickhouse", "oceanbase", "dameng", "kingbase", "gaussdb",
              "postgres", "postgresql", "oracle", "sqlserver", "mysql", "mariadb", "sqlite"):
        if k in low:
            return {"postgresql": "postgres", "mariadb": "mysql"}.get(k, k)
    return "unknown"


def redact_dsn(dsn: str) -> str:
    """日志/档案里出现连接串时一律脱敏。"""
    if not dsn:
        return ""
    redacted = re.sub(r"(?<=://)[^@/]*@(?=[^@/]+[:@])", "***@", re.sub(r"://[^@/]*@", "://***@", dsn))
    redacted = re.sub(r"(password\s*=\s*)[^\s;]+", r"\1***", redacted, flags=re.I)
    redacted = re.sub(r"(pwd\s*=\s*)[^\s;]+", r"\1***", redacted, flags=re.I)
    return redacted


# ---------------------------------------------------------------- SQL 指纹


_LITERAL_RE = re.compile(r"('(?:[^']|'')*')|(\"[^\"]*\")|(-?\b\d+(?:\.\d+)?\b)")
_WS_RE = re.compile(r"\s+")
_IN_RE = re.compile(r"\b(IN|VALUES)\s*\(([^)]*)\)", re.I)


def normalize_sql_for_fingerprint(sql: str) -> str:
    """把 SQL 归一化到可比较的形式：字面量替换为占位符、压缩空白、去掉注释。"""
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    # IN (...) 与 VALUES (...) 的所有取值统一折叠成一个占位，避免参数个数影响指纹
    s = _IN_RE.sub(lambda m: f"{m.group(1).upper()} (?)", s)
    s = _LITERAL_RE.sub("?", s)
    s = _WS_RE.sub(" ", s)
    s = s.strip().rstrip(";")
    # 多空格 ○ 统一
    s = re.sub(r"\s*([(),=<>+\-*/])\s*", r"\1", s)
    return s.strip().lower()


def sql_fingerprint(sql: str) -> str:
    return f"fp:{_sha(normalize_sql_for_fingerprint(sql), 20)}"


def table_fingerprint(database: str, table: str) -> str:
    return f"tb:{_sha(f'{database}.{table}'.lower(), 16)}"


def change_id(instance_id: str, sql: str) -> str:
    import time
    return f"chg-{int(time.time()):010d}-{_sha(instance_id + normalize_sql_for_fingerprint(sql), 8)}"


def correlation_id() -> str:
    import time
    import uuid
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:12]}"


def short(value: Any, n: int = 8) -> str:
    return _sha(str(value), n)
