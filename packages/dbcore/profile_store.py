"""实例档案 —— 整条工具链的唯一数据落点。

为什么必须单一落点：
  每个 MCP 各存一份日志 = 一堆能被单独替换的原子能力，没有复利。
  所有 MCP 往同一个库写观测 = 别人第一天拿到一条 SQL，
  你拿到的是这张表三个月的历史。**这个文件才是资产，MCP 只是它的入口。**

冷启动相关（关键）：
  档案不足时 tier 计算必须如实返回 tier0/tier1，并在需要历史的工具里显式报错，
  而不是悄悄给出基于空数据的结论。见 tier_of()。

兼容约定：
  保持 record_observation / get_history / instance_summary / record_audit / recent_audit 的旧签名，
  旧调用方无需改动。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".dbtoolchain")

SCHEMA_VERSION = 2

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        ts REAL NOT NULL,
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
    "CREATE INDEX IF NOT EXISTS idx_obs_instance ON observations(instance_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_obs_kind ON observations(instance_id, kind, ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_instance ON audit_log(instance_id, ts)",
]


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


# 老版本库可能缺后加的列；sqlite 没有 ALTER TABLE ADD COLUMN IF NOT EXISTS，
# 只能查表结构后按需补。这一步必须做，否则旧档案库会让所有新功能报错。
_EXTRA_COLUMNS = {
    "audit_log": {"correlation_id": "TEXT"},
}


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
        "first_seen_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[8] or 0)),
        "last_seen_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[9] or 0)),
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
# 观测
# ------------------------------------------------------------------


def record_observation(instance_id: str, kind: str, payload: Any) -> int:
    if not isinstance(payload, dict):
        payload = {"value": payload}
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO observations(instance_id, kind, ts, payload) VALUES(?,?,?,?)",
            (instance_id, kind, time.time(), json.dumps(payload, ensure_ascii=False, default=str)),
        )
        c.execute("UPDATE instances SET last_seen=? WHERE instance_id=?", (time.time(), instance_id))
        return int(cur.lastrowid or 0)


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
            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[2])),
            "payload": payload,
        })
    return out


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
            {"kind": k, "count": n, "last_seen": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))}
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
        "dictionary": list_dictionary(limit=20),
        "recent_audit": recent_audit(limit=10, instance_id=instance_id),
    }


# ------------------------------------------------------------------
# 基线（Tier2 的数据来源）
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
            "updated_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[2]))}


def _get_baselines(instance_id: str) -> dict[str, dict]:
    with _conn() as c:
        rows = c.execute("SELECT metric, value, window_days, updated_at FROM baselines "
                         "WHERE instance_id=?", (instance_id,)).fetchall()
    return {
        r[0]: {"value": r[1], "window_days": r[2],
               "updated_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[3]))}
        for r in rows
    }


def compute_baselines(instance_id: str, window_days: int = 30, metrics: Iterable[str] = ()) -> dict:
    """从已有观测计算基线。样本不足时明确拒绝，绝不拿 1 个点冒充基线。"""
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
        d["created_at_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ca or 0))
        d["updated_at_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ua or 0))
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
    d["updated_at_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[1]))
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
         "updated_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[3]))}
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
        {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0])), "tool": r[1],
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
            for t in ("observations", "audit_log", "instances", "baselines", "changes", "dictionary", "links")
        }
    return {
        "path": path,
        "size_bytes": size,
        "size_mb": round(size / 1024 / 1024, 3),
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
    }
