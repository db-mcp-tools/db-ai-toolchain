"""指标归一化 —— 让不同引擎的同一个概念拥有同一个名字。

MySQL 叫 Threads_running，PG 叫 pg_stat_activity(state='active')，Oracle 叫 sessions_active。
如果每个 MCP 各用各的名字，跨引擎的规则就不能共用，档案也不可迁移。

所以要有一层逻辑指标 -> 引擎实现的映射：

    logical_name            说明                        单位
    ---------------------------------------------------------------
    cpu_used_pct            CPU 使用率                  %
    memory_used_pct         内存使用率                  %
    connections_used        当前连接数                  count
    connections_max         连接上限                    count
    threads_active          活跃会话数                  count
    threads_sleep           空闲会话数                  count
    long_trx_count          长事务数                    count
    longest_trx_seconds     最长事务运行时长             s
    lock_wait_count         锁等待数                    count
    repl_lag_seconds        复制延迟                    s
    cache_hit_ratio         缓存命中率                  %
    disk_read_iops          读 IOPS                    ops/s
    disk_util_pct           磁盘利用率                  %
    slow_query_count        慢查询数                    count
    tmp_disk_tables         落盘临时表数                count
    rows_read_per_sec       每秒读取行数                rows/s
    rows_changed_per_sec    每秒变更行数                rows/s
    deadlocks               死锁次数                    count
    table_count             表数量                      count
    dead_tuples_ratio       PG 死元组占比               %
    repl_slot_lag_bytes     PG 复制槽积压               bytes

设计约束：**没有采到就是 None，不允许填 0**。
  0 是有意义的真实值（比如 0 个死锁），None 表示"不知道"，
  把不知道当成 0 会让下游规则误判为"健康"。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricDef:
    name: str
    unit: str
    desc: str
    higher_is_worse: bool = False
    default_warn: float | None = None
    default_critical: float | None = None
    # kind: gauge=瞬时值可直接用；counter=引擎侧累计值（自启动以来），
    #       直接当瞬时指标用是正确性错误，必须先经 diff_raw_counters() 差分。
    kind: str = "gauge"


LOGICAL_METRICS: dict[str, MetricDef] = {m.name: m for m in [
    MetricDef("cpu_used_pct", "%", "主机 CPU 使用率", True, 80, 95),
    MetricDef("memory_used_pct", "%", "主机内存使用率", True, 85, 95),
    MetricDef("disk_used_pct", "%", "磁盘空间使用率", True, 85, 95),
    MetricDef("disk_util_pct", "%", "磁盘利用率", True, 80, 95),
    MetricDef("iowait_pct", "%", "CPU 等待 IO 占比", True, 20, 40),
    MetricDef("connections_used", "count", "当前已建立连接数", True, None, None),
    MetricDef("connections_max", "count", "最大连接数", False),
    MetricDef("connections_used_pct", "%", "连接使用率", True, 80, 95),
    MetricDef("threads_active", "count", "活跃会话数", True, 50, 200),
    MetricDef("threads_sleep", "count", "空闲会话数", True),
    MetricDef("long_trx_count", "count", "运行超过阈值的事务数", True, 1, 10),
    MetricDef("longest_trx_seconds", "s", "最长事务已运行时长", True, 30, 300),
    MetricDef("idle_in_transaction", "count", "事务中空闲会话数", True, 1, 10),
    MetricDef("lock_wait_count", "count", "当前锁等待数", True, 1, 20),
    MetricDef("lock_wait_max_seconds", "s", "最长锁等待时长", True, 3, 30),
    MetricDef("deadlocks", "count", "死锁累计次数", True, 1, 10, kind="counter"),
    MetricDef("repl_lag_seconds", "s", "复制延迟", True, 10, 60),
    MetricDef("repl_io_running", "bool", "IO 线程是否运行", False),
    MetricDef("repl_sql_running", "bool", "SQL 线程是否运行", False),
    MetricDef("cache_hit_ratio", "%", "缓存命中率", False),
    MetricDef("index_hit_ratio", "%", "索引缓存命中率", False),
    MetricDef("slow_query_count", "count", "慢查询累计次数", True, 1, 100, kind="counter"),
    MetricDef("tmp_disk_tables", "count", "落盘临时表累计", True, 1, 1000, kind="counter"),
    MetricDef("tmp_disk_ratio", "%", "临时表落盘占比", True, 25, 50),
    MetricDef("rows_read_per_sec", "rows/s", "每秒读取行数", True, kind="counter"),
    MetricDef("rows_changed_per_sec", "rows/s", "每秒变更行数", True, kind="counter"),
    MetricDef("qps", "qps", "每秒查询数", False, kind="counter"),
    MetricDef("disk_read_iops", "iops", "读 IOPS", True, kind="counter"),
    MetricDef("disk_write_iops", "iops", "写 IOPS", True, kind="counter"),
    MetricDef("table_count", "count", "用户表数量", False),
    MetricDef("largest_table_rows", "rows", "最大表行数", True),
    MetricDef("dead_tuples_ratio", "%", "PG 死元组占比", True, 20, 40),
    MetricDef("repl_slot_lag_bytes", "bytes", "PG 复制槽积压", True, 16 * 1024 * 1024, 1024 * 1024 * 1024),
    MetricDef("uptime_seconds", "s", "运行时长", False),
]}


def is_counter(name: str) -> bool:
    m = LOGICAL_METRICS.get(name)
    return bool(m and m.kind == "counter")


# counter 类指标：速率指标名 -> 引擎原始累计变量的名字。
# 采集时读的是原始累计值；只有经 diff_raw_counters() 差分后，
# 才允许以速率指标名（qps / rows_read_per_sec / …）进入观测与告警链路。
COUNTER_ALIASES: dict[str, str] = {
    "qps": "Queries",
    "rows_read_per_sec": "Innodb_rows_read",
    "rows_changed_per_sec": "Innodb_rows_updated",
    "slow_query_count": "Slow_queries",
    "tmp_disk_tables": "Created_tmp_disk_tables",
    "deadlocks": "Innodb_deadlocks",
}


# 逻辑指标 -> (SQL 或 status 变量取值表达式)
#   kind=var : SHOW VARIABLES / SHOW STATUS 里的名字
#   kind=sql : 一条 SELECT，返回值取第一个列的标量
MYSQL_SOURCES: dict[str, tuple[str, str]] = {
    "connections_used": ("var", "Threads_connected"),
    "connections_max": ("var", "max_connections"),
    "threads_active": ("var", "Threads_running"),
    "threads_sleep": ("var", "Threads_cached"),
    "slow_query_count": ("counter", "Slow_queries"),
    "tmp_disk_tables": ("counter", "Created_tmp_disk_tables"),
    "qps": ("counter", "Queries"),
    "rows_read_per_sec": ("counter", "Innodb_rows_read"),
    "rows_changed_per_sec": ("counter", "Innodb_rows_updated"),
    "deadlocks": ("counter", "Innodb_deadlocks"),
    "cache_hit_ratio": ("sql",
        "SELECT ROUND((1 - (SELECT variable_value FROM performance_schema.global_status "
        "WHERE variable_name='Innodb_buffer_pool_reads') / NULLIF((SELECT variable_value FROM "
        "performance_schema.global_status WHERE variable_name='Innodb_buffer_pool_read_requests'),0)) * 100, 4) "
        "AS hit_ratio"),
    "long_trx_count": ("sql",
        "SELECT COUNT(*) FROM information_schema.innodb_trx "
        "WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 30"),
    "longest_trx_seconds": ("sql",
        "SELECT IFNULL(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW())),0) FROM information_schema.innodb_trx"),
    "lock_wait_count": ("sql", "SELECT COUNT(*) FROM performance_schema.data_lock_waits"),
    "lock_wait_max_seconds": ("sql",
        "SELECT IFNULL(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW())),0) "
        "FROM information_schema.innodb_trx WHERE trx_state='LOCK WAIT'"),
    "table_count": ("sql",
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema NOT IN "
        "('mysql','sys','information_schema','performance_schema')"),
    "largest_table_rows": ("sql",
        "SELECT IFNULL(MAX(table_rows),0) FROM information_schema.tables WHERE table_schema NOT IN "
        "('mysql','sys','information_schema','performance_schema')"),
    "uptime_seconds": ("status", "Uptime"),
}

POSTGRES_SOURCES: dict[str, tuple[str, str]] = {
    "connections_used": ("sql", "SELECT COUNT(*) FROM pg_stat_activity"),
    "connections_max": ("sql", "SHOW max_connections"),
    "threads_active": ("sql", "SELECT COUNT(*) FROM pg_stat_activity WHERE state='active'"),
    "threads_sleep": ("sql", "SELECT COUNT(*) FROM pg_stat_activity WHERE state='idle'"),
    "idle_in_transaction": ("sql", "SELECT COUNT(*) FROM pg_stat_activity WHERE state='idle in transaction'"),
    "long_trx_count": ("sql",
        "SELECT COUNT(*) FROM pg_stat_activity WHERE xact_start IS NOT NULL "
        "AND xact_start < now() - interval '30 seconds'"),
    "longest_trx_seconds": ("sql",
        "SELECT COALESCE(MAX(EXTRACT(EPOCH FROM (now() - xact_start))),0)::int "
        "FROM pg_stat_activity WHERE xact_start IS NOT NULL"),
    "lock_wait_count": ("sql", "SELECT COUNT(*) FROM pg_locks WHERE NOT granted"),
    "cache_hit_ratio": ("sql",
        "SELECT ROUND(100 * sum(blks_hit) / NULLIF(sum(blks_hit) + sum(blks_read),0), 4) "
        "FROM pg_stat_database"),
    "index_hit_ratio": ("sql",
        "SELECT ROUND(100 * sum(idx_blks_hit) / NULLIF(sum(idx_blks_hit) + sum(idx_blks_read),0), 4) "
        "FROM pg_statio_user_indexes"),
    "table_count": ("sql", "SELECT COUNT(*) FROM pg_stat_user_tables"),
    "largest_table_rows": ("sql", "SELECT COALESCE(MAX(n_live_tup),0) FROM pg_stat_user_tables"),
    "dead_tuples_ratio": ("sql",
        "SELECT ROUND(100 * SUM(n_dead_tup) / NULLIF(SUM(n_live_tup + n_dead_tup),0), 4) "
        "FROM pg_stat_user_tables"),
    "repl_slot_lag_bytes": ("sql",
        "SELECT COALESCE(SUM(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)),0)::bigint "
        "FROM pg_replication_slots WHERE active"),
    "qps": ("counter", "SELECT SUM(xact_commit + xact_rollback) FROM pg_stat_database"),
    "rows_read_per_sec": ("counter", "SELECT SUM(tup_returned + tup_fetched) FROM pg_stat_database"),
    "rows_changed_per_sec": ("counter", "SELECT SUM(tup_inserted + tup_updated + tup_deleted) FROM pg_stat_database"),
    "uptime_seconds": ("sql",
        "SELECT EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))::int"),
}

SOURCES = {
    "mysql": MYSQL_SOURCES,
    "mariadb": MYSQL_SOURCES,
    "postgres": POSTGRES_SOURCES,
    "gaussdb": POSTGRES_SOURCES,
    "kingbase": POSTGRES_SOURCES,
    "oceanbase": MYSQL_SOURCES,
}


def list_metrics() -> list[dict]:
    return [
        {"name": m.name, "unit": m.unit, "desc": m.desc,
         "kind": m.kind,
         "higher_is_worse": m.higher_is_worse,
         "default_warn": m.default_warn, "default_critical": m.default_critical}
        for m in LOGICAL_METRICS.values()
    ]


# ---------------------------------------------------------------- 差分管道 --
def diff_counter(prev: float | None, now: float | None, dt: float) -> tuple[float | None, str | None]:
    """把一个原始累计值差分为速率。

    返回 (rate, warning)。三种结果：
      rate      正常速率
      None, None          上一个点不存在（首次观测），无可差分
      None, "restart"     now < prev：实例在两次观测间重启过，该点作废
      None, "bad_dt"      时间间隔非法（<=0）
    """
    if prev is None or now is None:
        return None, None
    if dt <= 0:
        return None, "bad_dt"
    if now < prev:
        return None, "restart"
    return round((now - prev) / dt, 4), None


def diff_raw_counters(prev_raw: dict[str, float] | None, now_raw: dict[str, float],
                      dt: float, names: list[str] | None = None) -> tuple[dict[str, float], list[str]]:
    """批量差分：原始累计变量快照 -> 速率指标。

    prev_raw / now_raw 以 COUNTER_ALIASES 的键（速率指标名）为键存原始累计值。
    返回 (rates, warnings)；重启过的指标不出现在 rates 里，并记 warning。
    """
    rates: dict[str, float] = {}
    warns: list[str] = []
    for name in (names or list(COUNTER_ALIASES)):
        prev = (prev_raw or {}).get(name)
        now = now_raw.get(name)
        r, w = diff_counter(prev, now, dt)
        if r is not None:
            rates[name] = r
        if w:
            warns.append(f"{name}: {w}（该窗口速率作废）")
    return rates, warns


def get_source(dialect: str, name: str) -> tuple[str, str] | None:
    return SOURCES.get(dialect, {}).get(name)


def supported_metrics(dialect: str) -> list[str]:
    return sorted(SOURCES.get(dialect, {}))


def derive(metrics: dict[str, float]) -> dict[str, float]:
    """从已采集的指标推导派生指标（尽量少占用一次查询）。"""
    out = dict(metrics)
    used, mx = metrics.get("connections_used"), metrics.get("connections_max")
    if used is not None and mx:
        out["connections_used_pct"] = round(used / mx * 100, 2)
    tcreated = metrics.get("tmp_disk_tables")
    ttotal = metrics.get("tmp_tables")
    if tcreated is not None and ttotal:
        out["tmp_disk_ratio"] = round(tcreated / ttotal * 100, 2)
    return out


def evaluate(metrics: dict[str, float], overrides: dict[str, tuple] | None = None) -> list[dict]:
    """按阈值逐条评估，返回告警清单。取不到值的指标跳过（绝不当成健康）。"""
    alerts = []
    for name, val in metrics.items():
        if val is None:
            continue
        m = LOGICAL_METRICS.get(name)
        warn = crit = None
        if overrides and name in overrides:
            warn, crit = overrides[name]
        elif m:
            warn, crit = m.default_warn, m.default_critical
        if warn is None:
            continue
        if m and not m.higher_is_worse:                  # 命中率类：越低越糟
            if val < crit if crit else False:
                alerts.append({"metric": name, "value": val, "level": "critical",
                               "threshold": crit, "direction": "below"})
            elif val < warn:
                alerts.append({"metric": name, "value": val, "level": "warn",
                               "threshold": warn, "direction": "below"})
            continue
        if crit is not None and val >= crit:
            alerts.append({"metric": name, "value": val, "level": "critical",
                           "threshold": crit, "direction": "above"})
        elif val >= warn:
            alerts.append({"metric": name, "value": val, "level": "warn",
                           "threshold": warn, "direction": "above"})
    return alerts
