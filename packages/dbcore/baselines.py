"""静态基线（Tier0 知识层 · 其一）。

这个文件存在的唯一理由：**回答冷启动问题。**

客户的档案是本仓最有价值的资产，但新客户第一天一定没有档案。
如果所有诊断都依赖历史，第一单就卖不出去。所以必须有一层「不依赖任何历史、当天就能用」的知识：

  * 各引擎/版本的参数默认值与推荐区间
  * 业界共识的健康度阈值（命中率、临时表落盘率、锁等待、复制延迟……）
  * 硬件/规格的经验上限（连接数、IOPS、批量大小）
  * 版本差异（MySQL 5.7 vs 8.0 online DDL、PG 各版本并行度）

这些值是**可维护的知识资产**，不是散落在代码里的魔法数字。
任何规则/诊断引用阈值时必须从这里取，并在 evidence 里写清 source，便于追溯和修订。

维护约定：
  * 每条阈值带 `why`（为什么是这个值）和 `ref`（依据来源类型）
  * 不确定或有争议的区间给 `[lo, hi]` 而不是单点，避免假装精确
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Threshold:
    """一个带依据的阈值/区间。"""

    value: float | tuple[float, float]
    unit: str
    why: str
    ref: str = "industry_practice"  # industry_practice | official_doc | derived


# --------------------------------------------------------------------------
# 1. MySQL / MariaDB
# --------------------------------------------------------------------------

MYSQL = {
    # ---- 连接与线程 ----
    "connections_used_pct": Threshold(80.0, "%",
        "已用连接占 max_connections 比例持续 >80% 通常是连接泄漏或慢查询堆积的早期信号，等到 100% 已经无法登录",
        "derived"),
    "threads_running_high": Threshold((50, 200), "threads",
        "Threads_running 基线通常是个位数到几十；超过 50 说明并发堆积，需立刻看慢查询与锁",
        "industry_practice"),
    "max_connections_recommended": Threshold((500, 2000), "connections",
        "过大（>5000）会因线程上下文切换与内存（每连接 thread_stack+buffer）反而降低吞吐",
        "industry_practice"),
    "aborted_connects_pct": Threshold(1.0, "%",
        "连接失败占比超过 1% 通常指向密码错误/网络抖动/max_connect_errors/连接池配置错误",
        "derived"),

    # ---- InnoDB ----
    "buffer_pool_hit_ratio": Threshold(99.0, "%",
        "OLTP 场景 InnoDB 缓冲池命中率低于 99% 通常意味着热数据集大于内存或有大量全表扫描",
        "industry_practice"),
    "buffer_pool_size_pct": Threshold((60, 75), "% of RAM",
        "独占机器上 60~75% 是常见区间；超过 80% 会与 OS page cache、连接内存争抢",
        "official_doc"),
    "log_wait_flush_pct": Threshold(1.0, "%",
        "redo 刷盘等待占比高说明 innodb_log_file_size 偏小或磁盘 fsync 能力不足",
        "derived"),
    "row_lock_wait_avg_ms": Threshold(100.0, "ms",
        "行锁平均等待超过 100ms 在 OLTP 里已经能被业务感知（TP99 抬升）",
        "derived"),
    "lock_wait_timeout_suggest": Threshold((10, 30), "s",
        "默认 50s 太长：持有锁的事务会拖垮上游；互联网业务常用 10~30s 让失败快速暴露并重试",
        "industry_practice"),
    "innodb_io_capacity_hdd": Threshold(200, "iops", "机械盘经验值", "industry_practice"),
    "innodb_io_capacity_ssd": Threshold(2000, "iops", "SATA SSD 经验起点", "industry_practice"),
    "innodb_io_capacity_nvme": Threshold(10000, "iops", "NVMe 经验起点", "industry_practice"),

    # ---- 临时表与排序 ----
    "tmp_table_disk_pct": Threshold(25.0, "%",
        "Created_tmp_disk_tables / Created_tmp_tables 超过 25% 说明 sort_buffer/tmp_table_size 偏小或 SQL 需要优化",
        "industry_practice"),
    "sort_merge_passes_per_sec": Threshold(1.0, "/s",
        "Sort_merge_passes 增长说明排序无法在内存中完成，需调 sort_buffer_size 或改 SQL",
        "derived"),

    # ---- 复制 ----
    "repl_lag_warn_s": Threshold(10, "s", "读写分离场景，10s 延迟会导致明显的读己之写不一致", "industry_practice"),
    "repl_lag_critical_s": Threshold(60, "s", "超过 1 分钟的延迟通常已影响业务正确性", "industry_practice"),
    "repl_sql_thread_single": Threshold(1, "workers",
        "MySQL 5.6/5.7 默认单线程回放，是主库大事务导致延迟的首要原因；8.0 可用 replica_parallel_workers",
        "official_doc"),

    # ---- 慢查询 ----
    "long_query_time_default": Threshold(10.0, "s", "默认值，对 OLTP 几乎无诊断价值", "official_doc"),
    "long_query_time_recommended": Threshold((0.5, 2.0), "s", "OLTP 建议 0.5~2s", "industry_practice"),
    "slow_query_rate_pct": Threshold(1.0, "% of queries",
        "慢查询占比超过 1% 说明存在系统性问题而非个别 SQL", "derived"),

    # ---- 表与索引 ----
    "table_rows_large": Threshold(10_000_000, "rows", "超过千万行后 DDL 与全表扫描成本显著上升", "derived"),
    "table_rows_huge": Threshold(100_000_000, "rows", "过亿行的表，DDL 必须使用在线工具", "derived"),
    "table_size_large_gb": Threshold(50, "GB", "单表（含索引）超过 50GB 需要考虑分区或归档", "industry_practice"),
    "index_count_per_table": Threshold(6, "indexes", "单表索引超过 6 个写入放大明显，需检查冗余", "derived"),
    "index_selectivity_min": Threshold(0.1, "ratio",
        "选择性低于 10% 的索引，优化器通常不会选择，白占写入成本", "derived"),
    "redundant_index_overlap": Threshold(2, "indexes", "存在前缀重复即视为冗余候选", "derived"),

    # ---- 批量操作 ----
    "batch_size_max": Threshold(5000, "rows/batch",
        "单批超过 5000 行会产生大事务：锁持有时间长、undo 膨胀、主从延迟", "derived"),
    "batch_sleep_ms": Threshold((50, 200), "ms", "批量循环每批之间的建议休眠，避免持续打满主库", "industry_practice"),
    "transaction_rows_max": Threshold(100_000, "rows", "单事务影响行数上限建议", "derived"),
    "transaction_duration_s": Threshold(10, "s", "单个事务执行超过 10s 即视为长事务", "industry_practice"),
    "autocommit_off_risk": Threshold(1, "flag", "显式关闭 autocommit 且未提交是最常见的长事务来源", "derived"),
}

# MySQL 版本差异（用于 baseline 选择）
MYSQL_VERSIONS = {
    "5.6": {"online_ddl_add_index": False, "instant_add_column": False, "json": False, "window_func": False,
            "histogram": False, "parallel_replica": False, "cte": False},
    "5.7": {"online_ddl_add_index": True, "instant_add_column": False, "json": True, "window_func": False,
            "histogram": False, "parallel_replica": False, "cte": True},
    "8.0": {"online_ddl_add_index": True, "instant_add_column": True, "json": True, "window_func": True,
            "histogram": True, "parallel_replica": True, "cte": True},
    "8.4": {"online_ddl_add_index": True, "instant_add_column": True, "json": True, "window_func": True,
            "histogram": True, "parallel_replica": True, "cte": True},
}

# --------------------------------------------------------------------------
# 2. PostgreSQL
# --------------------------------------------------------------------------

POSTGRES = {
    "shared_buffers_pct": Threshold((15, 25), "% of RAM",
        "PG 依赖 OS page cache，shared_buffers 超过 25% 会造成双重缓存且收益递减", "official_doc"),
    "cache_hit_ratio": Threshold(99.0, "%",
        "pg_statio 命中率低于 99% 通常需要扩容内存或优化随机 IO", "industry_practice"),
    "index_cache_hit_ratio": Threshold(99.0, "%", "索引命中率低于此值说明索引大于可用缓存", "industry_practice"),
    "connections_used_pct": Threshold(80.0, "%", "PG 每连接成本高于 MySQL线程模型，务必配连接池", "derived"),
    "max_connections_recommended": Threshold((100, 400), "connections",
        "PG 建议配合 pgbouncer，直连超过 400 通常吃力", "official_doc"),
    "long_running_query_s": Threshold(60, "s", "超过 1 分钟的查询需介入", "derived"),
    "idle_in_transaction_s": Threshold(60, "s",
        "idle in transaction 会阻止 vacuum 回收死元组，是最隐蔽的问题", "official_doc"),
    "idle_in_transaction_max": Threshold(10, "sessions", "存在即应告警", "derived"),
    "bloat_warn_pct": Threshold(20.0, "%", "表膨胀超过 20% 建议 VACUUM FULL / pg_repack", "industry_practice"),
    "bloat_critical_pct": Threshold(40.0, "%", "严重膨胀，索引扫描退化", "industry_practice"),
    "dead_tuples_pct": Threshold(20.0, "%", "死元组占比，触发 autovacuum 的经验阈值", "official_doc"),
    "autovacuum_lag_transactions": Threshold(1000, "xacts", "距离冻结的事务数落后量告警阈值", "derived"),
    "seq_scan_rows_threshold": Threshold(10_000, "rows", "顺序扫描返回行数超过此值需检查索引", "derived"),
    "repl_lag_warn_bytes": Threshold(16 * 1024 * 1024, "bytes", "物理复制延迟 16MB 告警起点", "derived"),
    "repl_lag_warn_s": Threshold(10, "s", "逻辑复制/流复制时间延迟告警阈值", "industry_practice"),
    "work_mem_default": Threshold(4, "MB",
        "默认 4MB；注意这是每个排序节点、每个连接有机会申请的上限，不是全局上限", "official_doc"),
    "temp_file_size_warn_mb": Threshold(1024, "MB", "单次查询落盘超过 1GB 说明 work_mem 不足或 SQL 有问题", "derived"),
    "table_rows_large": Threshold(10_000_000, "rows", "大表阈值", "derived"),
    "table_bloat_age_days": Threshold(30, "days", "长期未 vacuum 的表", "derived"),
}

POSTGRES_VERSIONS = {
    "9.6": {"parallel_query": True, "partition_native": False, "procedure": False, "generated_column": False},
    "10": {"parallel_query": True, "partition_native": True, "procedure": False, "generated_column": False},
    "11": {"parallel_query": True, "partition_native": True, "procedure": True, "generated_column": True},
    "12": {"parallel_query": True, "partition_native": True, "procedure": True, "generated_column": True},
    "13": {"parallel_query": True, "partition_native": True, "procedure": True, "generated_column": True},
    "14": {"parallel_query": True, "partition_native": True, "procedure": True, "generated_column": True},
    "15": {"parallel_query": True, "partition_native": True, "procedure": True, "generated_column": True},
    "16": {"parallel_query": True, "partition_native": True, "procedure": True, "generated_column": True},
}

# --------------------------------------------------------------------------
# 3. Oracle
# --------------------------------------------------------------------------

ORACLE = {
    "buffer_cache_hit_ratio": Threshold(90.0, "%", "OLTP 命中率低于 90% 需关注", "industry_practice"),
    "library_cache_hit_ratio": Threshold(95.0, "%", "低于此值说明硬解析过多，绑定变量使用不足", "industry_practice"),
    "hard_parse_per_sec": Threshold(20, "/s", "硬解析过高通常是未使用绑定变量", "derived"),
    "row_lock_wait_s": Threshold(3, "s", "行锁等待超过 3s 需介入", "derived"),
    "temp_tablespace_used_pct": Threshold(85.0, "%", "临时表空间接近打满会导致 ORA-1652", "derived"),
    "undo_retention_s": Threshold(900, "s", "典型起步值，长查询场景需更高", "official_doc"),
    "session_count_high": Threshold(500, "sessions", "非 RAC 单实例会话数经验阈值", "derived"),
    "pga_aggregate_pct": Threshold(80.0, "%", "PGA 使用率超过 80% 说明存在大排序/哈希", "derived"),
    "shared_pool_free_pct": Threshold(10.0, "%", "低于 10% 容易出现 ORA-04031", "derived"),
    "table_rows_large": Threshold(50_000_000, "rows", "Oracle 侧大表阈值普遍高于 MySQL", "derived"),
}

# --------------------------------------------------------------------------
# 4. ClickHouse
# --------------------------------------------------------------------------

CLICKHOUSE = {
    "max_threads_per_query": Threshold((4, 16), "threads", "超过 CPU 核数无收益且加剧争抢", "derived"),
    "merge_running_max": Threshold(10, "merges", "后台合并队列积压会导致 Too many parts", "derived"),
    "parts_per_partition_max": Threshold(300, "parts", "超过建议阈值触发 Too many parts 错误", "official_doc"),
    "insert_batch_rows": Threshold(10_000, "rows", "小批量高频写入是 parts 暴涨元凶", "official_doc"),
    "distributed_group_by_memory_pct": Threshold(50.0, "%", "分布式聚合落盘比例过高需调 max_memory_usage", "derived"),
}

# --------------------------------------------------------------------------
# 5. 通用（跨引擎）
# --------------------------------------------------------------------------

GENERIC = {
    "cpu_used_pct": Threshold(80.0, "%", "CPU 持续 >80% 说明容量接近上限", "industry_practice"),
    "cpu_iowait_pct": Threshold(20.0, "%", "iowait 超过 20% 说明存储成为瓶颈", "industry_practice"),
    "disk_used_pct": Threshold(85.0, "%", "磁盘使用率 85% 是通用告警线", "industry_practice"),
    "disk_used_pct_critical": Threshold(95.0, "%", "95% 以上随时可能写满导致实例只读", "industry_practice"),
    "conn_pool_wait_ms": Threshold(10, "ms", "连接池获取连接等待超过 10ms 说明池太小或慢查询占满连接", "derived"),
    "p99_latency_ms": Threshold(200, "ms", "OLTP 简单查询 P99 经验上限", "industry_practice"),
    "retry_storm_threshold": Threshold(3, "retries", "同一操作重试超过 3 次通常是设计问题而非瞬时抖动", "derived"),
    "backup_rpo_hours": Threshold(24, "hours", "RPO 超过 24h 通常不可接受", "industry_practice"),
}

SUPPORTED = {
    "mysql": MYSQL,
    "mariadb": MYSQL,
    "postgres": POSTGRES,
    "oracle": ORACLE,
    "clickhouse": CLICKHOUSE,
    "dameng": ORACLE,        # 达梦对 Oracle 兼容，初始基线沿用
    "oceanbase": MYSQL,      # OB MySQL 模式
    "gaussdb": POSTGRES,     # GaussDB 基于 PG
    "kingbase": POSTGRES,    #  KingbaseES 基于 PG
    "tidb": MYSQL,
}

VERSION_TABLES = {
    "mysql": MYSQL_VERSIONS,
    "postgres": POSTGRES_VERSIONS,
}


def get(dialect: str, key: str, default: float | tuple | None = None) -> Threshold | None:
    """取某方言的阈值；方言没有则回落到通用库。"""
    table = SUPPORTED.get(dialect, {})
    t = table.get(key)
    if t is None:
        t = GENERIC.get(key)
    if t is None and default is not None:
        t = Threshold(default, "", "未在基线库中定义，使用调用方默认值")
    return t


def value(dialect: str, key: str, default: float = 0.0) -> float:
    """取阈值的主值（区间取上界，便于做 '超过 X 即告警' 判断）。"""
    t = get(dialect, key)
    if t is None:
        return default
    if isinstance(t.value, tuple):
        return float(t.value[1])
    return float(t.value)


def range_of(dialect: str, key: str) -> tuple[float, float]:
    t = get(dialect, key)
    if t is None:
        return (0.0, 0.0)
    if isinstance(t.value, tuple):
        return (float(t.value[0]), float(t.value[1]))
    return (0.0, float(t.value))


def cite(dialect: str, key: str) -> str:
    """生成 evidence.source 用的引用路径。"""
    return f"baselines.{dialect}.{key}"


def list_keys(dialect: str) -> list[str]:
    return sorted(set(SUPPORTED.get(dialect, {})) | set(GENERIC))


def version_features(dialect: str, version: str) -> dict:
    """取某版本的引擎能力位（用于判断该版本支持哪些在线 DDL 手段等）。"""
    table = VERSION_TABLES.get(dialect)
    if not table:
        return {}
    major = ".".join(version.split(".")[:2])
    if major in table:
        return table[major]
    # 回落：取不大于当前版本的最大已知版本
    try:
        cur = tuple(int(p) for p in major.split(".") if p.isdigit())
    except ValueError:
        return {}
    best, best_ver = None, (0,)
    for v, feats in table.items():
        try:
            vt = tuple(int(p) for p in v.split("."))
        except ValueError:
            continue
        if vt <= cur and vt > best_ver:
            best, best_ver = feats, vt
    return best or {}
