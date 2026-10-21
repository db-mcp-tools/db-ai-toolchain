"""dbwatch MCP —— DBA 值守与根因报告。

★ 这个 MCP 存在的意义就是回答那个质问：

    "你说根因报告依赖积累的档案，但那是客户现场的数据，我拿不到。
     除非客户先买前面几个产品、积累几个月，才能用上值守 —— 那我永远卖不出第一单。"

答案是 **Tier 分级契约**（DESIGN.md §0.2）：
    任何诊断类工具必须在 Tier0（零历史）下可用。

    Tier0  一段症状描述 / 一份快照文本 → 静态指纹库 + 静态基线 → 结论 + 处置清单
    Tier1  连过一次库（capture_snapshot）→ 叠加真实元数据 → 假设收敛到 1~2 条
    Tier2  多次快照形成自身基线 → 趋势、漂移、容量预测

所以销售路径不是"先养几个月"，而是：
    今天就能交第一份报告 → 每次交付自动写档案 → 从第二次起自然升级到 Tier2
    **积累是使用的副产品，不是前置条件。**

工具清单见 DESIGN.md §3.3。

运行： python -m servers.dbwatch
"""

from __future__ import annotations

import json
import time

from servers._shared import register, make_server
from dbcore import bootstrap as BS
from dbcore import rca as RCA
from dbcore import metrics as M
from dbcore import baselines as BL
from dbcore import signature as SG
from dbcore import report as RPT
from dbcore.dialect import normalize_dialect
from dbcore.ids import instance_id_from_dsn
from dbcore.profile_store import (
    record_observation, upsert_instance, get_profile, tier_of,
    get_baseline, compute_baselines, get_history, instance_summary,
)
from dbcore.protocol import Result, ToolError

MCP_NAME = "dbwatch"
VERSION = "0.1.0"
mcp = make_server(MCP_NAME, VERSION)


def _iid(url: str = "", instance_id: str = "") -> str:
    return instance_id or (instance_id_from_dsn(url) if url else "")


# ==================================================================
# Tier0 —— 零依赖，今天就能交付
# ==================================================================


def explain_symptom(symptom: str, dialect: str | None = None, top_n: int = 3,
                    instance_id: str = "", correlation_id: str = "") -> Result:
    """★ Tier0：只给一句症状描述，就能输出检查清单 + 成因 + 处置。零历史依赖。"""
    out = RCA.diagnose_by_text(symptom, dialect, top_n=top_n)
    if not out.get("matched"):
        res = Result(
            ok=True, mcp=MCP_NAME, tool="explain_symptom", data=out,
            verdict="unknown", confidence="low", tier="tier0",
            summary="指纹库中没有匹配项", instance_id=instance_id,
            correlation_id=correlation_id,
        )
        res.warn("换个更具体的说法重试，例如「主从延迟突增」「锁等待超时」「磁盘满了」")
        return res
    primary = out["primary"]
    res = Result(
        ok=True, mcp=MCP_NAME, tool="explain_symptom", data=out,
        verdict="warn", confidence=primary["confidence"], tier="tier0",
        summary=f"匹配到 {len(out['hypotheses'])} 个候选，首选：{primary['title']}",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("signature", f"signature.{primary['id']}", primary["title"],
                     f"文本相似度得分 {primary['score']}")
    res.add_evidence("static_baseline", "knowledge_base",
                     f"指纹库共 {len(SG.all_signatures())} 种已知故障模式", "知识来源")
    res.next(MCP_NAME, "diagnose", symptom=symptom, dialect=dialect,
             reason="用快照数据验证这个假设", priority=2, optional=True)
    res.next(MCP_NAME, "oncall_checklist", symptom=symptom,
             reason="获取按顺序执行的排查清单", priority=2, optional=True)
    res.tier_upgrade = out.get("tier_upgrade")
    return res


def oncall_checklist(symptom: str = "", dialect: str | None = None,
                     instance_id: str = "", correlation_id: str = "") -> Result:
    """★ Tier0：故障时的标准排查顺序（静态知识，随时可用）。"""
    if symptom:
        out = RCA.diagnose_by_text(symptom, dialect, top_n=1)
        matched = out.get("matched")
        checks = out["primary"]["checks"] if matched else []
        title = out["primary"]["title"] if matched else symptom
    else:
        # 没有症状就给通用清单：这是值班手册的第一页
        checks = [
            {"name": "确认影响面与严重度", "kind": "inspect", "target": "",
             "why": "先判断是局部慢还是整体不可用，决定要不要拉人"},
            {"name": "确认最近是否有变更", "kind": "inspect", "target": "",
             "why": "发布、DDL、参数调整、流量切换 —— 大部分故障有变更背景"},
            {"name": "看主机资源（CPU/内存/磁盘/IO）", "kind": "metric",
             "target": "cpu_used_pct,memory_used_pct,disk_used_pct,disk_util_pct",
             "why": "先分清是数据库问题还是机器问题"},
            {"name": "看当前活跃会话", "kind": "sql",
             "target": "SELECT * FROM information_schema.processlist WHERE command<>'Sleep' ORDER BY time DESC LIMIT 20",
             "why": "直接看到正在跑什么"},
            {"name": "看是否有锁等待", "kind": "sql", "target": "lock_waits",
             "why": "锁阻塞的表现与 CPU 打满类似，处置方向完全不同"},
            {"name": "确认主从状态", "kind": "sql", "target": "replica_status",
             "why": "复制延迟会让「读」看起来像是数据丢失"},
            {"name": "确认错误日志", "kind": "inspect", "target": "error_log_tail",
             "why": "OOM kill、磁盘满、主从报错都在错误日志里有第一现场"},
        ]
        title = "通用故障排查清单"
    data = {"title": title, "checks": checks, "count": len(checks)}
    res = Result(
        ok=True, mcp=MCP_NAME, tool="oncall_checklist", data=data,
        verdict="pass", confidence="medium", tier="tier0",
        summary=f"{title}：{len(checks)} 项按顺序执行",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for c in checks[:8]:
        res.add_evidence("signature" if symptom else "external", f"checklist.{c['name']}",
                         c.get("target") or "人工检查", c.get("why", ""))
    return res


def analyze_slow_log(text: str, dialect: str = "mysql", top_n: int = 10,
                     instance_id: str = "", correlation_id: str = "") -> Result:
    """★ Tier0：贴一段慢日志文本就能聚合 TOP SQL，不需要连库。"""
    items = parse_slow_log(text, dialect)
    items.sort(key=lambda x: x.get("total_time", 0), reverse=True)
    top = items[:top_n]
    suspicious = [i for i in items if i.get("full_scan") or i.get("rows_examined", 0) > 100000]
    data = {
        "total": len(items), "top": top, "suspicious_count": len(suspicious),
        "suspicious": suspicious[:10],
        "aggregate": {
            "total_time": round(sum(i.get("total_time", 0) for i in items), 3),
            "avg_time": round(sum(i.get("avg_time", 0) for i in items) / max(len(items), 1), 3),
            "total_rows_examined": sum(i.get("rows_examined", 0) for i in items),
        },
    }
    verdict = "fail" if suspicious else "warn"
    res = Result(
        ok=True, mcp=MCP_NAME, tool="analyze_slow_log", data=data,
        verdict=verdict, confidence="high", tier="tier0",
        summary=f"解析 {len(items)} 类 SQL（去重后），可疑 {len(suspicious)} 类；"
                f"累计耗时 {data['aggregate']['total_time']}s",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("external", "slow_log.parse", f"{len(items)} 类 SQL", "来自用户提供的慢日志文本")
    for i in top[:3]:
        res.next("sql-review", "review_sql", sql=i["sample_sql"], dialect=dialect,
                 reason=f"该类 SQL 累计耗时 {i.get('total_time')}s，需体检",
                 priority=1, optional=False)
    return res


def parse_slow_log(text: str, dialect: str = "mysql") -> list[dict]:
    """解析 MySQL 慢日志格式；PG 的 log_min_duration 也能识别大部分。

    聚合的关键是把具体参数替换掉，否则同一条 SQL 会被拆成几百条。
    """
    from dbcore.ids import normalize_sql_for_fingerprint, sql_fingerprint
    import re

    blocks: list[dict] = []
    cur: dict | None = None
    for line in (text or "").splitlines():
        ls = line.strip()
        if re.match(r"^#\s*Time:", ls) or re.match(r"^#\s*User@Host", ls):
            if cur and cur.get("sql_lines"):
                blocks.append(cur)
            cur = {"sql_lines": [], "lines": []}
            continue
        if cur is None:
            continue
        m = re.match(r"#\s*Query_time:\s*([\d.]+)", ls)
        if m:
            cur["query_time"] = float(m.group(1))
            r = re.search(r"Lock_time:\s*([\d.]+)", ls)
            if r:
                cur["lock_time"] = float(r.group(1))
            r2 = re.search(r"Rows_examined:\s*(\d+)", ls)
            if r2:
                cur["rows_examined"] = int(r2.group(1))
            r3 = re.search(r"Rows_sent:\s*(\d+)", ls)
            if r3:
                cur["rows_sent"] = int(r3.group(1))
            continue
        if ls.startswith("#"):
            continue
        if ls.lower().startswith(("select", "insert", "update", "delete", "replace",
                                  "set timestamp", "use ")):
            if ls.lower().startswith("set timestamp"):
                continue
            cur["sql_lines"].append(ls)

    if cur and cur.get("sql_lines"):
        blocks.append(cur)

    agg: dict[str, dict] = {}
    for b in blocks:
        sql = " ".join(b["sql_lines"]).strip().rstrip(";")
        if not sql:
            continue
        fp = sql_fingerprint(sql)
        e = agg.setdefault(fp, {
            "fingerprint": fp, "sample_sql": sql[:1000], "count": 0,
            "total_time": 0.0, "max_time": 0.0, "rows_examined": 0, "rows_sent": 0,
            "avg_time": 0.0, "full_scan": False,
        })
        e["count"] += 1
        e["total_time"] += b.get("query_time", 0.0)
        e["max_time"] = max(e["max_time"], b.get("query_time", 0.0))
        e["rows_examined"] += b.get("rows_examined", 0)
        e["rows_sent"] += b.get("rows_sent", 0)
        low = sql.lower()
        if dialect in ("mysql", "mariadb", "oceanbase"):
            e["full_scan"] = e["full_scan"] or ("rows_examined" in b and not b.get("rows_sent", 1)
                                                and b.get("rows_examined", 0) > 10000)
        else:
            e["full_scan"] = e["full_scan"] or ("seq scan" in low)
    out = []
    for e in agg.values():
        e["avg_time"] = round(e["total_time"] / max(e["count"], 1), 4)
        e["total_time"] = round(e["total_time"], 3)
        out.append(e)
    return out


# ==================================================================
# Tier1 —— 需要连一次库
# ==================================================================


def capture_snapshot(url: str, dialect: str = "mysql", sections: list[str] | None = None,
                     instance_id: str = "", correlation_id: str = "",
                     max_rows: int = 200, persist: bool = True) -> Result:
    """一次性采集现场信息。这是把 Tier0 升到 Tier1/2 的唯一动作。"""
    d = normalize_dialect(dialect)
    iid = _iid(url, instance_id)
    snap = BS.collect_snapshot(url, d, sections=sections, max_rows=max_rows)
    snap["correlation_id"] = correlation_id
    if persist and iid:
        upsert_instance(iid, dialect=d, version=str(snap.get("version") or ""),
                        database_name=(snap.get("target") or "").rsplit("/", 1)[-1],
                        meta={"captured_at": snap.get("captured_at_iso")})
        record_observation(iid, "snapshot", {"metrics": snap.get("metrics"),
                                             "summary": BS.snapshot_summary(snap)})
    summary = BS.snapshot_summary(snap)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="capture_snapshot",
        data={"snapshot_key": summary, "sections_ok": snap.get("completed_sections"),
              "sections_failed": snap.get("failed_sections"), "errors": snap.get("errors"),
              "metrics": snap.get("metrics"), "top_tables": summary.get("top_tables"),
              "took_ms": snap.get("took_ms")},
        verdict="warn" if snap.get("failed_sections") else "pass",
        confidence="high", tier="tier1",
        summary=f"采集完成：{len(snap.get('completed_sections', []))} 项成功，"
                f"{len(snap.get('failed_sections', []))} 项失败",
        instance_id=iid, correlation_id=correlation_id,
    )
    res.add_evidence("runtime_metric", "snapshot.metrics",
                     f"{len([v for v in snap.get('metrics', {}).values() if v is not None])} 项指标",
                     "本次采集到的真实数值")
    for f in (snap.get("failed_sections") or [])[:5]:
        res.warn(f"采集项 {f} 失败（通常因为权限不足或版本不支持）："
                 f"{str(snap.get('errors', {}).get(f))[:80]}")
    res.next(MCP_NAME, "diagnose", instance_id=iid,
             reason="用刚采集的快照做根因推理", priority=1, optional=False)
    res.tier_upgrade = {"to": "tier2", "need": "至少 2 次快照且间隔 >= 7 天",
                        "how": "对本实例重复执行 capture_snapshot",
                        "gain": "得到自身基线，能发现偏离常态的异常（静态阈值查不出来）"}
    return res


def diagnose(instance_id: str = "", snapshot: dict | None = None, symptom: str | None = None,
             dialect: str | None = None, correlation_id: str = "") -> Result:
    """★ 根因推理。给 instance_id 用档案，给 snapshot 用快照，什么都没有就用症状文本（Tier0）。"""
    if instance_id and not snapshot:
        prof = instance_summary(instance_id)
        if prof.get("total_observations"):
            hist = get_history(instance_id, kind="snapshot", limit=1)
            snapshot = (hist[0]["payload"] if hist else None) or None
    out = RCA.diagnose(snapshot=snapshot, symptom=symptom, dialect=dialect)
    data = dict(out)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="diagnose", data=data,
        verdict="fail" if data.get("primary") else (
            "pass" if data.get("verdict") == "no_signal" else "warn"),
        confidence=(data.get("primary") or {}).get("confidence", "medium"),
        tier=data.get("tier", "tier0"),
        summary=data.get("summary", ""),
        instance_id=instance_id or data.get("instance_id", ""),
        correlation_id=correlation_id,
    )
    for e in (data.get("evidence") or [])[:10]:
        res.add_evidence(e.get("kind", "runtime_metric"), e.get("source", ""),
                         e.get("fact", ""), e.get("supports", ""))
    if data.get("tier") == "tier0":
        res.warn("当前是 Tier0：结论来自静态知识库，尚未结合实例真实指标")
    res.next(MCP_NAME, "incident_report", instance_id=instance_id,
             reason="把这份诊断渲染成可交付的事故报告", priority=2, optional=True)
    return res


def classify_load(instance_id: str = "", snapshot: dict | None = None,
                  correlation_id: str = "") -> Result:
    """判断当前是 CPU / IO / 锁 / 连接 / 复制 哪类压力。处置方向完全不同，第一步必须分清。"""
    metrics: dict = {}
    if snapshot is None and instance_id:
        hist = get_history(instance_id, kind="snapshot", limit=1)
        if hist:
            metrics = (hist[0]["payload"] or {}).get("metrics") or {}
    elif snapshot:
        metrics = snapshot.get("metrics") or {}
    if not metrics:
        raise ToolError("E_NOT_TIER2",
                        "没有可用的指标数据。请先调用 capture_snapshot 采集（只需一次）")
    d = snapshot.get("dialect", "mysql") if snapshot else "mysql"
    dialect = d
    scores = {}
    cpu = metrics.get("cpu_used_pct")
    if cpu is not None:
        scores["cpu"] = max(0.0, (cpu - BL.value(dialect, "cpu_used_pct", 80)) / 20 * 100)
    active = metrics.get("threads_active")
    if active is not None:
        scores["connection"] = max(0.0, (active - BL.value(dialect, "threads_active", 50)) / 100 * 100)
    lock = metrics.get("lock_wait_count")
    if lock is not None:
        scores["lock"] = 100.0 if lock > 0 else 0.0
    lag = metrics.get("repl_lag_seconds")
    if lag is not None:
        scores["replication"] = max(0.0, (lag - BL.value(dialect, "repl_lag_warn_s", 10)) / 50 * 100)
    idle = metrics.get("idle_in_transaction")
    if idle:
        scores["lock"] = max(scores.get("lock", 0), 60.0)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    primary_cat = ranked[0][0] if ranked and ranked[0][1] > 0 else "none"
    advice = {
        "cpu": "看 TOP SQL：要么是慢 SQL 要么就是规格不足。先用 analyze_slow_log 找累计耗时最大的",
        "connection": "连接池积压或泄漏：区分是慢查询堆积还是连接未归还（看 threads_sleep 占比）",
        "lock": "先看谁阻塞谁（analyze_lock），再决定 kill 谁。kill 前必须评估回滚耗时",
        "replication": "先确认是大事务还是回放能力不足，处置方式完全不同",
        "none": "各项指标均在阈值内，压力源的判断需要更多维度（建议做 capture_snapshot）",
    }.get(primary_cat, "")
    res = Result(
        ok=True, mcp=MCP_NAME, tool="classify_load",
        data={"scores": {k: round(v, 1) for k, v in scores.items()},
              "primary": primary_cat, "advice": advice, "metrics_used": metrics},
        verdict="fail" if primary_cat != "none" else "pass",
        confidence="high", tier="tier1",
        summary=f"压力类型判定：{primary_cat}｜{advice}",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for k, v in ranked[:3]:
        res.add_evidence("runtime_metric", f"load.{k}", f"得分 {round(v, 1)}", "压力分类依据")
    if primary_cat == "cpu":
        res.next("sql-review", "review_sql", dialect=d,
                 reason="CPU 压力通常来自慢 SQL", priority=1, optional=False)
    elif primary_cat == "lock":
        res.next(MCP_NAME, "analyze_lock", instance_id=instance_id,
                 reason="需要定位阻塞源头", priority=1, optional=False)
    elif primary_cat == "replication":
        res.next(MCP_NAME, "check_replication", instance_id=instance_id,
                 reason="检查复制状态", priority=1, optional=False)
    return res


def analyze_lock(url: str, dialect: str = "mysql", instance_id: str = "",
                 correlation_id: str = "") -> Result:
    """锁等待链：谁阻塞谁。**kill 前必须评估回滚耗时**"""
    from dbcore.connectors import run_readonly
    d = normalize_dialect(dialect)
    if d in ("mysql", "mariadb", "oceanbase"):
        sql = ("SELECT waiting_pid, waiting_query, waiting_age_secs, blocking_pid, "
               "blocking_query, blocking_age_secs, blocking_trx_rows_modified "
               "FROM sys.innodb_lock_waits ORDER BY waiting_age_secs DESC LIMIT 20")
    else:
        sql = ("SELECT blocked.pid AS waiting_pid, left(blocked.query,120) AS waiting_query, "
               "EXTRACT(EPOCH FROM (now()-blocked.query_start))::int AS waiting_age_secs, "
               "blocking.pid AS blocking_pid, left(blocking.query,120) AS blocking_query, "
               "EXTRACT(EPOCH FROM (now()-blocking.xact_start))::int AS blocking_age_secs "
               "FROM pg_stat_activity blocked "
               "JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid)) "
               "WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0")
    try:
        rr = run_readonly(url, sql, d, max_rows=50)
    except Exception as exc:
        raise ToolError("E_CONNECT", f"锁查询失败：{exc}")
    cols = rr.get("columns", [])
    rows = [dict(zip(cols, r)) for r in rr.get("rows", [])]
    iid = _iid(url, instance_id)
    safe_threshold = BL.value(d, "transaction_duration_s", 10)
    for r in rows:
        rows_modified = r.get("blocking_trx_rows_modified") or 0
        age = r.get("blocking_age_secs") or 0
        r["kill_advice"] = (
            f"该事务已运行 {age}s、修改过 {rows_modified} 行。"
            f"{'★ 回滚可能耗时数倍于已执行时间，kill 前务必评估' if (rows_modified or 0) > 100000 else '影响面可控，可在确认后 kill'}"
        )
        r["kill_safe"] = (rows_modified or 0) <= 100000 and (age or 0) <= safe_threshold * 6
    res = Result(
        ok=True, mcp=MCP_NAME, tool="analyze_lock",
        data={"waits": rows, "count": len(rows), "columns": cols},
        verdict="fail" if rows else "pass", confidence="high", tier="tier1",
        summary=f"当前 {len(rows)} 条锁等待"
                + (f"，最长等待 {rows[0].get('waiting_age_secs')}s" if rows else "，无锁等待"),
        instance_id=iid, correlation_id=correlation_id,
    )
    for r in rows[:5]:
        res.add_evidence("runtime_metric", "lock_waits",
                         f"pid {r.get('waiting_pid')} 被 {r.get('blocking_pid')} 阻塞 "
                         f"{r.get('waiting_age_secs')}s", r.get("kill_advice", ""))
    if rows:
        res.warn("kill 阻塞源之前必须评估回滚耗时：大事务回滚可能比继续等待更糟")
        res.next(MCP_NAME, "diagnose", instance_id=iid, symptom="锁等待",
                 reason="结合指纹库给出处置建议", priority=2, optional=True)
    return res


def detect_blocking_chain(url: str, dialect: str = "mysql", instance_id: str = "",
                          correlation_id: str = "") -> Result:
    """递归阻塞检测：找出真正的源头（被 kill 的应该是源头，不是中间受害者）。"""
    from dbcore.connectors import run_readonly
    d = normalize_dialect(dialect)
    waits = []
    try:
        if d in ("mysql", "mariadb", "oceanbase"):
            rr = run_readonly(url, "SELECT requesting_trx_id, blocking_trx_id FROM "
                                   "performance_schema.data_lock_waits", d, max_rows=500)
            pairs = [(str(r[0]), str(r[1])) for r in rr.get("rows", [])]
            rr2 = run_readonly(url, "SELECT trx_id,trx_started,trx_rows_modified,"
                                    "TIMESTAMPDIFF(SECOND,trx_started,NOW()) AS dur,"
                                    "left(trx_query,120) FROM information_schema.innodb_trx",
                               d, max_rows=500)
            info = {str(r[0]): {"started": r[1], "rows_modified": r[2],
                                "duration": r[3], "query": r[4]} for r in rr2.get("rows", [])}
            blocked_by = {a: b for a, b in pairs}
        else:
            rr = run_readonly(url, "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity "
                                   "WHERE cardinality(pg_blocking_pids(pid)) > 0", d, max_rows=500)
            blocked_by = {str(r[0]): str((r[1] or [None])[0]) for r in rr}
            rr2 = run_readonly(url, "SELECT pid,xact_start,left(query,120) FROM pg_stat_activity",
                               d, max_rows=500)
            info = {str(r[0]): {"started": r[1], "query": r[2]} for r in rr2.get("rows", [])}
    except Exception as exc:
        raise ToolError("E_CONNECT", f"阻塞链检测失败：{exc}")

    def root_of(pid: str, depth: int = 0) -> str:
        seen = {pid}
        cur = pid
        while depth < 20:
            nxt = blocked_by.get(cur)
            if not nxt or nxt in seen or nxt == cur:
                return cur
            seen.add(nxt)
            cur = nxt
            depth += 1
        return cur

    roots: dict[str, list[str]] = {}
    for victim in blocked_by:
        r = root_of(victim)
        roots.setdefault(r, []).append(victim)
    chains = [{"root": r, "victims": sorted(v), "victim_count": len(v),
               "info": info.get(r, {}),
               "note": "这个是源头，kill 应该针对它；受害者不需要处理"}
              for r, v in roots.items()]
    chains.sort(key=lambda c: -(c["victim_count"]))
    res = Result(
        ok=True, mcp=MCP_NAME, tool="detect_blocking_chain",
        data={"chains": chains, "root_count": len(chains),
              "total_blocked": sum(c["victim_count"] for c in chains)},
        verdict="fail" if chains else "pass", confidence="high", tier="tier1",
        summary=f"{len(chains)} 个阻塞源头，共阻塞 {sum(c['victim_count'] for c in chains)} 个会话",
        instance_id=_iid(url, instance_id), correlation_id=correlation_id,
    )
    for c in chains[:5]:
        res.add_evidence("runtime_metric", f"blocking.root.{c['root']}",
                         f"阻塞 {c['victim_count']} 个会话", str(c.get("info"))[:120])
    return res


def check_replication(url: str, dialect: str = "mysql", instance_id: str = "",
                      correlation_id: str = "") -> Result:
    """复制状态检查：延迟 / 线程状态 / 错误码 → 修复顺序"""
    from dbcore.connectors import run_readonly
    d = normalize_dialect(dialect)
    rows = []
    try:
        if d in ("mysql", "mariadb", "oceanbase"):
            sql = "SHOW REPLICA STATUS"
            try:
                rr = run_readonly(url, sql, d, max_rows=1)
            except Exception:
                rr = run_readonly(url, "SHOW SLAVE STATUS", d, max_rows=1)
            if rr.get("rows"):
                rows = [dict(zip(rr["columns"], rr["rows"][0]))]
        else:
            rr = run_readonly(url, "SELECT client_addr,state,sync_state,"
                                   "COALESCE(EXTRACT(EPOCH FROM replay_lag),0) AS replay_lag_s,"
                                   "COALESCE(EXTRACT(EPOCH FROM write_lag),0) AS write_lag_s "
                                   "FROM pg_stat_replication", d, max_rows=10)
            rows = [dict(zip(rr["columns"], r)) for r in rr.get("rows", [])]
    except Exception as exc:
        raise ToolError("E_CONNECT", f"复制状态查询失败：{exc}")

    lag = None
    io_running = sql_running = None
    last_err = None
    if d in ("mysql", "mariadb", "oceanbase") and rows:
        r = rows[0]
        lag = float(r.get("Seconds_Behind_Master") or r.get("Seconds_Behind_Source") or 0) or None
        io_running = str(r.get("Replica_IO_Running") or r.get("Slave_IO_Running") or "")
        sql_running = str(r.get("Replica_SQL_Running") or r.get("Slave_SQL_Running") or "")
        last_err = (r.get("Last_SQL_Error") or r.get("Last_IO_Error") or "").strip() or None
    elif rows:
        lag = max(float(r.get("replay_lag_s") or 0) for r in rows) or None

    warn_t = BL.value(d, "repl_lag_warn_s", 10)
    crit_t = BL.value(d, "repl_lag_critical_s", 60)
    if lag is None:
        verdict = "unknown"
    elif lag >= crit_t:
        verdict = "fail"
    elif lag >= warn_t:
        verdict = "warn"
    else:
        verdict = "pass"
    if io_running and io_running.lower() not in ("yes", "connecting"):
        verdict = "fail"
    if sql_running and sql_running.lower() != "yes":
        verdict = "fail"

    data = {"lag_seconds": lag, "io_running": io_running, "sql_running": sql_running,
            "last_error": last_err, "rows": rows[:5],
            "thresholds": {"warn": warn_t, "critical": crit_t}}
    res = Result(
        ok=True, mcp=MCP_NAME, tool="check_replication", data=data,
        verdict=verdict, confidence="high", tier="tier1",
        summary=(f"延迟 {lag}s" if lag is not None else "非副本实例或未取到复制状态")
                 + (f"｜错误：{last_err[:120]}" if last_err else ""),
        instance_id=_iid(url, instance_id), correlation_id=correlation_id,
    )
    if lag is not None:
        res.add_evidence("runtime_metric", "repl.lag", f"{lag}s",
                         f"告警阈值 {warn_t}s / 严重 {crit_t}s "
                         f"（来源 {BL.cite(d, 'repl_lag_warn_s')}）")
    if last_err:
        code = _err_code(last_err)
        res.add_evidence("runtime_metric", "repl.last_error", last_err[:200],
                         "复制中断的直接原因")
        res.next(MCP_NAME, "diagnose", symptom=f"复制中断 {code}".strip(),
                 reason=f"按错误码 {code} 查处置方案", priority=1, optional=False)
    elif lag and lag >= warn_t:
        res.next(MCP_NAME, "diagnose", symptom="主从延迟突增",
                 reason="延迟过大需要定位根因", priority=1, optional=False)
    return res


def _err_code(text: str) -> str:
    import re
    m = re.search(r"\b(1\d{3})\b", text or "")
    return m.group(1) if m else ""


def health_score(instance_id: str = "", url: str = "", dialect: str = "mysql",
                 correlation_id: str = "") -> Result:
    """实例健康评分 + 扣分明细。有基线时对比基线，没有就用静态阈值。"""
    metrics: dict = {}
    if not instance_id and url:
        instance_id = _iid(url)
    hist = get_history(instance_id, kind="snapshot", limit=1) if instance_id else []
    if hist:
        payload = hist[0]["payload"] or {}
        metrics = payload.get("metrics") or {}
    if not metrics:
        raise ToolError("E_NOT_TIER2",
                        "没有快照数据。先调用 capture_snapshot（一次即可）")
    d = dialect
    alerts = M.evaluate({k: v for k, v in metrics.items() if v is not None})
    score = 100
    deductions = []
    for a in alerts:
        pen = 20 if a["level"] == "critical" else 8
        score -= pen
        deductions.append({"metric": a["metric"], "value": a["value"],
                           "penalty": pen, "reason": f"超过 {a['level']} 阈值 {a['threshold']}"})
    baseline_alerts = []
    base = get_baseline(instance_id, "connections_used") if instance_id else None
    if base:
        cur = metrics.get("connections_used")
        if cur is not None:
            dev = (cur - base["value"]) / max(base["value"], 1) * 100
            if abs(dev) > 50:
                score -= 10
                baseline_alerts.append({"metric": "connections_used", "current": cur,
                                        "baseline": base["value"], "deviation_pct": round(dev, 1)})
    score = max(0, min(100, score))
    tier = "tier2" if base else "tier1"
    res = Result(
        ok=True, mcp=MCP_NAME, tool="health_score",
        data={"score": score, "deductions": deductions, "baseline_deviation": baseline_alerts,
              "metrics_used": metrics,
              "grade": "优" if score >= 90 else "良" if score >= 75 else
                       "差" if score >= 60 else "危险"},
        verdict="pass" if score >= 90 else ("warn" if score >= 75 else "fail"),
        confidence="high", tier=tier,
        summary=f"健康分 {score}（{res_grade(score)}），{len(deductions)} 项扣分",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for dd in deductions[:6]:
        res.add_evidence("runtime_metric", f"health.{dd['metric']}", f"{dd['value']}",
                         f"扣 {dd['penalty']} 分：{dd['reason']}")
    if not base:
        res.tier_upgrade = {"to": "tier2", "need": "多次快照形成自身基线",
                            "how": "定期执行 capture_snapshot",
                            "gain": "健康分将对比该实例自身常态，而不是通用阈值"}
    return res


def res_grade(score: int) -> str:
    return "优" if score >= 90 else "良" if score >= 75 else "差" if score >= 60 else "危险"


# ==================================================================
# Tier2 —— 需要历史
# ==================================================================


def capacity_forecast(instance_id: str, metric: str = "connections_used",
                      days_ahead: int = 30, correlation_id: str = "") -> Result:
    """基于历史趋势做容量预测。**样本不足时明确拒绝，不编造趋势**"""
    hist = get_history(instance_id, kind="snapshot", limit=500)
    points = [(h["ts"], (h["payload"] or {}).get("metrics", {}).get(metric)) for h in hist]
    pts = [(t, v) for t, v in points if isinstance(v, (int, float))]
    pts.sort()
    if len(pts) < 3:
        raise ToolError("E_NOT_TIER2",
                        f"容量预测需要至少 3 个有效数据点，当前只有 {len(pts)} 个。"
                        f"先持续采集一段时间（这是唯一能让它工作的办法）")
    xs = [t for t, _ in pts]
    ys = [v for _, v in pts]
    n = len(pts)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((x - mx) ** 2 for x in xs) or 1e-9
    slope = num / den                                  # 每秒变化量
    per_day = slope * 86400
    future_ts = xs[-1] + days_ahead * 86400
    predicted = ys[-1] + per_day * days_ahead
    yhat = [my + slope * (x - mx) for x in xs]
    ss_res = sum((ys[i] - yhat[i]) ** 2 for i in range(n))
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-9
    r2 = 1 - ss_res / ss_tot
    confident = r2 > 0.5 and abs(per_day) > 1e-9
    res = Result(
        ok=True, mcp=MCP_NAME, tool="capacity_forecast",
        data={"metric": metric, "samples": n, "days_ahead": days_ahead,
              "current": ys[-1], "predicted": round(predicted, 2),
              "change_per_day": round(per_day, 4), "r2": round(r2, 3),
              "predicted_at": time.strftime("%Y-%m-%d", time.localtime(future_ts)),
              "history": [{"ts_iso": time.strftime("%Y-%m-%d", time.localtime(t)), "value": v}
                          for t, v in pts[-20:]],
              "confident": confident},
        verdict="warn" if (confident and abs(predicted) > abs(ys[-1]) * 2) else "pass",
        confidence="high" if confident else "low", tier="tier2",
        summary=f"{metric}：当前 {ys[-1]}，{days_ahead} 天后预计 {round(predicted, 2)}"
                + ("" if confident else "（样本趋势不明显，预测仅供参考）"),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("history", f"snapshot.{metric}", f"{n} 个样本，R²={round(r2, 3)}",
                     "线性外推的拟合质量")
    if not confident:
        res.warn("历史波动大或趋势不明显，线性外推不可靠。样本越多预测越准")
    return res


def anomaly_report(instance_id: str, window_days: int = 30,
                   correlation_id: str = "") -> Result:
    """与自身历史基线比对发现异常。**没有历史时退化为静态阈值告警，并明确说明**"""
    hist = get_history(instance_id, kind="snapshot", limit=500,
                       since=time.time() - window_days * 86400)
    if len(hist) < 2:
        latest = hist[0]["payload"].get("metrics", {}) if hist else {}
        alerts = M.evaluate({k: v for k, v in latest.items() if v is not None})
        res = Result(
            ok=True, mcp=MCP_NAME, tool="anomaly_report",
            data={"mode": "static_threshold", "samples": len(hist), "alerts": alerts},
            verdict="fail" if alerts else "pass", confidence="medium", tier="tier1",
            summary=f"仅 {len(hist)} 个样本，退化为静态阈值告警（{len(alerts)} 项）",
            instance_id=instance_id, correlation_id=correlation_id,
        )
        res.warn("静态阈值只能发现已知模式的异常；要发现偏离自身常态的异常需要更多历史样本")
        res.tier_upgrade = {"to": "tier2", "need": f"{window_days} 天内至少 7 个样本",
                            "how": "每日固定时间执行 capture_snapshot",
                            "gain": "与自身基线比对，能发现静态阈值发现不了的异常"}
        return res

    series: dict[str, list[float]] = {}
    for h in hist:
        for k, v in ((h["payload"] or {}).get("metrics") or {}).items():
            if isinstance(v, (int, float)):
                series.setdefault(k, []).append(float(v))
    anomalies = []
    baselines = compute_baselines(instance_id, window_days) if len(hist) >= 3 else {}
    for k, vs in series.items():
        if len(vs) < 2:
            continue
        mean = sum(vs) / len(vs)
        var = sum((x - mean) ** 2 for x in vs) / max(len(vs) - 1, 1)
        sd = var ** 0.5
        latest = vs[-1]
        if sd > 1e-9:
            z = (latest - mean) / sd
            if abs(z) >= 2:
                anomalies.append({"metric": k, "latest": latest, "mean": round(mean, 3),
                                  "stddev": round(sd, 3), "z_score": round(z, 2),
                                  "direction": "高于常态" if z > 0 else "低于常态",
                                  "samples": len(vs)})
    anomalies.sort(key=lambda a: -abs(a["z_score"]))
    res = Result(
        ok=True, mcp=MCP_NAME, tool="anomaly_report",
        data={"mode": "self_baseline", "samples": len(hist), "window_days": window_days,
              "anomalies": anomalies, "baselines": baselines},
        verdict="fail" if any(abs(a["z_score"]) >= 3 for a in anomalies)
                else ("warn" if anomalies else "pass"),
        confidence="high", tier="tier2",
        summary=f"基于自身基线发现 {len(anomalies)} 项异常（样本 {len(hist)}）",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for a in anomalies[:6]:
        res.add_evidence("history", f"anomaly.{a['metric']}",
                         f"当前 {a['latest']}，常态均值 {a['mean']}（z={a['z_score']}）",
                         f"{a['direction']}，偏离超过 2 个标准差")
    return res


# ==================================================================
# 输出 / 归档
# ==================================================================


def incident_report(instance_id: str = "", symptom: str = "", title: str = "",
                    correlation_id: str = "") -> Result:
    """渲染成可交付的事故报告（markdown）。

    ★ 这是"今天就能给客户的第一份交付物"：Tier0 也能生成。
    """
    diagnosis = None
    if instance_id:
        hist = get_history(instance_id, kind="snapshot", limit=1)
        snap = hist[0]["payload"] if hist else None
        try:
            diagnosis = RCA.diagnose(snapshot=snap, symptom=symptom)
        except Exception:
            diagnosis = None
    if diagnosis is None:
        diagnosis = RCA.diagnose(symptom=symptom or "未知现象") if symptom else None
    if diagnosis is None:
        raise ToolError("E_ARGS", "需要提供 instance_id 或 symptom 之一")
    md = RCA.render_diagnosis(diagnosis)
    if title:
        md = f"# {title}\n\n{md}"
    return Result(
        ok=True, mcp=MCP_NAME, tool="incident_report",
        data={"markdown": md, "diagnosis": {k: v for k, v in diagnosis.items()
                                            if k in ("tier", "summary", "primary", "hypotheses")}},
        verdict="warn", confidence=(diagnosis.get("primary") or {}).get("confidence", "medium"),
        tier=diagnosis.get("tier", "tier0"),
        summary=f"已生成事故报告（{diagnosis.get('tier')}，"
                f"{len(md.splitlines())} 行）",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def record_observation_tool(instance_id: str, kind: str, payload: dict,
                            correlation_id: str = "") -> Result:
    """把一次观测写入共享档案。这是让 Tier0 → Tier2 升级的唯一写入点。"""
    if not isinstance(payload, dict):
        raise ToolError("E_ARGS", "payload 必须是 dict")
    oid = record_observation(instance_id, kind, payload)
    return Result(
        ok=True, mcp=MCP_NAME, tool="record_observation",
        data={"observation_id": oid, "new_tier": tier_of(instance_id)},
        verdict="pass", confidence="high", tier=tier_of(instance_id),
        summary=f"已记录观测 #{oid}，该实例当前能力档位：{tier_of(instance_id)}",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def list_metrics_tool(dialect: str = "mysql", instance_id: str = "",
                      correlation_id: str = "") -> Result:
    """列出该引擎支持的逻辑指标（便于客户理解采集了什么）。"""
    d = normalize_dialect(dialect)
    return Result(
        ok=True, mcp=MCP_NAME, tool="list_metrics",
        data={"supported": M.supported_metrics(d), "all_metrics": M.list_metrics()},
        verdict="pass", confidence="high", tier="tier0",
        summary=f"{d} 支持采集 {len(M.supported_metrics(d))} 项逻辑指标",
        instance_id=instance_id, correlation_id=correlation_id,
    )


TOOLS = {
    "capture_snapshot": capture_snapshot,
    "classify_load": classify_load,
    "diagnose": diagnose,
    "explain_symptom": explain_symptom,
    "analyze_slow_log": analyze_slow_log,
    "analyze_lock": analyze_lock,
    "detect_blocking_chain": detect_blocking_chain,
    "check_replication": check_replication,
    "capacity_forecast": capacity_forecast,
    "health_score": health_score,
    "anomaly_report": anomaly_report,
    "incident_report": incident_report,
    "oncall_checklist": oncall_checklist,
    "record_observation": record_observation_tool,
    "list_metrics": list_metrics_tool,
}

for _n, _f in TOOLS.items():
    register(mcp, MCP_NAME, _f, _n)


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
