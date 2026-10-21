"""冷启动采集器 —— 把「零历史的客户现场」变成「Tier1 可诊断状态」。

设计要点：
  * **只读**。采集过程一条写语句都不执行，这是客户愿意让你连生产库的前提。
  * **可降级**。任何一项采集失败都不影响其它项，失败项在 report 里显式标注 `unavailable`，
    绝不允许用 0 / 空 冒充"健康"。
  * **有限体量**。默认每项 1~20 行，避免把大实例的元数据全拉回来。
  * **可离线**。它也接受一个「人工粘贴的文本快照」（见 from_text），
    客户不敢给你连库权限时，让他执行一条脚本把结果贴回来照样能出报告。

采集完的 snapshot 是纯 JSON，直接进 profile-hub，第二次起就是 Tier1 → Tier2。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from dbcore import metrics as M
from dbcore import baselines as BL
from dbcore.ids import instance_id_from_dsn, redact_dsn


class CollectError(Exception):
    pass


# ------------------------------------------------------------------
# 各引擎的元数据采集语句（全部只读）
# ------------------------------------------------------------------

_MYSQL_COLLECT: dict[str, str] = {
    "version": "SELECT VERSION()",
    "params": "SHOW VARIABLES",
    "top_tables": (
        "SELECT table_schema, table_name, table_rows, "
        "ROUND((data_length+index_length)/1024/1024,1) AS size_mb, engine, table_collation "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('mysql','sys','information_schema','performance_schema') "
        "ORDER BY (data_length+index_length) DESC LIMIT 20"
    ),
    "tables_no_pk": (
        "SELECT t.table_schema, t.table_name, t.table_rows "
        "FROM information_schema.tables t "
        "LEFT JOIN information_schema.statistics s "
        "  ON s.table_schema=t.table_schema AND s.table_name=t.table_name AND s.index_name='PRIMARY' "
        "WHERE t.table_schema NOT IN ('mysql','sys','information_schema','performance_schema') "
        "  AND t.table_type='BASE TABLE' AND s.table_name IS NULL "
        "ORDER BY t.table_rows DESC LIMIT 20"
    ),
    "indexes": (
        "SELECT table_schema, table_name, index_name, "
        "GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols, non_unique, cardinality "
        "FROM information_schema.statistics "
        "WHERE table_schema NOT IN ('mysql','sys','information_schema','performance_schema') "
        "GROUP BY table_schema, table_name, index_name, non_unique, cardinality "
        "LIMIT 200"
    ),
    "redundant_indexes": (
        "SELECT table_schema, table_name, redundant_index_name, dominant_index_name, "
        "       redundant_index_columns "
        "FROM sys.schema_redundant_indexes LIMIT 30"
    ),
    "unused_indexes": (
        "SELECT object_schema, object_name, index_name "
        "FROM sys.schema_unused_indexes LIMIT 30"
    ),
    "engine_summary": (
        "SELECT engine, COUNT(*) AS tables, "
        "ROUND(SUM(data_length+index_length)/1024/1024,1) AS size_mb "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('mysql','sys','information_schema','performance_schema') "
        "GROUP BY engine"
    ),
    "active_sessions": (
        "SELECT id,user,host,db,command,time,state,left(info,180) AS info "
        "FROM information_schema.processlist "
        "WHERE command <> 'Sleep' AND time > 1 ORDER BY time DESC LIMIT 20"
    ),
    "innodb_trx": (
        "SELECT trx_id,trx_state,trx_started,trx_rows_locked,trx_rows_modified,"
        "TIMESTAMPDIFF(SECOND,trx_started,NOW()) AS dur, left(trx_query,180) AS q "
        "FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 20"
    ),
    "lock_waits": (
        "SELECT waiting_pid, waiting_query, blocking_pid, blocking_query "
        "FROM sys.innodb_lock_waits LIMIT 20"
    ),
    "replica_status": "SHOW REPLICA STATUS",
    "databases": (
        "SELECT schema_name, default_character_set_name, default_collation_name "
        "FROM information_schema.schemata"
    ),
}

_PG_COLLECT: dict[str, str] = {
    "version": "SELECT version()",
    "params": "SELECT name, setting, unit, source FROM pg_settings",
    "top_tables": (
        "SELECT schemaname, relname, n_live_tup AS table_rows, "
        "ROUND(pg_total_relation_size(relid)/1024/1024.0,1) AS size_mb, "
        "n_dead_tup, last_autovacuum, last_autoanalyze "
        "FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 20"
    ),
    "tables_no_pk": (
        "SELECT c.relname, c.reltuples::bigint AS table_rows FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_index i ON i.indrelid=c.oid AND i.indisprimary "
        "WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND i.indrelid IS NULL ORDER BY c.reltuples DESC LIMIT 20"
    ),
    "indexes": (
        "SELECT schemaname, tablename, indexname, indexdef, "
        "idx_scan AS scans, pg_size_pretty(pg_relation_size(indexrelid)) AS size "
        "FROM pg_stat_user_indexes ORDER BY idx_scan LIMIT 200"
    ),
    "unused_indexes": (
        "SELECT schemaname, relname, indexrelname, idx_scan, "
        "pg_size_pretty(pg_relation_size(indexrelid)) AS size "
        "FROM pg_stat_user_indexes WHERE idx_scan = 0 "
        "AND NOT indisunique ORDER BY pg_relation_size(indexrelid) DESC LIMIT 30"
    ),
    "bloat_candidates": (
        "SELECT schemaname, relname, n_dead_tup, n_live_tup, "
        "ROUND(100.0*n_dead_tup/NULLIF(n_live_tup+n_dead_tup,0),2) AS dead_pct "
        "FROM pg_stat_user_tables WHERE n_dead_tup > 1000 "
        "ORDER BY dead_pct DESC LIMIT 30"
    ),
    "active_sessions": (
        "SELECT pid,usename,state,EXTRACT(EPOCH FROM (now()-query_start))::int AS secs, "
        "wait_event_type, wait_event, left(query,180) AS info "
        "FROM pg_stat_activity WHERE state <> 'idle' AND pid <> pg_backend_pid() "
        "ORDER BY secs DESC LIMIT 20"
    ),
    "idle_in_transaction": (
        "SELECT pid,usename,state,xact_start,left(query,180) AS info "
        "FROM pg_stat_activity WHERE state = 'idle in transaction' LIMIT 20"
    ),
    "lock_waits": (
        "SELECT blocked.pid AS waiting_pid, left(blocked.query,120) AS waiting_query, "
        "blocking.pid AS blocking_pid, left(blocking.query,120) AS blocking_query "
        "FROM pg_stat_activity blocked "
        "JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid)) "
        "WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0 LIMIT 20"
    ),
    "replication": (
        "SELECT client_addr,state,sent_lsn,write_lsn,flush_lsn,replay_lsn,"
        "COALESCE(EXTRACT(EPOCH FROM write_lag),0) AS write_lag_s,"
        "COALESCE(EXTRACT(EPOCH FROM replay_lag),0) AS replay_lag_s "
        "FROM pg_stat_replication"
    ),
    "replication_slots": (
        "SELECT slot_name,active,restart_lsn,"
        "pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained "
        "FROM pg_replication_slots"
    ),
    "long_queries": (
        "SELECT pid,state,EXTRACT(EPOCH FROM (now()-query_start))::int AS secs,left(query,180) "
        "FROM pg_stat_activity WHERE state='active' AND query_start < now() - interval '5 seconds' "
        "ORDER BY secs DESC LIMIT 20"
    ),
}

COLLECTORS: dict[str, dict[str, str]] = {
    "mysql": _MYSQL_COLLECT,
    "mariadb": _MYSQL_COLLECT,
    "postgres": _PG_COLLECT,
    "gaussdb": _PG_COLLECT,
    "kingbase": _PG_COLLECT,
    "oceanbase": _MYSQL_COLLECT,
}

# 需要采集的逻辑指标
MYSQL_LOGICAL = [
    "connections_used", "connections_max", "threads_active", "slow_query_count",
    "tmp_disk_tables", "qps", "rows_read_per_sec", "rows_changed_per_sec",
    "cache_hit_ratio", "long_trx_count", "longest_trx_seconds",
    "lock_wait_count", "lock_wait_max_seconds", "table_count", "largest_table_rows", "uptime_seconds",
]
PG_LOGICAL = [
    "connections_used", "connections_max", "threads_active", "threads_sleep",
    "idle_in_transaction", "long_trx_count", "longest_trx_seconds", "lock_wait_count",
    "cache_hit_ratio", "index_hit_ratio", "table_count", "largest_table_rows",
    "dead_tuples_ratio", "repl_slot_lag_bytes", "qps", "rows_read_per_sec",
    "rows_changed_per_sec", "uptime_seconds",
]
LOGICAL_BY_DIALECT = {
    "mysql": MYSQL_LOGICAL, "mariadb": MYSQL_LOGICAL, "oceanbase": MYSQL_LOGICAL,
    "postgres": PG_LOGICAL, "gaussdb": PG_LOGICAL, "kingbase": PG_LOGICAL,
}


def _scalar(runner: Callable[[str], dict]) -> Any:
    def _run(sql: str) -> Any:
        try:
            res = runner(sql)
            if res.get("rows"):
                return res["rows"][0][0]
        except Exception:
            return None
        return None
    return _run


def collect_snapshot(
    url: str,
    dialect: str | None = None,
    sections: list[str] | None = None,
    max_rows: int = 200,
    runner: Callable[[str, str | None, int], dict] | None = None,
) -> dict:
    """一次性采集现场信息。返回结构化 snapshot（可进档案、可渲染报告）。"""
    from dbcore.connectors import ConnectionSpec, run_readonly  # 局部导入，保证无驱动时也能 import 本模块

    spec = ConnectionSpec(url)
    d = dialect or spec.driver
    if runner is None:
        def runner(sql, dl=d, mx=max_rows):  # type: ignore[misc]
            return run_readonly(url, sql, dl, max_rows=mx)

    started = time.time()
    snapshot: dict[str, Any] = {
        "schema": "dbt.snapshot/v1",
        "instance_id": instance_id_from_dsn(url),
        "dialect": d,
        "target": redact_dsn(url),
        "captured_at": int(started),
        "captured_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "sections": {},
        "errors": {},
        "metrics": {},
        "completed_sections": [],
        "failed_sections": [],
    }

    plan = COLLECTORS.get(d, {})
    if not plan:
        snapshot["errors"]["__all__"] = f"暂不支持对该方言做自动采集：{d}"
        return snapshot

    # 版本优先，后面很多判断依赖它
    version = None
    try:
        version = _scalar(runner)(plan["version"])
        snapshot["sections"]["version"] = version
        snapshot["completed_sections"].append("version")
    except Exception as exc:
        snapshot["errors"]["version"] = str(exc)

    want = [s for s in plan if s != "version"]
    if sections:
        want = [s for s in want if s in sections]
    for name in want:
        sql = plan[name]
        try:
            res = runner(sql, d, max_rows)
            if res.get("rows") and name in ("params",) and d == "mysql":
                snapshot["sections"][name] = {
                    str(r[0]): str(r[1]) for r in res["rows"]
                }
            elif name == "params" and res.get("rows"):
                snapshot["sections"][name] = {str(r[0]): str(r[1]) for r in res["rows"]}
            elif len(res.get("rows", [])) == 1 and len(res.get("columns", [])) == 1:
                snapshot["sections"][name] = res["rows"][0][0]
            else:
                snapshot["sections"][name] = {
                    "columns": res.get("columns", []),
                    "rows": res.get("rows", []),
                    "truncated": res.get("truncated", False),
                }
            snapshot["completed_sections"].append(name)
        except Exception as exc:
            snapshot["errors"][name] = f"{type(exc).__name__}: {exc}"
            snapshot["failed_sections"].append(name)

    # 逻辑指标
    for mname in LOGICAL_BY_DIALECT.get(d, []):
        src = M.get_source(d, mname)
        if not src:
            continue
        kind, target = src
        try:
            if kind in ("var", "status"):
                # SHOW STATUS 的结果统一先从 performerschema 取
                sql = (
                    f"SELECT variable_value FROM performance_schema.global_status "
                    f"WHERE variable_name='{target}'"
                )
                if d in ("mysql", "mariadb", "oceanbase") and target in (
                    "max_connections", "innodb_lock_wait_timeout", "long_query_time",
                    "innodb_buffer_pool_size", "innodb_io_capacity", "sync_binlog",
                    "innodb_flush_log_at_trx_commit",
                ):
                    sql = f"SHOW VARIABLES LIKE '{target}'"
                    res = runner(sql, d, 1)
                    val = res["rows"][0][1] if res.get("rows") else None
                else:
                    res = runner(sql, d, 1)
                    val = res["rows"][0][0] if res.get("rows") else None
            else:
                res = runner(target, d, 1)
                val = res["rows"][0][0] if res.get("rows") else None
            if val is not None:
                try:
                    snapshot["metrics"][mname] = float(val)
                except (TypeError, ValueError):
                    snapshot["metrics"][mname] = None
            else:
                snapshot["metrics"][mname] = None
        except Exception as exc:
            snapshot["metrics"][mname] = None
            snapshot["errors"].setdefault("metrics", {})[mname] = str(exc)[:200]

    snapshot["metrics"] = M.derive(snapshot["metrics"])
    # 派生到 number only，None 保留
    snapshot["version"] = version
    snapshot["version_major"] = _major_version(version, d)
    snapshot["took_ms"] = int((time.time() - started) * 1000)
    snapshot["tier"] = "tier1"
    return snapshot


def _major_version(version: Any, dialect: str) -> str | None:
    if not version:
        return None
    s = str(version)
    import re
    if dialect in ("postgres", "gaussdb", "kingbase"):
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        return m.group(1) if m else None
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", s)
    return m.group(1) if m else None


def snapshot_summary(snapshot: dict) -> dict:
    """从 snapshot 提炼一页摘要，用于报告和档案索引。"""
    metrics = snapshot.get("metrics") or {}
    secs = snapshot.get("sections") or {}
    d = snapshot.get("dialect", "mysql")

    def rows_of(name: str) -> list:
        v = secs.get(name)
        return v.get("rows", []) if isinstance(v, dict) else []

    top = rows_of("top_tables")[:5]
    top_tables = []
    for r in top:
        if len(r) >= 4:
            top_tables.append({"schema": r[0], "table": r[1], "rows": int(r[2] or 0),
                               "size_mb": float(r[3] or 0)})
    return {
        "instance_id": snapshot.get("instance_id"),
        "dialect": d,
        "version": snapshot.get("version"),
        "captured_at_iso": snapshot.get("captured_at_iso"),
        "tier": snapshot.get("tier", "tier1"),
        "metrics": metrics,
        "top_tables": top_tables,
        "table_count": int(metrics.get("table_count") or 0),
        "no_pk_tables": rows_of("tables_no_pk")[:10],
        "index_count": len(rows_of("indexes")),
        "redundant_indexes": len(rows_of("redundant_indexes")),
        "unused_indexes": len(rows_of("unused_indexes")),
        "failed_sections": snapshot.get("failed_sections", []),
        "errors": snapshot.get("errors", {}),
    }


# ------------------------------------------------------------------
# 离线模式：客户不给连库权限时的兜底
# ------------------------------------------------------------------


def from_text(text: str, dialect: str | None = None) -> dict:
    """把客户粘贴的文本结果解析成 snapshot。

    支持：
      * SHOW VARIABLES 的 tab 分隔输出
      * 带表头的查询结果（首行列名，后续制表符/竖线分隔）
      * `key = value` 形式的参数
    这是"客户不给连接权限"时的唯一交付路径，必须保留。
    """
    lines = [l for l in text.splitlines() if l.strip()]
    params: dict[str, str] = {}
    tables: list[list] = []
    header: list[str] | None = None
    version = None

    for line in lines:
        if "=" in line and "\t" not in line and "|" not in line and len(line.split()) <= 3:
            k, _, v = line.partition("=")
            params[k.strip()] = v.strip()
            continue
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        elif "|" in line:
            parts = [p.strip() for p in line.strip("|").split("|")]
        else:
            continue
        if header is None:
            header = parts
            continue
        tables.append(parts)
        if version is None and any(k.lower() in ("version", "version()") for k in header):
            version = " ".join(parts)

    snap: dict[str, Any] = {
        "schema": "dbt.snapshot/v1",
        "instance_id": f"offline:{abs(hash(text[:2000])) % 10**12}",
        "dialect": dialect or "unknown",
        "version": version,
        "captured_at": int(time.time()),
        "captured_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sections": {"params": params},
        "errors": {},
        "metrics": {},
        "completed_sections": ["params"] + (["__table__"] if tables else []),
        "failed_sections": [],
        "offline": True,
        "tier": "tier1",
    }
    if header and tables:
        snap["sections"]["__table__"] = {"columns": header, "rows": tables, "truncated": False}
    return snap
