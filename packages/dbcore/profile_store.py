"""实例档案 + 审计留痕。

这是整条工具链唯一长期复利的资产：所有 MCP 必须往同一个库里写观测。
别人第一天拿到的是一份 SQL，你拿到的是这张表三个月的历史。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".dbtoolchain")


def store_path() -> str:
    return os.environ.get("DBT_STORE", os.path.join(DEFAULT_DIR, "store.db"))


def _conn() -> sqlite3.Connection:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        """CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            ts REAL NOT NULL,
            payload TEXT NOT NULL
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            tool TEXT NOT NULL,
            instance_id TEXT,
            summary TEXT
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_obs_instance ON observations(instance_id, ts)")
    return c


def record_observation(instance_id: str, kind: str, payload: dict) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO observations(instance_id, kind, ts, payload) VALUES(?,?,?,?)",
            (instance_id, kind, time.time(), json.dumps(payload, ensure_ascii=False)),
        )
        return int(cur.lastrowid or 0)


def get_history(instance_id: str, kind: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT id, kind, ts, payload FROM observations WHERE instance_id=?"
    args: list[Any] = [instance_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [
        {"id": r[0], "kind": r[1], "ts": r[2],
         "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[2])), "payload": json.loads(r[3])}
        for r in rows
    ]


def instance_summary(instance_id: str) -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM observations WHERE instance_id=?", (instance_id,)).fetchone()[0]
        kinds = c.execute(
            "SELECT kind, COUNT(*), MAX(ts) FROM observations WHERE instance_id=? GROUP BY kind ORDER BY 2 DESC",
            (instance_id,),
        ).fetchall()
    return {
        "instance_id": instance_id,
        "total_observations": total,
        "by_kind": [{"kind": k, "count": n, "last_seen": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))} for k, n, ts in kinds],
    }


def record_audit(tool: str, instance_id: str | None, summary: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO audit_log(ts, tool, instance_id, summary) VALUES(?,?,?,?)",
            (time.time(), tool, instance_id, summary[:500]),
        )


def recent_audit(limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT ts, tool, instance_id, summary FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [
        {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0])), "tool": r[1], "instance_id": r[2], "summary": r[3]}
        for r in rows
    ]
