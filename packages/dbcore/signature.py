"""故障指纹库（Tier0 知识层 · 其二）。

冷启动的第二根支柱：**把老 DBA 脑子里的"看到 X 就先查 Y"沉淀成数据。**

每种故障指纹描述：
  * symptoms  —— 现象关键词（中英文，用于 match_symptom 文本匹配）
  * checks    —— 必查项（每项是可执行的 SQL 或检查动作）
  * causes    —— 常见成因（按概率排序）
  * actions   —— 处置动作（含止损优先级的排序）
  * escalate  —— 什么情况下必须升级/停止自行处置

为什么必须外置成数据而不是写进代码：
  1. 这是可以直接交付给客户的东西，客户能看懂并认可专业性
  2. 每做一次项目就往里加几条，它是复利的
  3. 编排层可以按 symptom 检索并驱动 multi-step 检查，不需要改代码
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Check:
    name: str
    kind: str                    # sql | metric | inspect | client_side
    target: str = ""             # SQL 或指标名
    dialects: tuple[str, ...] = ("mysql", "postgres", "oracle", "clickhouse")
    why: str = ""


@dataclass(frozen=True)
class Signature:
    id: str
    title: str
    category: str                # latency | lock | replication | capacity | connection | data | config
    symptoms: tuple[str, ...]
    dialects: tuple[str, ...] = ("mysql", "postgres", "oracle")
    checks: tuple[Check, ...] = ()
    causes: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    escalate: str = ""
    priority: int = 5            # 冲突时小的先跑


_CH = Check

SIGNATURES: list[Signature] = [
    # ------------------------------------------------------------------ 复制 / 主从
    Signature(
        id="REPL001",
        title="主从延迟突增",
        category="replication",
        symptoms=("主从延迟", "延迟变大", "slave lag", "replication lag", "延迟高", "seconds_behind",
                  "读取不到刚写入", "读写不一致"),
        dialects=("mysql", "postgres", "mariadb"),
        checks=(
            _CH("确认延迟数值与趋势", "sql",
                "SHOW REPLICA STATUS", ("mysql", "mariadb"),
                "确认 Seconds_Behind_Master 及其是否持续增长"),
            _CH("确认回放线程是否为单线程", "inspect", "replica_parallel_workers", ("mysql",),
                "5.6/5.7 默认单线程回放是延迟最常见的结构性原因"),
            _CH("定位是否有大事务", "sql",
                "SELECT * FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 5", ("mysql",),
                "一个大事务在主库跑 10 分钟，从库回放也至少 10 分钟"),
            _CH("确认从库是否被长查询阻塞", "sql",
                "SELECT * FROM performance_schema.data_lock_waits", ("mysql",),
                "从库上的只读查询会阻塞回放线程"),
            _CH("确认是否有大表 DDL 正在回放", "sql", "SHOW PROCESSLIST", ("mysql", "mariadb"),
                "DDL 回放期间后续 binlog 堆积"),
            _CH("检查从库 IO/CPU 是否打满", "metric", "ioutil,cpu", ("mysql", "postgres"),
                "从库规格低于主库时，回放追不上产生速率"),
        ),
        causes=(
            "主库执行了大事务（批量更新/删除、无条件 UPDATE），回放耗时线性放大",
            "从库单线程回放能力不足（MySQL 5.6/5.7）",
            "从库上存在长事务/大查询阻塞回放线程",
            "主库进行了大表 DDL（加索引/改字段类型）",
            "从库规格不足（磁盘 IOPS 或 CPU 瓶颈）",
            "缺少主键的表导致回放退化为全表扫描",
            "binlog 格式为 STATEMENT 且存在不确定语句",
        ),
        actions=(
            "【止损】先把读流量切到主库或对一致性敏感的查询强制走主库",
            "【定位】查 information_schema.innodb_trx 找出运行最久的事务，评估 kill 的收益与代价",
            "【结构】无主键的表尽快补主键，这是回放慢的头号根因",
            "【参数】8.0 开启 replica_parallel_workers（建议 4~8）+ LOGICAL_CLOCK",
            "【规范】禁止一次性更新超过 1 万行，改为分批 + 每批 sleep",
            "【容量】核对从库与主库规格是否对等",
        ),
        escalate="延迟持续增长且无法通过 kill 事务收敛时，需评估重建从库",
        priority=2,
    ),
    Signature(
        id="REPL002",
        title="复制中断（SQL thread / IO thread 停止）",
        category="replication",
        symptoms=("复制中断", "slave sql thread", "io thread", "last_io_error", "last_sql_error",
                  "1032", "1062", "同步报错", "replication stopped"),
        dialects=("mysql", "mariadb"),
        checks=(
            _CH("读取错误信息", "sql", "SHOW REPLICA STATUS", ("mysql", "mariadb"),
                "Last_SQL_Error / Last_IO_Error 直接给出错误码"),
            _CH("确认从库是否只读", "sql", "SELECT @@read_only, @@super_read_only", ("mysql",),
                "防止从库被误写入导致后续冲突"),
            _CH("确认主库 binlog 是否还存在", "sql", "SHOW BINARY LOGS", ("mysql",),
                "IO thread 拉不到日志通常是主库已 PURGE"),
            _CH("对比主从表结构", "inspect", "schema_compare", ("mysql",),
                "只在主库执行过的 DDL 会让回放失败"),
        ),
        causes=(
            "1032：从库缺少要更新的行（从库被写入过或数据不一致）",
            "1062：主键冲突（同上，或重复回放）",
            "主库 binlog 被清理且位置错误（IO thread 拉不到）",
            "主从表结构不一致（ alter 只在主库执行过）",
            "磁盘写满导致 relay log 无法写入",
        ),
        actions=(
            "【止损】标记该从库不可用，摘除读流量",
            "【判因】按错误码分类：1032/1062 属于数据不一致，需要比对修复",
            "【不要】不要无脑跳过错误（sql_slave_skip_counter），会掩盖真实不一致",
            "【根治】重建从库是最省时的确定性路径，拖得越久不一致越多",
        ),
        escalate="出现 1032/1062 说明已数据不一致，建议重建从库而非逐条修补",
        priority=2,
    ),

    # ------------------------------------------------------------------ 锁 / 阻塞
    Signature(
        id="LOCK001",
        title="锁等待 / 死锁",
        category="lock",
        symptoms=("锁等待", "lock wait timeout", "死锁", "deadlock", "超时回滚", "trx_waiting",
                  "更新卡住", "卡死", "1205", "1213"),
        dialects=("mysql", "postgres", "oracle"),
        checks=(
            _CH("列出当前等待与持有者", "sql",
                "SELECT * FROM performance_schema.data_lock_waits", ("mysql",),
                "直接给出 requesting / blocking 事务对"),
            _CH("找出阻塞源头事务", "sql",
                "SELECT trx_id,trx_started,trx_rows_locked,trx_query FROM information_schema.innodb_trx "
                "ORDER BY trx_started LIMIT 10", ("mysql",),
                "trx_started 最早的通常是源头"),
            _CH("确认是否未提交的长事务", "inspect", "idle_in_transaction", ("postgres", "mysql"),
                "应用开启事务后未提交是最常见根因"),
            _CH("查看最近的死锁记录", "sql", "SHOW ENGINE INNODB STATUS", ("mysql",),
                "LATEST DETECTED DEADLOCK 段给出两条互相等待的 SQL"),
            _CH("检查隔离级别", "sql", "SELECT @@transaction_isolation", ("mysql",),
                "RR 下 gap lock 会显著扩大锁范围"),
        ),
        causes=(
            "事务内包含远程调用/人工操作，持有锁不放（最常见）",
            "多条 SQL 更新同一批资源的顺序不一致 → 死锁",
            "缺少索引导致 UPDATE/DELETE 退化为大量行锁",
            "RR 隔离级别下的 gap lock 锁住不存在的区间",
            "热点行并发更新（库存/余额类）",
            "大事务未拆分，一次锁住百万行",
        ),
        actions=(
            "【止损】kill 阻塞源头事务前必须确认：它跑了多久、影响多少行、能否回滚（大事务回滚可能更久）",
            "【不要】不要盲目 kill 大事务，回滚可能比等待更慢",
            "【根治】给 UPDATE/DELETE 的过滤条件加索引，避免锁升级为大量行锁",
            "【规范】事务内禁止 RPC、文件 IO、人工确认",
            "【优化】热点行改为异步排队或分段扣减",
            "【配置】适当调低 innodb_lock_wait_timeout（10~30s）让问题快速暴露",
        ),
        escalate="kill 前必须评估回滚时间；运行中大事务回滚可能耗时数倍于已执行时间",
        priority=1,
    ),
    Signature(
        id="LOCK002",
        title="元数据锁阻塞（Waiting for table metadata lock）",
        category="lock",
        symptoms=("metadata lock", "mdl", " Waiting for table metadata lock", "ddl卡住", "ddl 卡住",
                  "alter 不动", "truncate 卡住"),
        dialects=("mysql", "mariadb"),
        checks=(
            _CH("找出持有 MDL 的会话", "sql",
                "SELECT * FROM performance_schema.metadata_locks WHERE OWNER_THREAD_ID IS NOT NULL", ("mysql",),
                "定位持有者线程"),
            _CH("查看该会话当前语句", "sql", "SHOW PROCESSLIST", ("mysql", "mariadb"),
                "通常是未提交的事务占着 MDL"),
            _CH("检查是否有失败悬挂的事务", "sql",
                "SELECT trx_id,trx_state,trx_started FROM information_schema.innodb_trx",
                ("mysql",), "XA / 悬挂事务也会持有 MDL"),
            _CH("确认 DDL 是否已排队", "sql", "SHOW PROCESSLIST", ("mysql",),
                "已经排队的 DDL 会阻塞后续所有该表的查询"),
        ),
        causes=(
            "表上有未提交的事务（哪怕只 SELECT 过），DDL 会被无限期阻塞",
            "长事务 + DDL 撞在一起",
            "存在失败悬挂的事务未回滚",
        ),
        actions=(
            "【止损】找到并结束持有 MDL 的会话，或让业务提交事务",
            "【规范】DDL 必须用 change-risk.precheck_change_window 先检查有没有长事务",
            "【工具】大表 DDL 用 gh-ost / pt-osc 并设置 lock_wait_timeout",
        ),
        priority=1,
    ),

    # ------------------------------------------------------------------ 连接
    Signature(
        id="CONN001",
        title="连接数打满 / Too many connections",
        category="connection",
        symptoms=("too many connections", "连接数满", "连不上数据库", "连接池耗尽", "1040",
                  "connection pool exhausted", "无法连接"),
        dialects=("mysql", "postgres", "oracle", "clickhouse"),
        checks=(
            _CH("当前连接数与上限", "sql", "SHOW VARIABLES LIKE 'max_connections'", ("mysql",),
                "与被打满的程度"),
            _CH("按用户/来源 IP 统计连接", "sql",
                "SELECT user,host,count(*) FROM information_schema.processlist GROUP BY user,host", ("mysql",),
                "快速判断是哪个应用实例泄漏"),
            _CH("统计连接状态分布", "sql",
                "SELECT command,count(*) FROM information_schema.processlist GROUP BY command", ("mysql",),
                "Sleep 占多数说明连接未归还"),
            _CH("查看是否有 sleep 很久的连接", "sql",
                "SELECT id,time,state,info FROM information_schema.processlist "
                "WHERE command='Sleep' AND time>300", ("mysql",),
                "连接泄漏的典型特征"),
        ),
        causes=(
            "慢查询堆积，连接被长时间占用无法释放",
            "连接池配置过大或未设超时（maxLifetime/idleTimeout）",
            "应用实例扩容但未相应下调单实例连接池大小",
            "事务未提交导致连接无法归还池中",
            "网络分区导致连接假死堆积",
        ),
        actions=(
            "【止损】先用管理员预留连接登录（extra_port / super 用户），再 kill 长时间 Sleep 连接",
            "【定位】确认是慢查询还是纯泄漏：前者 Threads_running 高，后者 Sleep 多",
            "【参数】设置 wait_timeout / interactive_timeout（如 300s）",
            "【客户端】连接池开启 testOnBorrow + 合理 maxLifetime",
            "【注意】不要一味调大 max_connections，每连接都占内存且会加剧上下文切换",
        ),
        escalate="已达上限无法登录时，需通过预留管理端口或重启实例",
        priority=1,
    ),

    # ------------------------------------------------------------------ CPU / 慢查询
    Signature(
        id="CPU001",
        title="CPU 打满 / 慢查询堆积",
        category="latency",
        symptoms=("cpu 高", "cpu打满", "cpu 100%", "负载高", "慢查询", "慢 sql", "响应变慢",
                  "接口超时", "load average", "performance drop"),
        dialects=("mysql", "postgres", "oracle", "clickhouse"),
        checks=(
            _CH("确认是 CPU 还是 IO 瓶颈", "metric", "cpu,iowait", ("mysql", "postgres", "oracle"),
                "iowait 高则是存储瓶颈，处置方向完全不同"),
            _CH("抓取当前活跃语句", "sql",
                "SELECT id,time,state,left(info,200) FROM information_schema.processlist "
                "WHERE command!='Sleep' AND time>2 ORDER BY time DESC", ("mysql",),
                "直接看到正在跑什么"),
            _CH("按总耗时聚合慢日志 TOP SQL", "sql", "mysqldumpslow -s t -t 10", ("mysql",),
                "看累计影响而非单次"),
            _CH("观察排序/临时表指标", "metric", "Created_tmp_disk_tables,Sort_merge_passes", ("mysql",),
                "判断是否有大量内存排序失败"),
            _CH("检查 SQL 是否走索引", "sql", "EXPLAIN <top_sql>", ("mysql", "postgres"),
                "看 type/key/rows/Extra"),
        ),
        causes=(
            "新增 SQL 未走索引（上线引入最常见）",
            "数据量增长使原本能走索引的 SQL 优化器改走全表扫描",
            "统计信息过期导致优化器选错计划",
            "大结果集排序 / 分组导致 CPU 密集",
            "隐式类型转换使索引失效",
            "复杂 JOIN 顺序不合理，驱动表选择错误",
            "并发量自然增长触及规格上限",
        ),
        actions=(
            "【止损】先定位 TOP SQL，必要时临时 kill 少数长时间查询",
            "【最优】用 sql-review.suggest_fixes 拿到可执行改写并用 compare_plans 量化收益",
            "【索引】用 advise_indexes 评估加索引，注意先检查重复/前缀覆盖",
            "【统计】MySQL 执行 ANALYZE TABLE，PG 执行 ANALYZE（不改变数据）",
            "【验证】改写后必须 compare_plans 确认 rows 下降，不要凭感觉上线",
        ),
        priority=1,
    ),
    Signature(
        id="LAT002",
        title="单条 SQL 偶发变慢（执行计划跳变）",
        category="latency",
        symptoms=("时快时慢", "偶发变慢", "计划变差", "plan flip", "同一条sql", "稳定后又变慢",
                  "optimizer choose wrong"),
        dialects=("mysql", "postgres", "oracle"),
        checks=(
            _CH("对比历史执行计划", "plan", "EXPLAIN", ("mysql", "postgres"),
                "有 history 时做同比"),
            _CH("检查表统计信息更新时间", "sql",
                "SELECT table_name,update_time FROM information_schema.tables WHERE table_schema=DATABASE()",
                ("mysql",), "统计过期是计划跳变首因"),
            _CH("检查数据倾斜", "sql", "SELECT ... GROUP BY <key> ORDER BY cnt DESC LIMIT 20",
                ("mysql", "postgres"), "直方图缺失时优化器按均匀分布估算"),
            _CH("确认是否有参数变化", "inspect", "optimizer_switch,work_mem,random_page_cost",
                ("mysql", "postgres"), ""),
        ),
        causes=(
            "统计信息过期/采样不准",
            "数据分布倾斜（长尾值）",
            "索引数量变化使可选路径增加",
            "PG 的 random_page_cost 与实际 SSD 不匹配",
            "MySQL optimizer_switch 被修改",
        ),
        actions=(
            "ANALYZE 刷新统计",
            "MySQL 8 / PG 10+ 建直方图 ANALYZE TABLE t UPDATE HISTOGRAM",
            "必要时用 hint 或固化计划（但要记录原因）",
        ),
        priority=3,
    ),

    # ------------------------------------------------------------------ IO / 磁盘
    Signature(
        id="IO001",
        title="磁盘 IO 打满 / IOPS 瓶颈",
        category="capacity",
        symptoms=("io 高", "iowait", "磁盘忙", "io打满", "util 100", "写入卡", "flush 慢",
                  "io bottleneck"),
        dialects=("mysql", "postgres", "oracle", "clickhouse"),
        checks=(
            _CH("确认磁盘 util 与 await", "metric", "iostat -x 1", ("mysql", "postgres"),
                "util≈100% 且 await 高即为饱和"),
            _CH("定位 IO 来源", "sql",
                "SELECT * FROM sys.io_global_by_file_by_bytes LIMIT 10", ("mysql",),
                "看是数据文件还是临时文件/redo"),
            _CH("检查 redo 与刷盘策略", "inspect", "innodb_flush_log_at_trx_commit,sync_binlog", ("mysql",),
                "双 1 配置下每次写事务两次 fsync"),
            _CH("检查是否有大量临时表落盘", "metric", "Created_tmp_disk_tables", ("mysql",),
                "SQL 侧可控"),
        ),
        causes=(
            "大量随机写 + 双 1 刷盘策略",
            "慢 SQL 全表扫描刷冷数据，稀释缓冲池",
            "批量导入/删除产生大量 redo 与 undo",
            "磁盘规格不足（云盘 IOPS 配额打满）",
            "CHECKPOINT 过于频繁",
        ),
        actions=(
            "【SQL 侧】消除全表扫描，通常比换磁盘便宜得多",
            "【配置】innodb_io_capacity 与实际磁盘匹配；评估 innodb_flush_neighbors=0（SSD）",
            "【权衡】双 1 改为 2/1000 可降 IO 但会丢最后一秒数据，需业务决策而非 DBA 独断",
            "【扩容】云盘 IOPS 打满时申请更高配额",
        ),
        priority=2,
    ),
    Signature(
        id="DISK001",
        title="磁盘空间不足 / 实例只读",
        category="capacity",
        symptoms=("磁盘满", "空间不足", "no space left", "read only", "只读", "写入失败",
                  "binlog 打满", "临时空间满"),
        dialects=("mysql", "postgres", "oracle", "clickhouse"),
        checks=(
            _CH("确认各目录用量", "inspect", "df -h", ("mysql", "postgres"),
                "区分数据目录、日志目录、临时目录"),
            _CH("找出最大的表", "sql",
                "SELECT table_schema,table_name,data_length+index_length FROM information_schema.tables "
                "ORDER BY 3 DESC LIMIT 20", ("mysql",), "快速定位大表"),
            _CH("检查 binlog 保留策略", "sql", "SHOW VARIABLES LIKE 'binlog_expire_logs_seconds'", ("mysql",),
                "默认期过长会堆积"),
            _CH("检查是否有大 undo/临时文件", "inspect", "du -sh /var/lib/mysql/*", ("mysql",),
                "长事务会撑大 undo"),
        ),
        causes=(
            "binlog/归档日志未清理",
            "大表无归档策略，历史数据只增不减",
            "长事务撑大 undo 表空间",
            "临时文件未清理（PG 典型）",
            "慢查询产生巨大临时表",
        ),
        actions=(
            "【紧急】先清理可安全删除的日志（binlog、慢日志、错误日志）换取空间",
            "【注意】不要直接 rm 正在写入的 binlog，应 PURGE BINARY LOGS",
            "【归档】建立历史数据归档策略",
            "【监控】磁盘超过 85% 就该告警，不要等到 100%",
        ),
        escalate="空间彻底耗尽后实例会变只读，恢复过程可能需要扩容磁盘",
        priority=1,
    ),

    # ------------------------------------------------------------------ PG 专属
    Signature(
        id="PG001",
        title="PG 表膨胀 / autovacuum 跟不上",
        category="capacity",
        symptoms=("膨胀", "bloat", "vacuum", "空间不释放", "autovacuum", "死元组", "dead tuples",
                  "索引变大", "删除后空间没小"),
        dialects=("postgres", "gaussdb", "kingbase"),
        checks=(
            _CH("查看死元组占比", "sql",
                "SELECT relname,n_dead_tup,n_live_tup FROM pg_stat_user_tables "
                "ORDER BY n_dead_tup DESC LIMIT 20", ("postgres",), ""),
            _CH("检查 autovacuum 是否正在工作", "sql",
                "SELECT pid,phase,relid FROM pg_stat_progress_vacuum", ("postgres",), ""),
            _CH("检查是否有长事务阻塞 vacuum", "sql",
                "SELECT pid,state,xact_start,left(query,100) FROM pg_stat_activity "
                "WHERE state IN ('idle in transaction','active') AND xact_start < now() - interval '5 min'",
                ("postgres",), "idle in transaction 会阻止死元组回收"),
            _CH("表龄与 freeze 进度", "sql",
                "SELECT relname,age(relfrozenxid) FROM pg_class WHERE relkind='r' ORDER BY 2 DESC LIMIT 10",
                ("postgres",), "接近 2 亿会强制 autovacuum"),
        ),
        causes=(
            "存在长时间 idle in transaction 会话",
            "autovacuum 参数过于保守（默认 scale factor 对大表不友好）",
            "高频 UPDATE 表的清理速度跟不上产生速度",
            "复制槽未消费阻止 vacuum",
            "未提交的 prepared transaction",
        ),
        actions=(
            "【止损】先清掉 idle in transaction 会话（这是最常见且立竿见影的一步）",
            "【调参】高频更新表设置 per-table autovacuum_vacuum_scale_factor=0.01",
            "【清理】VACUUM (ANALYZE) 温和；VACUUM FULL 锁表，需变更窗口",
            "【在线】严重膨胀用 pg_repack 替代 VACUUM FULL",
            "【检查】确认没有废弃的复制槽",
        ),
        priority=2,
    ),
    Signature(
        id="PG002",
        title="PG 连接数打满 / 未使用连接池",
        category="connection",
        symptoms=("too many clients", "remaining connection slots", "连接池", "pgbouncer",
                  "fat connections"),
        dialects=("postgres", "gaussdb", "kingbase"),
        checks=(
            _CH("按状态统计连接", "sql",
                "SELECT state,count(*) FROM pg_stat_activity GROUP BY state", ("postgres",), ""),
            _CH("找出 idle in transaction", "sql",
                "SELECT pid,xact_start,left(query,100) FROM pg_stat_activity "
                "WHERE state='idle in transaction'", ("postgres",), "这类会话危害最大"),
            _CH("按应用来源统计连接", "sql",
                "SELECT application_name,count(*) FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC",
                ("postgres",), "定位是哪个服务开太多连接"),
        ),
        causes=("未使用连接池，每个 HTTP 请求一个连接", "事务未提交", "应用重连风暴"),
        actions=("引入 pgbouncer/PgBouncer transaction 模式", "修复未提交事务", "控制应用侧池大小"),
        priority=2,
    ),

    # ------------------------------------------------------------------ 内存 / OOM
    Signature(
        id="MEM001",
        title="内存暴涨 / OOM 被杀",
        category="capacity",
        symptoms=("oom", "内存暴涨", "被杀", "killed", "out of memory", "swap", "内存不足"),
        dialects=("mysql", "postgres", "oracle", "clickhouse"),
        checks=(
            _CH("确认 OS 层是谁吃内存", "inspect", "top / free -m", ("mysql", "postgres"),
                "先分清是数据库还是同机其它进程"),
            _CH("MySQL：按 buffer 拆分核算", "sql",
                "SHOW VARIABLES WHERE variable_name IN "
                "('innodb_buffer_pool_size','key_buffer_size','tmp_table_size','sort_buffer_size','join_buffer_size')",
                ("mysql",), "注意 × 连接数的部分"),
            _CH("PG：检查 work_mem 与实际落盘", "sql",
                "SELECT * FROM pg_stat_statements ORDER BY temp_blk_written DESC LIMIT 10", ("postgres",),
                "work_mem 是每节点每连接的"),
            _CH("检查是否有大查询同时执行", "sql", "SHOW PROCESSLIST", ("mysql", "mariadb"),
                "并发大查询会叠加内存"),
        ),
        causes=(
            "每连接内存参数（sort/join/read_buffer）× 高并发",
            "大结果集客户端未流式读取，全部缓存在服务端/客户端",
            "临时表/排序远超预期",
            "同机部署其它进程竞争",
            "内存泄漏（少见但存在）",
        ),
        actions=(
            "【核算】先算清：全局缓冲 + 每连接缓冲 × max_connections，不要只看单项",
            "【限流】限制并发大查询",
            "【调优】sort_buffer_size 不要盲目调大，它是每个连接独立申请的",
            "【隔离】数据库独占机器",
        ),
        priority=2,
    ),

    # ------------------------------------------------------------------ 数据正确性
    Signature(
        id="DATA001",
        title="数据不一致 / 计数对不上",
        category="data",
        symptoms=("数据不一致", "对不上", "口径不一致", "两边不一致", "数量不对", "多了", "少了",
                  "重复数据", "dau 不一样", "报表不一致", "dau 不一致", "数据不同",
                  "结果不一样", "算出来不一样"),
        dialects=("mysql", "postgres", "oracle", "clickhouse"),
        checks=(
            _CH("确认统计口径是否一致", "inspect", "definition_compare", ("mysql", "postgres"),
                "多数是口径而非数据问题：时间窗口、去重键、是否含测试账号"),
            _CH("检查 JOIN 是否发生扇出", "inspect", "fanout_detect", ("mysql", "postgres"),
                "一对多 JOIN 导致行数放大是最常见的计数虚高"),
            _CH("逐层下钻定位差异", "inspect", "reconcile", ("mysql", "postgres"),
                "从最细粒度回溯"),
            _CH("确认时区与日期边界", "inspect", "timezone", ("mysql", "postgres"),
                "UTC vs 本地时区会造成整天偏移"),
        ),
        causes=(
            "JOIN 扇出：一对多关系导致行数被放大后 COUNT",
            "口径差异：时间窗口/去重维度/过滤条件不同",
            "时区不一致导致日期边界错位",
            "LEFT JOIN 后 WHERE 过滤误伤 NULL 行",
            "NULL 参与聚合的语义差异（COUNT(col) 不计 NULL）",
            "软删除标志未在两边统一过滤",
        ),
        actions=(
            "【先别急着修数据】先让两边用完全相同的 SQL 跑同一份数据，确认是口径还是数据问题",
            "【工具】data-semantics.compare_definitions 结构化比对口径差异",
            "【工具】data-semantics.reconcile 自动下钻定位到具体 JOIN/过滤层",
            "【规范】把最终口径固化成指标契约",
        ),
        escalate="确认是真实数据丢失时，需审计 binlog / 审计日志定位写入点",
        priority=2,
    ),
    Signature(
        id="DATA002",
        title="误删除 / 误更新数据的止损与恢复",
        category="data",
        symptoms=("误删", "误更新", "删错了", "update 没加 where", "回滚数据", "恢复数据",
                  "delete 错了"),
        dialects=("mysql", "postgres", "oracle"),
        checks=(
            _CH("确认操作时间与影响范围", "sql", "SHOW BINARY LOGS", ("mysql",),
                "确定要回溯到哪个位置"),
            _CH("确认是否有备份", "inspect", "backup_list", ("mysql", "postgres"), ""),
            _CH("确认 binlog 格式", "sql", "SELECT @@binlog_format", ("mysql",),
                "ROW 格式才能做 flashback 式反向操作"),
        ),
        causes=("UPDATE/DELETE 未加 WHERE 或条件写错", "脚本没有 dry-run", "批量任务参数错误"),
        actions=(
            "【止损】立即冻结写入权限，避免二次污染",
            "【不要】不要在生产库上直接尝试修复语句",
            "【恢复】ROW 格式 binlog 可用 flashback 工具生成反向 SQL；否则走备份 + 增量回放",
            "【验证】恢复后必须做 COUNT/CHECKSUM 校验",
            "【预防】上线前用 sql-review.review_sql，无 WHERE 的 UPDATE 是 block 级问题",
        ),
        escalate="涉及跨库或无法精确定位时，需要专业数据恢复，勿自行操作",
        priority=1,
    ),

    # ------------------------------------------------------------------ DDL / 变更
    Signature(
        id="DDL001",
        title="大表 DDL 卡住 / 影响业务",
        category="lock",
        symptoms=("ddl 卡住", "alter 卡", "加索引慢", "加字段卡住", "online ddl", "锁表",
                  "alter table 慢", "gh-ost"),
        dialects=("mysql", "postgres", "oracle"),
        checks=(
            _CH("确认表大小", "sql",
                "SELECT table_rows,data_length+index_length FROM information_schema.tables "
                "WHERE table_name='<t>'", ("mysql",), "决定用哪种方式"),
            _CH("确认版本是否支持 INSTANT/INPLACE", "inspect", "version", ("mysql",),
                "8.0 才支持 INSTANT ADD COLUMN"),
            _CH("检查是否有长事务", "sql", "SELECT * FROM information_schema.innodb_trx", ("mysql",),
                "会导致 DDL 等待 MDL"),
            _CH("确认复制延迟余量", "sql", "SHOW REPLICA STATUS", ("mysql", "mariadb"),
                "DDL 会传导到从库"),
        ),
        causes=(
            "MySQL 5.7 及以下 ADD INDEX 会重建表（COPY）",
            "存在长事务持有 MDL",
            "磁盘空间不足以建临时表",
            "从库延迟导致 DDL 回放堆积",
        ),
        actions=(
            "【工具】用 change-risk.assess_change 先拿 L0~L3 评级与推荐工具",
            "【方式】优先 gh-ost / pt-osc，或 8.0 的 ALGORITHM=INSTANT",
            "【时机】用 change-risk.precheck_change_window 确认窗口安全",
            "【PG】务必 CREATE INDEX CONCURRENTLY，否则全程锁写",
            "【回滚】先 generate_rollback，没有回滚方案的 DDL 不要执行",
        ),
        priority=1,
    ),

    # ------------------------------------------------------------------ 其他常见
    Signature(
        id="CFG001",
        title="参数配置不当导致的稳定性问题",
        category="config",
        symptoms=("参数配置", "默认值", "没有调优", "配置不合理", "缓冲池", "buffer pool 太小"),
        dialects=("mysql", "postgres", "oracle", "clickhouse"),
        checks=(
            _CH("核对核心参数与推荐区间", "inspect", "baselines", ("mysql", "postgres", "oracle"),
                "对比 baselines.py"),
            _CH("确认 innodb_buffer_pool_size 占比", "sql", "SHOW VARIABLES LIKE 'innodb_buffer_pool_size'",
                ("mysql",), "独占机 60~75% RAM"),
            _CH("检查慢查询阈值", "sql", "SHOW VARIABLES LIKE 'long_query_time'", ("mysql",),
                "默认 10s 对 OLTP 无意义"),
        ),
        causes=("使用默认配置未调优", "参数照抄网上模板未结合实际规格", "变更后未同步其它节点"),
        actions=(
            "用 change-risk / dbwatch 的基线对比输出差异清单",
            "逐项给出修改依据而非盲改",
            "主备参数必须一致",
        ),
        priority=4,
    ),
    Signature(
        id="TX001",
        title="长事务",
        category="lock",
        symptoms=("长事务", "事务没提交", "running 很久", "transaction open", "未提交"),
        dialects=("mysql", "postgres", "oracle"),
        checks=(
            _CH("MySQL 列出运行中的事务", "sql",
                "SELECT trx_id,trx_started,trx_rows_locked,trx_rows_modified,left(trx_query,120) "
                "FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 10", ("mysql",), ""),
            _CH("PG 列出长会话", "sql",
                "SELECT pid,state,xact_start,left(query,120) FROM pg_stat_activity "
                "WHERE xact_start IS NOT NULL ORDER BY xact_start LIMIT 10", ("postgres",), ""),
            _CH("检查事务是否被外部调用阻塞", "inspect", "application_stack",
                ("mysql", "postgres"), "事务内有 RPC 是典型"),
        ),
        causes=(
            "事务内包含 RPC / 人工审批 / 文件处理",
            "批量操作未分批",
            "应用异常退出未回滚",
            "autocommit=0 且忘记提交",
        ),
        actions=(
            "【止损】评估后决定是否 kill，注意回滚耗时可能与已运行时长同量级",
            "【根治】事务尽量短，禁止事务内外部调用",
            "【规范】设置 autocommit=1 + 连接池归还时强制回滚",
        ),
        escalate="kill 大事务前必须评估回滚耗时，可能引发更长的不可用",
        priority=2,
    ),
    Signature(
        id="BACKUP001",
        title="备份缺失或不可恢复风险",
        category="capacity",
        symptoms=("备份", "备份失败", "恢复不了", "rman", "xtrabackup", "备份策略"),
        dialects=("mysql", "postgres", "oracle"),
        checks=(
            _CH("确认最近一次成功备份时间", "inspect", "backup_list", ("mysql", "postgres", "oracle"), ""),
            _CH("是否做过恢复演练", "inspect", "restore_drill", ("mysql", "postgres"),
                "没演练过的备份等于没有备份"),
            _CH("备份是否与 binlog 配合到时间点", "inspect", "pitr_capability", ("mysql", "postgres"), ""),
        ),
        causes=("备份任务失败无人发现", "只做全备没有增量/binlog", "备份与生产同盘"),
        actions=(
            "建立全备 + 增量/binlog 策略",
            "定期做恢复演练并记录 RTO",
            "备份异地存放",
        ),
        priority=5,
    ),
]

_BY_ID = {s.id: s for s in SIGNATURES}


def all_signatures() -> list[Signature]:
    return list(SIGNATURES)


def get_signature(sid: str) -> Signature | None:
    return _BY_ID.get(sid)


def by_category(category: str) -> list[Signature]:
    return [s for s in SIGNATURES if s.category == category]


def categories() -> list[str]:
    return sorted({s.category for s in SIGNATURES})


def _norm(text: str) -> str:
    return re.sub(r"[\s_\-]+", "", text).lower()


def match_symptom(text: str, dialect: str | None = None, top_n: int = 5) -> list[tuple[Signature, float]]:
    """按症状文本检索指纹库。返回 (指纹, 得分) 列表，得分高的排前面。

    打分规则：关键词命中越多分越高；标题包含整体查询串给额外加权。
    """
    q = _norm(text)
    scored: list[tuple[Signature, float]] = []
    for s in SIGNATURES:
        if dialect and s.dialects and dialect not in s.dialects:
            continue
        hits = 0
        for kw in s.symptoms:
            k = _norm(kw)
            if len(k) < 2:
                continue
            if k in q or q in k:
                hits += 1
        if hits == 0:
            continue
        score = hits / max(len(s.symptoms), 1) ** 0.5
        if _norm(s.title) in q or q in _norm(s.title):
            score += 1.5
        score += (10 - s.priority) * 0.05
        scored.append((s, round(score, 3)))
    scored.sort(key=lambda x: (-x[1], x[0].priority, x[0].id))
    return scored[:top_n]


def to_dict(s: Signature) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "category": s.category,
        "symptoms": list(s.symptoms),
        "dialects": list(s.dialects),
        "priority": s.priority,
        "checks": [
            {"name": c.name, "kind": c.kind, "target": c.target,
             "dialects": list(c.dialects), "why": c.why}
            for c in s.checks
        ],
        "causes": list(s.causes),
        "actions": list(s.actions),
        "escalate": s.escalate,
    }


CHECK_KINDS = ("sql", "metric", "inspect", "plan", "client_side")
