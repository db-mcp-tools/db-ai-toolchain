"""安全连接：只读会话 + 语句超时 + 行数上限。

驱动为可选依赖，缺失时抛出清晰的安装提示而不是 ImportError 栈。
所有对目标库的语句都必须先过 dbcore.guard 的只读护栏。
"""

from __future__ import annotations

import os
import re

from dbcore.dialect import normalize_dialect
from dbcore.guard import assert_readonly

DEFAULT_TIMEOUT = int(os.environ.get("DBT_QUERY_TIMEOUT", "10"))
MAX_ROWS = int(os.environ.get("DBT_MAX_ROWS", "1000"))

_URL_RE = re.compile(
    r"^(?P<scheme>[a-zA-Z0-9+]+)://(?:(?P<user>[^:@/]*)(?::(?P<password>[^@/]*))?@)?"
    r"(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<db>[^?]*)"
)


class MissingDriver(Exception):
    pass


class ConnectionSpec:
    def __init__(self, url: str):
        m = _URL_RE.match(url.strip())
        if not m:
            raise ValueError("无法解析连接串，期望 mysql://user:pass@host:3306/db 或 postgres://...")
        self.url = url
        self.driver = normalize_dialect(m.group("scheme"))
        self.user = m.group("user") or ""
        self.password = m.group("password") or ""
        self.host = m.group("host")
        self.port = int(m.group("port") or (3306 if self.driver == "mysql" else 5432))
        self.database = m.group("db")

    @property
    def instance_id(self) -> str:
        return f"{self.driver}://{self.host}:{self.port}/{self.database}"


def _connect(spec: ConnectionSpec):
    if spec.driver == "mysql":
        try:
            import pymysql  # type: ignore
        except ImportError as e:
            raise MissingDriver("需要 pymysql：pip install pymysql") from e
        conn = pymysql.connect(
            host=spec.host, port=spec.port, user=spec.user, password=spec.password,
            database=spec.database, connect_timeout=5, read_timeout=DEFAULT_TIMEOUT, charset="utf8mb4",
        )
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cur.execute("SET SESSION MAX_EXECUTION_TIME=%s", (DEFAULT_TIMEOUT * 1000,))
        return conn

    if spec.driver == "postgres":
        dsn = f"host={spec.host} port={spec.port} dbname={spec.database} user={spec.user} password={spec.password}"
        try:
            import psycopg  # type: ignore

            return psycopg.connect(dsn)
        except ImportError:
            pass
        try:
            import psycopg2  # type: ignore

            return psycopg2.connect(dsn)
        except ImportError as e:
            raise MissingDriver("需要 psycopg：pip install \"psycopg[binary]\"") from e

    raise ValueError(f"暂不支持的连接类型：{spec.driver}")


def _rows(result: str | int | float | None) -> list:
    return result if isinstance(result, list) else []


def run_readonly(url: str, sql: str, dialect: str | None = None, max_rows: int = MAX_ROWS) -> dict:
    """执行只读语句（SELECT / SHOW / EXPLAIN），返回结构化结果。"""
    spec = ConnectionSpec(url)
    d = dialect or spec.driver
    assert_readonly(sql, d)

    conn = _connect(spec)
    try:
        with conn.cursor() as cur:
            if spec.driver == "postgres":
                cur.execute("SET TRANSACTION READ ONLY")
                # 注意：PostgreSQL 的 SET 不接受服务端参数绑定（$1），
                # psycopg 3 会将其绑成 $1 并报错；timeout 是受控整数常量，
                # 直接内联字面量，避免对 psycopg2/psycopg3 的兼容差异。
                cur.execute(f"SET LOCAL statement_timeout = {int(DEFAULT_TIMEOUT * 1000)}")
            cur.execute(sql)
            if cur.description is None:
                return {"instance_id": spec.instance_id, "columns": [], "rows": [], "truncated": False}
            cols = [c[0] for c in cur.description]
            raw = cur.fetchall()
            limited = raw[:max_rows]
            rows = []
            for r in limited:
                rows.append([v if isinstance(v, (str, int, float, type(None))) else str(v) for v in r])
            return {
                "instance_id": spec.instance_id,
                "columns": cols,
                "rows": rows,
                "truncated": len(raw) > len(limited),
            }
    finally:
        conn.close()


def precheck_change_window(url: str, dialect: str | None = None, long_tx_seconds: int = 60) -> dict:
    """变更窗口前置检查：长事务、复制延迟、关键参数。输出 go / no-go 结论。"""
    spec = ConnectionSpec(url)
    d = dialect or spec.driver
    checks: list[dict] = []

    def _scalar(sql: str) -> float | None:
        r = run_readonly(url, sql, d, max_rows=1)
        if not r["rows"]:
            return None
        v = r["rows"][0][0]
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def add(name: str, value: object, status: str, advice: str) -> None:
        checks.append({"name": name, "value": value, "status": status, "advice": advice})

    def check(name: str, sql: str, ok, bad, advice: str) -> None:
        try:
            value = _scalar(sql)
        except Exception as exc:
            add(name, None, "unknown", f"检查失败：{exc}")
            return
        if value is None:
            add(name, None, "unknown", "未取到指标（无权限或版本不支持）")
            return
        status = "ok" if ok(value) else ("critical" if bad(value) else "warn")
        add(name, value, status, advice if status != "ok" else "")

    if d == "mysql":
        check(
            "长事务数",
            f"SELECT COUNT(*) FROM information_schema.innodb_trx "
            f"WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > {int(long_tx_seconds)}",
            ok=lambda v: v == 0,
            bad=lambda v: v >= 5,
            advice="长事务会卡住 DDL 的元数据锁并堆积后续会话，需等待或 kill",
        )
        try:
            lag = None
            status_sql = "SHOW REPLICA STATUS"
            try:
                replica = run_readonly(url, status_sql, d, max_rows=1)
            except Exception:
                replica = run_readonly(url, "SHOW SLAVE STATUS", d, max_rows=1)
            if replica["rows"]:
                pairs = dict(zip(replica["columns"], replica["rows"][0]))
                raw_lag = pairs.get("Seconds_Behind_Master") or pairs.get("Seconds_Behind_Source")
                lag = None if raw_lag is None else float(raw_lag)
            if lag is None:
                add("从库延迟秒", None, "unknown", "非副本实例或未取到复制状态")
            else:
                status = "ok" if lag < 5 else ("critical" if lag >= 30 else "warn")
                add("从库延迟秒", lag, status, "延迟过高时在线 DDL 会被级联放大" if status != "ok" else "")
        except Exception as exc:
            add("从库延迟秒", None, "unknown", f"检查失败：{exc}")
        check(
            "online_alter_log 上限(MB)",
            "SELECT ROUND(@@innodb_online_alter_log_max_size/1024/1024)",
            ok=lambda v: v >= 128,
            bad=lambda v: v < 128,
            advice="过小会导致大表在线 DDL 中途失败并回滚已耗时的中间状态",
        )
    else:
        check(
            "长事务数",
            f"SELECT COUNT(*) FROM pg_stat_activity "
            f"WHERE backend_xid IS NOT NULL AND xact_start < now() - interval '{int(long_tx_seconds)} seconds'",
            ok=lambda v: v == 0,
            bad=lambda v: v >= 5,
            advice="CREATE INDEX CONCURRENTLY 会等待所有旧事务结束；长事务会让它无限期挂起",
        )
        check(
            "复制延迟秒",
            "SELECT COALESCE(EXTRACT(EPOCH FROM MAX(replay_lag)),0) FROM pg_stat_replication",
            ok=lambda v: v < 5,
            bad=lambda v: v >= 30,
            advice="复制延迟期间做大表重建会进一步拉大延迟",
        )
        check(
            "lock_timeout(ms)",
            "SELECT setting::float FROM pg_settings WHERE name='lock_timeout'",
            ok=lambda v: v > 0,
            bad=lambda v: False,
            advice="建议设置 lock_timeout > 0，避免 DDL 无限等待堆积会话",
        )

    blocking = [c for c in checks if c["status"] in ("warn", "critical")]
    return {
        "instance_id": spec.instance_id,
        "checks": checks,
        "go": not blocking,
        "verdict": "可执行变更窗口" if not blocking else "不建议执行，先处理上述告警项",
    }


def explain(url: str, sql: str, dialect: str | None = None, analyze: bool = False) -> dict:
    """取执行计划。EXPLAIN 本身只读，内部 SQL 也必须先过只读护栏。"""
    spec = ConnectionSpec(url)
    d = dialect or spec.driver
    assert_readonly(sql, d)
    plan = run_readonly(url, ("EXPLAIN ANALYZE " if analyze else "EXPLAIN ") + sql.rstrip(";"), d, max_rows=200)
    flat = " | ".join(" ".join("" if c is None else str(c) for c in row) for row in plan["rows"])
    issues: list[str] = []
    low = flat.lower()
    if "seq scan" in low or "using filesort" in low or "using temporary" in low or "full scan" in low:
        issues.append("执行计划含全表扫描 / filesort / 临时表，需结合数据量判断是否可接受")
    if "rows=0" in low.replace(" ", ""):
        issues.append("估算行数为 0，统计信息可能过期，建议先收集统计信息再判断")
    plan["plan_text"] = flat[:4000]
    plan["issues"] = issues
    plan["instance_id"] = spec.instance_id
    return plan
