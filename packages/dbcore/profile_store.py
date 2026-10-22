"""实例档案 —— 整条工具链的唯一数据落点（schema v3）。

为什么必须单一落点：
  每个 MCP 各存一份日志 = 一堆能被单独替换的原子能力，没有复利。
  所有 MCP 往同一个库写观测 = 别人第一天拿到一条 SQL，
  你拿到的是这张表三个月的历史。**这个文件才是资产，MCP 只是它的入口。**

冷启动相关（关键）：
  档案不足时 tier 计算必须如实返回 tier0/tier1，并在需要历史的工具里显式报错，
  而不是悄悄给出基于空数据的结论。见 tier_of()。

v3 建模（docs/DATA_MODEL_AND_ALGORITHMS.md §1.3 / §4.1）：
  - Observation 是唯一写入口，六种 kind：metric_sample|snapshot|finding|
    change_event|diagnosis_event|symptom（旧 kind 继续兼容）。
  - metric_sample 走结构化列 (metric, value, dims)，是 J3 全部趋势算法的原料。
  - counter 指标必须在调用方经 metrics.diff_raw_counters() 差分为速率后才可写入。
  - metric_baselines：基线是分位数分布（p50/p95/p99/spread），不是单值。
  - metric_windows：5m/1h/1d 聚合档，raw 只保留 90 天（prune_raw_observations）。
  - sql_profiles：一条 SQL 的一生（首次/末次/出现次数）。
  - finding_lifecycle：技术债账本，open→confirmed→fixing→fixed→verified / dismissed。
  - incidents + suspect_links：事故封装与「变更后 N 分钟内异常」的疑似关联。

兼容约定：
  record_observation / get_history / instance_summary / record_audit / recent_audit /
  set_baseline / get_baseline / save_change / load_change / list_changes
  旧签名全部保持，旧调用方无需改动。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".dbtoolchain")

SCHEMA_VERSION = 3

# kind 契约（v3）。旧 kind（snapshot/review/plan/incident/...）继续可用。
OBSERVATION_KINDS = (
    "metric_sample", "snapshot", "finding", "change_event",
    "diagnosis_event", "symptom",
)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        ts REAL NOT NULL,
        metric TEXT,
        value REAL,
        dims TEXT,
        correlation_id TEXT,
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        tool TEXT NOT NULL,
        instance_id TEXT,
        correlation_id TEXT,
        summary TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS instances (
        instance_id TEXT PRIMARY KEY,
        dialect TEXT,
        version TEXT,
        host TEXT,
        port TEXT,
        database_name TEXT,
        label TEXT,
        meta TEXT,
        first_seen REAL,
        last_seen REAL
    )""",
    """CREATE TABLE IF NOT EXISTS baselines (
        instance_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        window_days INTEGER DEFAULT 30,
        updated_at REAL NOT NULL,
        PRIMARY KEY(instance_id, metric)
    )""",
    """CREATE TABLE IF NOT EXISTS changes (
        change_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        state TEXT,
        created_at REAL,
        updated_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS dictionary (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,          -- metric | dimension | entity
        payload TEXT NOT NULL,
        updated_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS links (
        source_instance TEXT NOT NULL,
        target_instance TEXT NOT NULL,
        relation TEXT,
        meta TEXT,
        PRIMARY KEY(source_instance, target_instance)
    )""",
    # ---- v3 新增 ----
    """CREATE TABLE IF NOT EXISTS metric_windows (
        instance_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        window TEXT NOT NULL,        -- '5m' | '1h' | '1d'
        bucket_ts REAL NOT NULL,     -- 窗口起点(epoch 秒)
        cnt INTEGER,
        avg REAL,
        min REAL,
        max REAL,
        p95 REAL,
        PRIMARY KEY(instance_id, metric, window, bucket_ts)
    )""",
    """CREATE TABLE IF NOT EXISTS metric_baselines (
        instance_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        window TEXT NOT NULL,        -- 基线对应的聚合档
        p50 REAL, p95 REAL, p99 REAL, spread REAL,
        samples INTEGER,
        updated_at REAL NOT NULL,
        PRIMARY KEY(instance_id, metric, window)
    )""",
    """CREATE TABLE IF NOT EXISTS sql_profiles (
        fingerprint TEXT PRIMARY KEY,
        sample_sql TEXT,
        kind TEXT,
        first_seen REAL,
        last_seen REAL,
        occurrence_count INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS finding_lifecycle (
        finding_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        fingerprint TEXT,
        object TEXT,
        rule_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        severity TEXT,
        occurrences INTEGER DEFAULT 1,
        first_seen REAL,
        last_seen REAL,
        fixed_by_change TEXT,
        dismiss_reason TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS incidents (
        incident_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        symptom TEXT,
        opened_at REAL,
        closed_at REAL,
        diagnosis TEXT,
        remediation TEXT,
        timeline TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS suspect_links (
        anomaly_id TEXT NOT NULL,
        change_id TEXT NOT NULL,
        delta_seconds REAL,
        created_at REAL,
        PRIMARY KEY(anomaly_id, change_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obs_instance ON observations(instance_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_obs_kind ON observations(instance_id, kind, ts)",
    "CREATE INDEX IF NOT EXISTS idx_obs_series ON observations(instance_id, metric, ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_instance ON audit_log(instance_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_lc_instance ON finding_lifecycle(instance_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_inc_instance ON incidents(instance_id, opened_at)",
]

# 老版本库缺列时按需补。sqlite 没有 ADD COLUMN IF NOT EXISTS，只能查表结构。
_EXTRA_COLUMNS = {
    "audit_log": {"correlation_id": "TEXT"},
    "observations": {"metric": "TEXT", "value": "REAL",
                     "dims": "TEXT", "correlation_id": "TEXT"},
}

# findings 生命周期状态机（docs/DATA_MODEL_AND_ALGORITHMS.md §1.6）
FINDING_STATES = ("open", "confirmed", "fixing", "fixed", "verified", "dismissed")
FINDING_TRANSITIONS = {
    "open": {"confirmed", "fixing", "fixed", "dismissed"},
    "confirmed": {"fixing", "fixed", "dismissed"},
    "fixing": {"fixed", "dismissed"},
    "fixed": {"verified", "open"},          # verified 失败可退回 open
    "verified": set(),
    "dismissed": set(),
}


def store_path() -> str:
    return os.environ.get("DBT_STORE", os.path.join(DEFAULT_DIR, "store.db"))


def _conn() -> sqlite3.Connection:
    path = store_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    for ddl in SCHEMA:
        c.execute(ddl)
    _migrate(c)
    return c


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in _EXTRA_COLUMNS.items():
        try:
            existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            continue
        for col, decl in cols.items():
            if col not in existing:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.Error:
                    pass


def _iso(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or 0))


# ------------------------------------------------------------------
# Tier 判定（冷启动的核心开关）
# ------------------------------------------------------------------


def tier_of(instance_id: str, kind: str | None = None, days: int = 7) -> str:
    """判断该实例当前处于哪个能力档位。

    tier0：无档案（只能依赖静态知识）
    tier1：有 1 次以上观测/元数据
    tier2：有跨 >= 7 天、且 >= 2 次同类观测，可以算趋势
    """
    if not instance_id:
        return "tier0"
    with _conn() as c:
        row = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM observations WHERE instance_id=?",
                        (instance_id,)).fetchone()
        total, tmin, tmax = row or (0, None, None)
        if not total:
            meta = c.execute("SELECT 1 FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
            return "tier1" if meta else "tier0"
        if kind:
            row = c.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM observations "
                "WHERE instance_id=? AND kind=?", (instance_id, kind)).fetchone()
            total, tmin, tmax = row or (0, None, None)
            if total < 2:
                return "tier1"
        span_days = ((tmax or 0) - (tmin or 0)) / 86400.0
        if span_days >= days and total >= 2:
            return "tier2"
        return "tier1"


def require_tier(instance_id: str, want: str, kind: str | None = None) -> None:
    cur = tier_of(instance_id, kind)
    order = {"tier0": 0, "tier1": 1, "tier2": 2}
    if order.get(cur, 0) < order.get(want, 0):
        raise PermissionError(
            f"该能力需要 {want}，当前实例为 {cur}（需要的历史数据不足）。"
            f"请先执行 capture_snapshot 至少 2 次并间隔一段时间，或改用不依赖历史的 Tier0 能力。"
        )


# ------------------------------------------------------------------
# 实例
# ------------------------------------------------------------------


def upsert_instance(instance_id: str, dialect: str = "", version: str = "",
                    host: str = "", port: str = "", database_name: str = "",
                    label: str = "", meta: dict | None = None) -> dict:
    now = time.time()
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
        if exists:
            c.execute(
                "UPDATE instances SET dialect=COALESCE(NULLIF(?,''),dialect), "
                "version=COALESCE(NULLIF(?,''),version), host=COALESCE(NULLIF(?,''),host), "
                "port=COALESCE(NULLIF(?,''),port), database_name=COALESCE(NULLIF(?,''),database_name), "
                "label=COALESCE(NULLIF(?,''),label), meta=?, last_seen=? WHERE instance_id=?",
                (dialect, version, host, port, database_name, label,
                 json.dumps(meta or {}, ensure_ascii=False), now, instance_id),
            )
        else:
            c.execute(
                "INSERT INTO instances(instance_id,dialect,version,host,port,database_name,label,meta,"
                "first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (instance_id, dialect, version, host, port, database_name, label,
                 json.dumps(meta or {}, ensure_ascii=False), now, now),
            )
        c.execute("UPDATE instances SET last_seen=? WHERE instance_id=?", (now, instance_id))
    return get_instance(instance_id) or {}


def get_instance(instance_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute(
            "SELECT instance_id,dialect,version,host,port,database_name,label,meta,first_seen,last_seen "
            "FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
    if not r:
        return None
    return {
        "instance_id": r[0], "dialect": r[1], "version": r[2], "host": r[3],
        "port": r[4], "database_name": r[5], "label": r[6],
        "meta": json.loads(r[7]) if r[7] else {},
        "first_seen": r[8], "last_seen": r[9],
        "first_seen_iso": _iso(r[8]),
        "last_seen_iso": _iso(r[9]),
        "tier": tier_of(instance_id),
    }


def list_instances(limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT instance_id FROM instances ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
    return [get_instance(r[0]) for r in rows if get_instance(r[0])]


def link_cross_instance(source: str, target: str, relation: str = "same_business",
                        meta: dict | None = None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO links(source_instance,target_instance,relation,meta) "
            "VALUES(?,?,?,?)",
            (source, target, relation, json.dumps(meta or {}, ensure_ascii=False)),
        )


def related_instances(instance_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT target_instance, relation, meta FROM links WHERE source_instance=? "
            "UNION SELECT source_instance, relation, meta FROM links WHERE target_instance=?",
            (instance_id, instance_id)).fetchall()
    return [{"instance_id": r[0], "relation": r[1], "meta": json.loads(r[2]) if r[2] else {}}
            for r in rows]


# ------------------------------------------------------------------
# 观测（唯一写入口）
# ------------------------------------------------------------------


def record_observation(instance_id: str, kind: str, payload: Any,
                       correlation_id: str = "") -> int:
    """写入一条观测。payload 为 dict 时按 kind 契约携带字段。

    kind=metric_sample 且 payload 含 metric/value 时同步填结构化列；
    counter 指标必须在调用方差分为速率后再写（见 metrics.diff_raw_counters）。
    """
    if not isinstance(payload, dict):
        payload = {"value": payload}
    metric = payload.get("metric")
    value = payload.get("value")
    dims = payload.get("dims")
    if isinstance(value, bool):
        value = 1.0 if value else 0.0
    val_f = float(value) if isinstance(value, (int, float)) else None
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO observations(instance_id, kind, ts, metric, value, dims, correlation_id, payload) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (instance_id, kind, time.time(), metric, val_f,
             json.dumps(dims, ensure_ascii=False) if dims else None,
             correlation_id,
             json.dumps(payload, ensure_ascii=False, default=str)),
        )
        c.execute("UPDATE instances SET last_seen=? WHERE instance_id=?", (time.time(), instance_id))
        return int(cur.lastrowid or 0)


def record_metric_sample(instance_id: str, metric: str, value: float,
                         dims: dict | None = None, correlation_id: str = "") -> int:
    """结构化写一维指标样本（gauge 值或已差分的 counter 速率）。"""
    return record_observation(instance_id, "metric_sample",
                              {"metric": metric, "value": value, "dims": dims or {}},
                              correlation_id=correlation_id)


def record_change_event(instance_id: str, change_id: str, from_state: str, to_state: str,
                        actor: str = "", correlation_id: str = "") -> int:
    """变更状态机每一步迁移都写一条事件 —— 供 suspect_links 时间关联。"""
    return record_observation(instance_id, "change_event",
                              {"change_id": change_id, "from_state": from_state,
                               "to_state": to_state, "actor": actor},
                              correlation_id=correlation_id)


def get_history(instance_id: str, kind: str | None = None, limit: int = 50,
                since: float | None = None, until: float | None = None) -> list[dict]:
    sql = "SELECT id, kind, ts, payload FROM observations WHERE instance_id=?"
    args: list[Any] = [instance_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if since:
        sql += " AND ts>=?"
        args.append(since)
    if until:
        sql += " AND ts<=?"
        args.append(until)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[3])
        except json.JSONDecodeError:
            payload = {"raw": r[3]}
        out.append({
            "id": r[0], "kind": r[1], "ts": r[2],
            "ts_iso": _iso(r[2]),
            "payload": payload,
        })
    return out


def metric_series(instance_id: str, metric: str, since: float | None = None,
                  until: float | None = None, limit: int = 2000) -> list[dict]:
    """一维时间序列（J3 全部趋势算法的统一输入）。"""
    sql = ("SELECT ts, value, dims FROM observations "
           "WHERE instance_id=? AND kind='metric_sample' AND metric=?")
    args: list[Any] = [instance_id, metric]
    if since:
        sql += " AND ts>=?"
        args.append(since)
    if until:
        sql += " AND ts<=?"
        args.append(until)
    sql += " ORDER BY ts ASC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [{"ts": r[0], "value": r[1], "dims": json.loads(r[2]) if r[2] else {}}
            for r in rows if r[1] is not None]


def aggregate_windows(instance_id: str, window: str = "1h",
                      since: float | None = None, until: float | None = None) -> int:
    """把 raw metric_sample 聚合进 metric_windows 档。返回写入的窗口行数。"""
    span = {"5m": 300, "1h": 3600, "1d": 86400}.get(window)
    if not span:
        raise ValueError(f"未知窗口 {window}，可选 5m|1h|1d")
    since = since if since is not None else time.time() - 30 * 86400
    until = until if until is not None else time.time()
    with _conn() as c:
        rows = c.execute(
            "SELECT metric, ts, value FROM observations "
            "WHERE instance_id=? AND kind='metric_sample' AND value IS NOT NULL "
            "AND ts>=? AND ts<=? ORDER BY ts", (instance_id, since, until)).fetchall()
        if not rows:
            return 0
        buckets: dict[tuple, list[float]] = {}
        for metric, ts, val in rows:
            key = (metric, int(ts // span) * span)
            buckets.setdefault(key, []).append(float(val))
        n = 0
        for (metric, bts), vals in buckets.items():
            sv = sorted(vals)
            cnt = len(sv)
            p95 = sv[min(cnt - 1, int(cnt * 0.95))]
            c.execute(
                "INSERT OR REPLACE INTO metric_windows"
                "(instance_id,metric,window,bucket_ts,cnt,avg,min,max,p95) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (instance_id, metric, window, bts, cnt,
                 round(sum(sv) / cnt, 4), sv[0], sv[-1], p95))
            n += 1
        return n


def window_series(instance_id: str, metric: str, window: str = "1d",
                  limit: int = 400) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT bucket_ts, cnt, avg, min, max, p95 FROM metric_windows "
            "WHERE instance_id=? AND metric=? AND window=? ORDER BY bucket_ts DESC LIMIT ?",
            (instance_id, metric, window, limit)).fetchall()
    return [{"bucket_ts": r[0], "bucket_ts_iso": _iso(r[0]), "cnt": r[1],
             "avg": r[2], "min": r[3], "max": r[4], "p95": r[5]}
            for r in rows]


def prune_raw_observations(days: int = 90) -> int:
    """raw 观测保留 N 天；metric_sample 已聚合进 window 档后可安全删除。返回删除行数。"""
    cutoff = time.time() - days * 86400
    with _conn() as c:
        cur = c.execute("DELETE FROM observations WHERE kind='metric_sample' AND ts<?", (cutoff,))
        return cur.rowcount


def instance_summary(instance_id: str) -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM observations WHERE instance_id=?", (instance_id,)).fetchone()[0]
        kinds = c.execute(
            "SELECT kind, COUNT(*), MAX(ts) FROM observations WHERE instance_id=? "
            "GROUP BY kind ORDER BY 2 DESC", (instance_id,)).fetchall()
    inst = get_instance(instance_id) or {}
    return {
        "instance_id": instance_id,
        "tier": tier_of(instance_id),
        "total_observations": total,
        "by_kind": [
            {"kind": k, "count": n, "last_seen": _iso(ts)}
            for k, n, ts in kinds
        ],
        "instance": inst,
        "related": related_instances(instance_id),
    }


def get_profile(instance_id: str) -> dict:
    """实例的完整画像（给 LLM / 给客户看的全貌）。"""
    inst = get_instance(instance_id)
    if inst is None:
        return {"instance_id": instance_id, "known": False, "tier": "tier0",
                "note": "档案库中暂无该实例"}
    return {
        "instance_id": instance_id,
        "known": True,
        "tier": tier_of(instance_id),
        "instance": inst,
        "summary": instance_summary(instance_id),
        "snapshots": get_history(instance_id, kind="snapshot", limit=3),
        "reviews": get_history(instance_id, kind="review", limit=5),
        "plans": get_history(instance_id, kind="plan", limit=5),
        "changes": list_changes(instance_id, limit=10),
        "baselines": _get_baselines(instance_id),
        "metric_baselines": list_metric_baselines(instance_id),
        "open_findings": list_findings(instance_id, status="open", limit=20),
        "top_sql": top_sql(instance_id="", limit=10),
        "dictionary": list_dictionary(limit=20),
        "recent_audit": recent_audit(limit=10, instance_id=instance_id),
    }


# ------------------------------------------------------------------
# 基线
# ------------------------------------------------------------------


def set_baseline(instance_id: str, metric: str, value: float, window_days: int = 30) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO baselines(instance_id,metric,value,window_days,updated_at) "
            "VALUES(?,?,?,?,?)", (instance_id, metric, float(value), window_days, time.time()))


def get_baseline(instance_id: str, metric: str) -> dict | None:
    with _conn() as c:
        r = c.execute(
            "SELECT value, window_days, updated_at FROM baselines WHERE instance_id=? AND metric=?",
            (instance_id, metric)).fetchone()
    if not r:
        return None
    return {"metric": metric, "value": r[0], "window_days": r[1], "updated_at": r[2],
            "updated_at_iso": _iso(r[2])}


def _get_baselines(instance_id: str) -> dict[str, dict]:
    with _conn() as c:
        rows = c.execute("SELECT metric, value, window_days, updated_at FROM baselines "
                         "WHERE instance_id=?", (instance_id,)).fetchall()
    return {
        r[0]: {"value": r[1], "window_days": r[2],
               "updated_at_iso": _iso(r[3])}
        for r in rows
    }


def set_metric_baseline(instance_id: str, metric: str, window: str,
                        p50: float, p95: float, p99: float, spread: float,
                        samples: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO metric_baselines"
            "(instance_id,metric,window,p50,p95,p99,spread,samples,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (instance_id, metric, window, p50, p95, p99, spread, samples, time.time()))


def get_metric_baseline(instance_id: str, metric: str, window: str = "1h") -> dict | None:
    """返回分位数基线；不存在返回 None —— 调用方应回落到静态基线，不许猜。"""
    with _conn() as c:
        r = c.execute(
            "SELECT p50,p95,p99,spread,samples,updated_at FROM metric_baselines "
            "WHERE instance_id=? AND metric=? AND window=?",
            (instance_id, metric, window)).fetchone()
    if not r:
        return None
    return {"metric": metric, "window": window, "p50": r[0], "p95": r[1], "p99": r[2],
            "spread": r[3], "samples": r[4],
            "updated_at_iso": _iso(r[5])}


def list_metric_baselines(instance_id: str, window: str | None = None) -> list[dict]:
    sql = ("SELECT metric,window,p50,p95,p99,spread,samples,updated_at FROM metric_baselines "
           "WHERE instance_id=?")
    args: list[Any] = [instance_id]
    if window:
        sql += " AND window=?"
        args.append(window)
    sql += " ORDER BY metric"
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [{"metric": r[0], "window": r[1], "p50": r[2], "p95": r[3], "p99": r[4],
             "spread": r[5], "samples": r[6], "updated_at_iso": _iso(r[7])}
            for r in rows]


def compute_metric_baselines(instance_id: str, window: str = "1h",
                             window_days: int = 30, min_samples: int = 30) -> dict:
    """从 raw metric_sample 计算分位数基线。样本 < min_samples 的指标跳过（不猜）。"""
    since = time.time() - window_days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT metric, value FROM observations "
            "WHERE instance_id=? AND kind='metric_sample' AND value IS NOT NULL AND ts>=?",
            (instance_id, since)).fetchall()
    by_metric: dict[str, list[float]] = {}
    for metric, val in rows:
        by_metric.setdefault(metric, []).append(float(val))
    out = {}
    for metric, vals in by_metric.items():
        if len(vals) < min_samples:
            continue
        sv = sorted(vals)
        n = len(sv)
        p50 = sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2
        p95 = sv[min(n - 1, int(n * 0.95))]
        p99 = sv[min(n - 1, int(n * 0.99))]
        spread = max(p95 - p50, 0.0)
        set_metric_baseline(instance_id, metric, window, p50, p95, p99, spread, n)
        out[metric] = {"p50": round(p50, 4), "p95": round(p95, 4), "p99": round(p99, 4),
                       "spread": round(spread, 4), "samples": n}
    return out


def compute_baselines(instance_id: str, window_days: int = 30, metrics: Iterable[str] = ()) -> dict:
    """兼容入口（v2 语义）：从 snapshot 观测计算并写单值 baselines 表。"""
    since = time.time() - window_days * 86400
    hist = get_history(instance_id, kind="snapshot", since=since, limit=500)
    values: dict[str, list[float]] = {}
    for h in hist:
        m = (h["payload"] or {}).get("metrics") or h["payload"]
        if not isinstance(m, dict):
            continue
        for k, v in m.items():
            if isinstance(v, (int, float)):
                values.setdefault(k, []).append(float(v))
    out = {}
    for k, vs in values.items():
        if metrics and k not in metrics:
            continue
        if len(vs) < 2:
            continue                                  # 单点不能算基线
        vs_sorted = sorted(vs)
        n = len(vs)
        median = vs_sorted[n // 2] if n % 2 else (vs_sorted[n // 2 - 1] + vs_sorted[n // 2]) / 2
        mean = sum(vs) / n
        p95 = vs_sorted[min(n - 1, int(n * 0.95))]
        var = sum((x - mean) ** 2 for x in vs) / max(n - 1, 1)
        out[k] = {
            "samples": n, "median": round(median, 4), "mean": round(mean, 4),
            "p95": round(p95, 4), "stddev": round(var ** 0.5, 4),
            "min": round(vs_sorted[0], 4), "max": round(vs_sorted[-1], 4),
        }
        set_baseline(instance_id, k, median, window_days)
    return out


# ------------------------------------------------------------------
# SQL 档案（一条 SQL 的一生）
# ------------------------------------------------------------------


def record_sql_seen(fingerprint: str, sample_sql: str = "", kind: str = "") -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO sql_profiles(fingerprint,sample_sql,kind,first_seen,last_seen,occurrence_count) "
            "VALUES(?,?,?, ?,?,1) "
            "ON CONFLICT(fingerprint) DO UPDATE SET "
            "last_seen=excluded.last_seen, occurrence_count=occurrence_count+1, "
            "sample_sql=CASE WHEN excluded.sample_sql!='' THEN excluded.sample_sql ELSE sql_profiles.sample_sql END",
            (fingerprint, sample_sql[:2000], kind, now, now))


def get_sql_profile(fingerprint: str) -> dict | None:
    with _conn() as c:
        r = c.execute(
            "SELECT fingerprint,sample_sql,kind,first_seen,last_seen,occurrence_count "
            "FROM sql_profiles WHERE fingerprint=?", (fingerprint,)).fetchone()
    if not r:
        return None
    return {"fingerprint": r[0], "sample_sql": r[1], "kind": r[2],
            "first_seen_iso": _iso(r[3]), "last_seen_iso": _iso(r[4]),
            "occurrence_count": r[5]}


def top_sql(instance_id: str = "", limit: int = 20) -> list[dict]:
    """出现次数最多的 SQL（instance_id 目前未按实例隔离，保留参数兼容将来分库）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT fingerprint,sample_sql,kind,first_seen,last_seen,occurrence_count "
            "FROM sql_profiles ORDER BY occurrence_count DESC LIMIT ?", (limit,)).fetchall()
    return [{"fingerprint": r[0], "sample_sql": r[1], "kind": r[2],
             "first_seen_iso": _iso(r[3]), "last_seen_iso": _iso(r[4]),
             "occurrence_count": r[5]} for r in rows]


# ------------------------------------------------------------------
# 技术债账本（finding 生命周期）
# ------------------------------------------------------------------


def record_finding(instance_id: str, rule_id: str, object_ref: str = "",
                   fingerprint: str = "", severity: str = "medium",
                   finding_id: str = "") -> dict:
    """登记/累加一条发现。同一 (instance, rule, object/fingerprint) 聚合为一条生命周期。

    重复出现时 occurrences+1、last_seen 刷新、severity 取更严重者。
    """
    fid = finding_id or f"f:{instance_id}:{rule_id}:{fingerprint or object_ref}"
    now = time.time()
    sev_order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    with _conn() as c:
        r = c.execute("SELECT status, occurrences, severity, first_seen FROM finding_lifecycle "
                      "WHERE finding_id=?", (fid,)).fetchone()
        if r:
            status, occ, old_sev, first_seen = r
            new_sev = severity if sev_order.get(severity, 0) > sev_order.get(old_sev or "info", 0) else old_sev
            c.execute(
                "UPDATE finding_lifecycle SET occurrences=?, last_seen=?, severity=? "
                "WHERE finding_id=?", (occ + 1, now, new_sev, fid))
            return {"finding_id": fid, "status": status, "occurrences": occ + 1,
                    "first_seen": first_seen, "updated": True}
        c.execute(
            "INSERT INTO finding_lifecycle(finding_id,instance_id,fingerprint,object,rule_id,"
            "status,severity,occurrences,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fid, instance_id, fingerprint, object_ref, rule_id, "open", severity, 1, now, now))
    return {"finding_id": fid, "status": "open", "occurrences": 1, "updated": False}


def transition_finding(finding_id: str, to_state: str,
                       fixed_by_change: str = "", dismiss_reason: str = "") -> dict:
    """按状态机推进 finding 生命周期。非法转移抛 ValueError。"""
    if to_state not in FINDING_STATES:
        raise ValueError(f"未知状态 {to_state}，可选 {FINDING_STATES}")
    with _conn() as c:
        r = c.execute("SELECT status FROM finding_lifecycle WHERE finding_id=?", (finding_id,)).fetchone()
        if not r:
            raise ValueError(f"finding {finding_id} 不存在")
        cur = r[0]
        if to_state not in FINDING_TRANSITIONS.get(cur, set()):
            raise ValueError(f"非法转移 {cur} -> {to_state}，允许：{sorted(FINDING_TRANSITIONS[cur]) or '无'}")
        c.execute(
            "UPDATE finding_lifecycle SET status=?, fixed_by_change=CASE WHEN ?!='' THEN ? ELSE fixed_by_change END, "
            "dismiss_reason=CASE WHEN ?!='' THEN ? ELSE dismiss_reason END WHERE finding_id=?",
            (to_state, fixed_by_change, fixed_by_change, dismiss_reason, dismiss_reason, finding_id))
    return {"finding_id": finding_id, "status": to_state}


def list_findings(instance_id: str, status: str | None = None,
                  limit: int = 50) -> list[dict]:
    sql = ("SELECT finding_id,rule_id,object,fingerprint,status,severity,occurrences,"
           "first_seen,last_seen,fixed_by_change,dismiss_reason FROM finding_lifecycle "
           "WHERE instance_id=?")
    args: list[Any] = [instance_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY last_seen DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [{"finding_id": r[0], "rule_id": r[1], "object": r[2], "fingerprint": r[3],
             "status": r[4], "severity": r[5], "occurrences": r[6],
             "first_seen_iso": _iso(r[7]), "last_seen_iso": _iso(r[8]),
             "fixed_by_change": r[9], "dismiss_reason": r[10]} for r in rows]


def finding_stats(instance_id: str) -> dict:
    """月报素材：新发现/已修复/仍开放/误报率。"""
    with _conn() as c:
        rows = c.execute("SELECT status, COUNT(*), SUM(occurrences) FROM finding_lifecycle "
                         "WHERE instance_id=? GROUP BY status", (instance_id,)).fetchall()
    by = {r[0]: {"count": r[1], "occurrences": r[2] or 0} for r in rows}
    open_n = by.get("open", {}).get("count", 0) + by.get("confirmed", {}).get("count", 0) \
        + by.get("fixing", {}).get("count", 0)
    done = by.get("fixed", {}).get("count", 0) + by.get("verified", {}).get("count", 0)
    dismissed = by.get("dismissed", {}).get("count", 0)
    total = sum(v["count"] for v in by.values())
    return {
        "total": total, "open": open_n, "done": done, "dismissed": dismissed,
        "by_status": by,
        "dismiss_rate": round(dismissed / total, 3) if total else 0.0,
    }


# ------------------------------------------------------------------
# 事故与变更-异常疑似关联
# ------------------------------------------------------------------


def open_incident(instance_id: str, symptom: str, incident_id: str = "") -> str:
    iid = incident_id or f"inc:{instance_id}:{int(time.time())}"
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO incidents(incident_id,instance_id,symptom,opened_at,closed_at,"
            "diagnosis,remediation,timeline) VALUES(?,?,?,?,NULL,?,?,?)",
            (iid, instance_id, symptom[:2000], now, json.dumps({}, ensure_ascii=False),
             json.dumps([], ensure_ascii=False), json.dumps([], ensure_ascii=False)))
    return iid


def update_incident(incident_id: str, diagnosis: dict | None = None,
                    remediation: str = "", timeline_event: dict | None = None) -> bool:
    with _conn() as c:
        r = c.execute("SELECT diagnosis, remediation, timeline FROM incidents WHERE incident_id=?",
                      (incident_id,)).fetchone()
        if not r:
            return False
        diag = json.loads(r[0]) if r[0] else {}
        if diagnosis:
            diag.update(diagnosis)
        rem = remediation if remediation else r[1]
        tl = json.loads(r[2]) if r[2] else []
        if timeline_event:
            tl.append({"ts": time.time(), **timeline_event})
        c.execute("UPDATE incidents SET diagnosis=?, remediation=?, timeline=? WHERE incident_id=?",
                  (json.dumps(diag, ensure_ascii=False), rem,
                   json.dumps(tl, ensure_ascii=False), incident_id))
        return True


def close_incident(incident_id: str) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE incidents SET closed_at=? WHERE incident_id=? AND closed_at IS NULL",
                        (time.time(), incident_id))
        return cur.rowcount > 0


def list_incidents(instance_id: str, open_only: bool = False, limit: int = 20) -> list[dict]:
    sql = ("SELECT incident_id,symptom,opened_at,closed_at,diagnosis,remediation,timeline "
           "FROM incidents WHERE instance_id=?")
    args: list[Any] = [instance_id]
    if open_only:
        sql += " AND closed_at IS NULL"
    sql += " ORDER BY opened_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [{"incident_id": r[0], "symptom": r[1],
             "opened_at_iso": _iso(r[2]), "closed_at_iso": _iso(r[3]) if r[3] else None,
             "diagnosis": json.loads(r[4]) if r[4] else {},
             "remediation": r[5], "timeline": json.loads(r[6]) if r[6] else []}
            for r in rows]


def find_recent_changes(instance_id: str, at_ts: float, window_seconds: float = 1800) -> list[dict]:
    """at_ts 之前 window_seconds 内发生状态迁移的变更单（变更-异常时间关联的左表）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT change_id, state, updated_at FROM changes "
            "WHERE instance_id=? AND updated_at BETWEEN ? AND ? ORDER BY updated_at DESC",
            (instance_id, at_ts - window_seconds, at_ts)).fetchall()
    return [{"change_id": r[0], "state": r[1],
             "delta_seconds": round(at_ts - r[2], 1), "updated_at_iso": _iso(r[2])}
            for r in rows]


def add_suspect_link(anomaly_id: str, change_id: str, delta_seconds: float) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO suspect_links(anomaly_id,change_id,delta_seconds,created_at) "
            "VALUES(?,?,?,?)", (anomaly_id, change_id, delta_seconds, time.time()))


def suspects_for(anomaly_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT change_id, delta_seconds, created_at FROM suspect_links "
            "WHERE anomaly_id=? ORDER BY created_at DESC", (anomaly_id,)).fetchall()
    return [{"change_id": r[0], "delta_seconds": r[1], "created_at_iso": _iso(r[2])}
            for r in rows]


# ------------------------------------------------------------------
# 变更
# ------------------------------------------------------------------


def save_change(instance_id: str, payload: dict) -> None:
    cid = payload.get("change_id")
    if not cid:
        raise ValueError("payload.change_id 不能为空")
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO changes(change_id,instance_id,payload,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (cid, instance_id, json.dumps(payload, ensure_ascii=False, default=str),
             payload.get("state", "draft"), payload.get("created_at", now), now))


def load_change(instance_id: str, change_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT payload FROM changes WHERE change_id=? AND instance_id=?",
                      (change_id, instance_id)).fetchone()
    return json.loads(r[0]) if r else None


def list_changes(instance_id: str, state: str | None = None, limit: int = 20) -> list[dict]:
    sql = "SELECT payload, state, created_at, updated_at FROM changes WHERE instance_id=?"
    args: list[Any] = [instance_id]
    if state:
        sql += " AND state=?"
        args.append(state)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for p, st, ca, ua in rows:
        d = json.loads(p)
        d["state"] = st
        d["created_at_iso"] = _iso(ca)
        d["updated_at_iso"] = _iso(ua)
        out.append(d)
    return out


# ------------------------------------------------------------------
# 指标字典
# ------------------------------------------------------------------


def put_dictionary(kind: str, key: str, payload: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO dictionary(id,kind,payload,updated_at) VALUES(?,?,?,?)",
            (f"{kind}:{key}", kind, json.dumps(payload, ensure_ascii=False, default=str), time.time()))


def get_dictionary(kind: str, key: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT payload, updated_at FROM dictionary WHERE id=?",
                      (f"{kind}:{key}",)).fetchone()
    if not r:
        return None
    d = json.loads(r[0])
    d["updated_at_iso"] = _iso(r[1])
    return d


def list_dictionary(kind: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT id, kind, payload, updated_at FROM dictionary"
    args: list[Any] = []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [
        {"id": r[0], "kind": r[1], **json.loads(r[2]),
         "updated_at_iso": _iso(r[3])}
        for r in rows
    ]


# ------------------------------------------------------------------
# 审计
# ------------------------------------------------------------------


def record_audit(tool: str, instance_id: str | None, summary: str,
                 correlation_id: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO audit_log(ts, tool, instance_id, correlation_id, summary) VALUES(?,?,?,?,?)",
            (time.time(), tool, instance_id, correlation_id, summary[:1000]),
        )


def recent_audit(limit: int = 20, instance_id: str | None = None) -> list[dict]:
    sql = "SELECT ts, tool, instance_id, correlation_id, summary FROM audit_log"
    args: list[Any] = []
    if instance_id:
        sql += " WHERE instance_id=?"
        args.append(instance_id)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [
        {"ts": _iso(r[0]), "tool": r[1],
         "instance_id": r[2], "correlation_id": r[3], "summary": r[4]}
        for r in rows
    ]


# ------------------------------------------------------------------
# 知识包导出/导入 —— 冷启动的第二种解法
# ------------------------------------------------------------------


def export_profile(instance_id: str | None = None, include_observations: bool = True) -> dict:
    """导出脱敏的知识包。

    为什么要这个功能：新客户零历史时，可以先导入一份同行业的匿名基线包作为 Tier1 起点。
    这让"经验"变成可以交付的实体，而不只是脑子里的东西。
    """
    data: dict[str, Any] = {
        "schema": "dbt.profile/v1",
        "schema_version": SCHEMA_VERSION,
        "exported_at": int(time.time()),
        "exported_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "instances": [], "baselines": [], "dictionary": [], "changes": [], "observations": [],
    }
    inst_ids = [instance_id] if instance_id else [i["instance_id"] for i in list_instances(500)]
    for iid in inst_ids:
        inst = get_instance(iid)
        if not inst:
            continue
        # 脱敏：host 保留但去掉 label（label 通常含客户名）
        inst.pop("label", None)
        inst.pop("host", None)
        data["instances"].append(inst)
        bl = _get_baselines(iid)
        for metric, info in bl.items():
            data["baselines"].append({"instance_hash": _anon(iid), "metric": metric, **info})
        for chg in list_changes(iid, limit=200):
            chg.pop("sql", None)
            data["changes"].append(chg)
        if include_observations:
            for kind in ("snapshot", "review", "plan", "incident"):
                for h in get_history(iid, kind=kind, limit=50):
                    data["observations"].append({
                        "instance_hash": _anon(iid), "kind": kind,
                        "ts_iso": h["ts_iso"], "payload": _scrub_payload(h["payload"]),
                    })
    for item in list_dictionary(limit=500):
        data["dictionary"].append(item)
    return data


def _scrub_payload(payload: Any) -> Any:
    """去掉可能的业务敏感信息（库名、表名的具体值保留，因为它们是诊断必需；去掉连接信息）。"""
    if not isinstance(payload, dict):
        return payload
    drop_keys = {"target", "url", "dsn", "password", "user", "host"}
    return {k: (v if k not in drop_keys else "***")
            for k, v in payload.items()
            if not isinstance(v, dict) or k not in drop_keys}


def _anon(instance_id: str) -> str:
    import hashlib
    return "anon:" + hashlib.sha256(instance_id.encode()).hexdigest()[:12]


def import_profile(data: dict, remap_to: str | None = None) -> dict:
    """导入知识包。remap_to 指定把匿名实例的基线挂到当前实例下。

    这是"把别处积累的行业常识带给新客户"的通道，也是长期最值钱的能力之一。
    """
    if not isinstance(data, dict) or data.get("schema") != "dbt.profile/v1":
        raise ValueError("不是有效的 dbt.profile/v1 知识包")
    stats = {"instances": 0, "baselines": 0, "observations": 0, "dictionary": 0, "changes": 0}
    for inst in data.get("instances", []):
        target = remap_to or inst.get("instance_id")
        upsert_instance(
            target,
            dialect=inst.get("dialect", ""), version=inst.get("version", ""),
            database_name=inst.get("database_name", ""),
            meta={"imported": True, "imported_at": time.time(), "original": inst.get("instance_id")},
        )
        stats["instances"] += 1
    if remap_to:
        for b in data.get("baselines", []):
            set_baseline(remap_to, b["metric"], b.get("value", 0), b.get("window_days", 30))
            stats["baselines"] += 1
        for o in data.get("observations", []):
            record_observation(remap_to, o.get("kind", "unknown"), o.get("payload", {}))
            stats["observations"] += 1
    for d in data.get("dictionary", []):
        kind = d.get("kind")
        key = d.get("id", "").split(":", 1)[-1] or d.get("name", "unknown")
        if kind:
            put_dictionary(kind, key, {k: v for k, v in d.items() if k not in ("id", "kind")})
            stats["dictionary"] += 1
    return stats


def store_stats() -> dict:
    path = store_path()
    size = os.path.getsize(path) if os.path.exists(path) else 0
    with _conn() as c:
        counts = {
            t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("observations", "audit_log", "instances", "baselines", "changes",
                      "dictionary", "links", "metric_windows", "metric_baselines",
                      "sql_profiles", "finding_lifecycle", "incidents", "suspect_links")
        }
    return {
        "path": path,
        "size_bytes": size,
        "size_mb": round(size / 1024 / 1024, 3),
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
    }
