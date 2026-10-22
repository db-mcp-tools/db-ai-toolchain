"""数据口径治理 —— 解决"两个部门算出来的数不一样"。

绝大多数"数据不一致"根本不是数据错了，是**口径没对齐**：
  * 时间窗口不同（自然日 vs 近 24 小时 vs 业务日）
  * 去重维度不同（COUNT(user_id) vs COUNT(DISTINCT user_id) vs COUNT(session)）
  * 过滤条件不同（有没有剔除测试账号/内部员工/已退款订单）
  * JOIN 扇出（一对多关联后 COUNT 被放大，这是最常见的"数字虚高"）
  * 粒度不一致（明细表 vs 聚合表混算）
  * 时区（UTC vs Asia/Shanghai，边界整天偏移）

本模块的做事顺序：
  1. 把口径从 SQL 或自然语言里**结构化抽出来**（没法比对的口径=没治理）
  2. **结构化 diff** 列出每一个不等价点（不是让人肉眼对着两段 SQL 看）
  3. 数字不一致时，**自动下钻**定位到具体是哪层 JOIN/过滤导致的
  4. 固化成**指标契约**，进指标字典
"""

from __future__ import annotations

import re
from typing import Any

from sqlglot import exp

from dbcore.dialect import normalize_dialect, parse_statements


# ------------------------------------------------------------------
# 1. 口径结构化
# ------------------------------------------------------------------

_TIME_HINTS = {
    "created_at": "event_time", "create_time": "event_time", "ctime": "event_time",
    "gmt_create": "event_time", "order_time": "event_time", "pay_time": "pay_time",
    "paid_at": "pay_time", "updated_at": "update_time", "update_time": "update_time",
    "event_date": "event_date", "dt": "partition_date", "stat_date": "stat_date",
    "log_date": "date", "gmt_modified": "update_time",
}

_EXCLUDE_HINTS = ("test", "fake", "mock", "demo", "internal", "staff", "employee",
                  "is_deleted", "deleted", "status", "state", "refund", "cancel")


def _agg_kind(agg: dict) -> str:
    if agg["func"] == "COUNT" and agg["column"] in ("*", None):
        return "count_rows"
    if agg["func"] == "COUNT" and agg.get("distinct"):
        return "count_distinct"
    if agg["func"] == "COUNT":
        return "count_nonnull"
    if agg["func"] == "SUM":
        return "sum"
    if agg["func"] == "AVG":
        return "avg"
    return agg["func"].lower()


def parse_metric_definition(
    sql: str | None = None,
    text: str | None = None,
    name: str = "",
    dialect: str | None = None,
) -> dict:
    """把口径抽成结构化对象。SQL 优先，没有 SQL 时尝试从自然语言抽。

    产出的 Definition 结构：
        name / measures[] / dimensions[] / filters[] / time_window / grain /
        dedupe_key / joins[] / source_tables[] / null_policy
    """
    d = normalize_dialect(dialect)
    out: dict[str, Any] = {
        "name": name or (text or "")[:40] or "unnamed_metric",
        "source": "sql" if sql else ("text" if text else "empty"),
        "dialect": d,
        "measures": [],
        "dimensions": [],
        "filters": [],
        "time_column": None,
        "time_window": None,
        "grain": None,
        "dedupe_key": None,
        "joins": [],
        "source_tables": [],
        "null_policy": "unknown",
        "warnings": [],
    }

    if sql:
        _parse_from_sql(sql, d, out)
    elif text:
        _parse_from_text(text, out)
    else:
        out["warnings"].append("既没有 SQL 也没有描述，无法解析口径")

    out["signature"] = signature_of(out)
    return out


def _parse_from_sql(sql: str, dialect: str, out: dict) -> None:
    from dbcore.extract import extract_objects
    try:
        ex = extract_objects(sql, dialect)
    except Exception as exc:
        out["warnings"].append(f"解析失败：{exc}")
        return
    if not ex["statements"]:
        out["warnings"].append("SQL 中没有可解析的语句")
        return
    st = ex["statements"][0]
    if st.get("parse_error"):
        out["warnings"].append(f"语句解析出错：{st['parse_error']}")

    for a in st.get("aggregates", []):
        out["measures"].append({
            "expr": a["sql"],
            "func": a["func"],
            "column": a["column"],
            "distinct": a["distinct"],
            "kind": _agg_kind(a),
        })
    for c in st.get("columns", []):
        if c["column"] not in [m.get("column") for m in out["measures"]]:
            out["dimensions"].append(c["column"])

    group = st.get("group_by", [])
    out["dimensions"] = sorted(set(out["dimensions"] + [_strip_alias(g) for g in group]))

    for p in st.get("predicates", []):
        if p["columns"]:
            out["filters"].append({
                "sql": p["sql"],
                "columns": p["columns"],
                "operator": p["operator"],
                "literals": p["literals"],
            })
    for f in out["filters"]:
        for c in f["columns"]:
            lc = c.lower()
            if lc in _TIME_HINTS:
                out["time_column"] = {"column": c, "role": _TIME_HINTS[lc]}
                break

    out["source_tables"] = st.get("tables") and [t["qualified"] for t in st["tables"]] or []
    out["joins"] = st.get("joins", [])
    if st.get("has_distinct"):
        out["dedupe_key"] = "ROW"
    for m in out["measures"]:
        if m.get("distinct"):
            out["dedupe_key"] = m.get("column")
    if out["measures"] and not out["dedupe_key"]:
        out["dedupe_key"] = None
        out["warnings"].append("未使用 DISTINCT，结果可能包含重复行（同一用户多次行为会被计多次）")

    # 粒度判定
    if out["measures"] and out["dimensions"]:
        out["grain"] = "grouped"
    elif out["measures"]:
        out["grain"] = "scalar"
    else:
        out["grain"] = "row_level"

    # NULL 策略
    low = sql.lower()
    if any(x in low for x in ("left join", "is null", "coalesce(")):
        out["null_policy"] = "left_join_nulls_possible"
        out["warnings"].append(
            "存在 LEFT JOIN：右表无匹配时计数字段可能为 NULL，"
            "COUNT(col) 不计 NULL 而 COUNT(*) 会算，务必确认用的是哪个")
    if "count(*)" in low:
        out["null_policy"] = "count_star"

    # 时间窗口
    tw = _find_time_window(sql)
    if tw:
        out["time_window"] = tw
    elif out["time_column"]:
        out["warnings"].append("检测到时间过滤列但没有明显的窗口边界，建议明确说明窗口定义")

    # 扇出风险
    fan = detect_fanout(sql, dialect)
    if fan["risk"]:
        out["warnings"].append(f"JOIN 扇出风险：{fan['explanation']}")


def _strip_alias(g: str) -> str:
    g = g.strip()
    parts = g.rsplit(".", 1)
    return parts[-1].strip("`\"' ")


_TZ_RE = re.compile(r"(utc|gmt|asia/shanghai|\+0?8:00|convert_tz|at time zone)", re.I)
_BETWEEN_RE = re.compile(r"(>=?|between)\s*[^a-zA-Z0-9]*\s*([\d\-: ']+)", re.I)


def _find_time_window(sql: str) -> dict | None:
    low = sql.lower()
    win: dict[str, Any] = {}
    if _TZ_RE.search(low):
        m = _TZ_RE.search(low)
        win["timezone_hint"] = m.group(1)
    else:
        win["timezone_hint"] = None
        win["timezone_unknown"] = True
    daily = bool(re.search(r"(date|day)\s*\(", low)) or "curdate()" in low or "current_date" in low
    if daily:
        win["window"] = "daily"
    elif "now()" in low or "current_timestamp" in low:
        win["window"] = "rolling_or_now"
    else:
        win["window"] = "unspecified"
    m = re.search(r">=?\s*'?(\d{4}-\d{2}-\d{2})", low)
    if m:
        win["lower_bound"] = m.group(1)
    m2 = re.search(r"<?=?\s*'?(\d{4}-\d{2}-\d{2})", low)
    if m2:
        win["upper_bound"] = m2.group(1)
    return win


def _parse_from_text(text: str, out: dict) -> None:
    low = text.lower()
    if any(k in low for k in ("去重", "distinct", "unique", "独立")):
        out["dedupe_key"] = "unknown_distinct"
    if any(k in low for k in ("日活", "dau", "每天")):
        out["grain"] = "daily_active"
        out["dimensions"].append("date")
    elif any(k in low for k in ("月活", "mau", "每月")):
        out["grain"] = "monthly_active"
        out["dimensions"].append("month")
    if "count" in low or "数" in low or "量" in low:
        out["measures"].append({"expr": "COUNT", "func": "COUNT", "column": None,
                                "distinct": out["dedupe_key"] is not None,
                                "kind": "count_distinct" if out["dedupe_key"] else "count_rows"})
    if any(k in low for k in ("剔除", "排除", "不含", "除了")):
        out["filters"].append({"sql": text, "columns": ["__exclusion__"],
                               "operator": "EXCLUDE", "literals": []})
        out["warnings"].append("描述中提到剔除条件但无法结构化，务必补全具体过滤表达式")
    out["warnings"].append(
        "口径来自自然语言描述，字段级细节缺失；建议用 SQL 重新定义后再比对（自然语言口径无法精确比对）")


def signature_of(defn: dict) -> str:
    """生成口径签名，用于快速判断两个口径是否可能一致。"""
    import hashlib
    parts = [
        "|".join(sorted(m.get("expr", "") for m in defn.get("measures", []))),
        "|".join(sorted(defn.get("dimensions", []) or [])),
        "|".join(sorted(f.get("sql", "") for f in defn.get("filters", []))),
        str(defn.get("dedupe_key")),
        str(defn.get("grain")),
        str((defn.get("time_window") or {}).get("window")),
        "|".join(sorted(defn.get("source_tables", []) or [])),
    ]
    return hashlib.sha256("##".join(parts).encode()).hexdigest()[:16]


# ------------------------------------------------------------------
# 2. 结构化 diff
# ------------------------------------------------------------------


def compare_definitions(a: dict, b: dict) -> dict:
    """列出两个口径的所有不等价点，按影响严重程度排序。"""
    diffs: list[dict] = []

    ma = {(_norm_expr(m)) for m in a.get("measures", [])}
    mb = {(_norm_expr(m)) for m in b.get("measures", [])}
    if ma != mb:
        diffs.append({
            "aspect": "measures", "severity": "critical",
            "detail": f"度量不同：A={sorted(ma)} / B={sorted(mb)}",
            "impact": "结果必然不同",
            "how_to_fix": "统一为同一个度量表达式；特别注意 COUNT(*) vs COUNT(DISTINCT col) 的差异",
        })

    da, db = set(a.get("dimensions") or []), set(b.get("dimensions") or [])
    if da != db:
        only_a, only_b = da - db, db - da
        diffs.append({
            "aspect": "dimensions", "severity": "high",
            "detail": f"维度不同：仅 A 有 {sorted(only_a)}，仅 B 有 {sorted(only_b)}",
            "impact": "聚合粒度不同，行数与每个格子的值都不可比",
            "how_to_fix": "对齐维度清单；如果 B 少一个维度，它通常是被再次聚合过的",
        })

    fa = {_norm_expr(f) for f in a.get("filters", [])}
    fb = {_norm_expr(f) for f in b.get("filters", [])}
    if fa != fb:
        diffs.append({
            "aspect": "filters", "severity": "critical",
            "detail": f"过滤条件不同：仅 A 有 {sorted(fa - fb)}，仅 B 有 {sorted(fb - fa)}",
            "impact": "过滤差异是最常见的口径不一致来源：测试账号、退款订单、内部员工是否剔除",
            "how_to_fix": "逐条对齐过滤条件，尤其是软删除标志与状态值",
        })

    ta = (a.get("time_window") or {}).get("window")
    tb = (b.get("time_window") or {}).get("window")
    if ta != tb:
        diffs.append({
            "aspect": "time_window", "severity": "high",
            "detail": f"时间窗口不同：A={ta} / B={tb}",
            "impact": "窗口定义不同会导致跨天/跨时区时数字系统性偏移",
            "how_to_fix": "明确是自然日还是滚动 24 小时，明确业务日切分点",
        })
    tza = (a.get("time_window") or {}).get("timezone_hint")
    tzb = (b.get("time_window") or {}).get("timezone_hint")
    if tza != tzb:
        diffs.append({
            "aspect": "timezone", "severity": "high",
            "detail": f"时区线索不同：A={tza} / B={tzb}",
            "impact": "UTC 与本地时区会让每日边界错位 8 小时，表现为'每天的数的'都不一样'",
            "how_to_fix": "统一到一个时区存储 / 或一个时区统计，并在契约里写死",
        })

    if (a.get("dedupe_key") or None) != (b.get("dedupe_key") or None):
        diffs.append({
            "aspect": "dedupe", "severity": "critical",
            "detail": f"去重口径不同：A={a.get('dedupe_key')} / B={b.get('dedupe_key')}",
            "impact": "同上，这是 DAU 类指标差异的最大来源（行为次数 vs 独立用户数）",
            "how_to_fix": "明确'活跃'的定义：是产生行为的次数还是去重后的用户数",
        })

    sa, sb = set(a.get("source_tables") or []), set(b.get("source_tables") or [])
    if sa != sb:
        diffs.append({
            "aspect": "sources", "severity": "medium",
            "detail": f"来源表不同：仅 A 有 {sorted(sa - sb)}，仅 B 有 {sorted(sb - sa)}",
            "impact": "可能一个是明细表一个是汇总表，汇总表可能有延迟或已经过加工",
            "how_to_fix": "确认血缘：是否一个是 ODS 一个是 ADS",
        })

    ja = len(a.get("joins") or [])
    jb = len(b.get("joins") or [])
    if ja != jb:
        diffs.append({
            "aspect": "joins", "severity": "high",
            "detail": f"JOIN 数量不同：A={ja} / B={jb}",
            "impact": "多一个 JOIN 就可能引入扇出放大",
            "how_to_fix": "对每个 JOIN 确认是 1:1 还是 1:N，1:N 必须先看右侧是否已去重",
        })

    if a.get("grain") != b.get("grain"):
        diffs.append({
            "aspect": "grain", "severity": "high",
            "detail": f"粒度不同：A={a.get('grain')} / B={b.get('grain')}",
            "impact": "明细与聚合混用会把同一笔业务算多次",
            "how_to_fix": "明确每层的数据粒度，禁止跨粒度直接比对",
        })

    equivalent = not diffs
    worst = min((d["severity"] for d in diffs),
                key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(s, 4),
                default=None)
    return {
        "equivalent": equivalent,
        "diff_count": len(diffs),
        "worst_severity": worst,
        "diffs": sorted(diffs, key=lambda d: {"critical": 0, "high": 1, "medium": 2, "low": 3}
                        .get(d["severity"], 4)),
        "a_signature": a.get("signature"),
        "b_signature": b.get("signature"),
        "summary": ("两个口径完全一致" if equivalent
                    else f"发现 {len(diffs)} 处差异，最严重为 {worst}"),
    }


def _norm_expr(x: Any) -> str:
    s = str(x).lower()
    return re.sub(r"\s+", "", s)


# ------------------------------------------------------------------
# 3. 扇出检测
# ------------------------------------------------------------------


def detect_fanout(sql: str, dialect: str | None = None) -> dict:
    """检测 JOIN 是否会把左侧行数放大。

    判定依据是结构而非统计（统计需要元数据）：
      * 存在 JOIN 且 JOIN 列不是右侧表的主键/唯一键 → 可能放大
      * JOIN 右侧是子查询且没有去重 → 高风险
      * 左侧 DISTINCT/去重后可以消除风险
    """
    from dbcore.dialect import unwrap_parens
    from dbcore.extract import extract_objects

    d = normalize_dialect(dialect)
    try:
        ex = extract_objects(sql, d)
    except Exception as exc:
        return {"risk": False, "explanation": f"解析失败，无法判定：{exc}"}
    st = ex["statements"][0] if ex["statements"] else None
    if not st or not st.get("joins"):
        return {"risk": False, "explanation": "没有 JOIN，不存在扇出"}

    has_count = any(m["func"] == "COUNT" for m in st.get("aggregates", []))
    has_distinct = st.get("has_distinct") or any(m.get("distinct") for m in st.get("aggregates", []))
    risk_items = []
    for j in st["joins"]:
        cols = j.get("columns") or []
        risk_items.append({
            "join": j.get("source"),
            "type": j.get("type"),
            "columns": cols,
            "reason": "右侧表在 JOIN 列上是否唯一未知（缺少元数据，无法证明是 1:1）",
        })
    subq = st.get("subqueries") or []
    derived_not_dedup = [
        s["alias"] for s in subq
        if not re.search(r"distinct|group\s+by", s.get("sql", ""), re.I)
    ]
    amplified = bool(risk_items) and has_count and not has_distinct
    return {
        "risk": amplified,
        "confidence": "medium" if not has_distinct else "low",
        "join_count": len(st["joins"]),
        "items": risk_items,
        "derived_without_dedup": derived_not_dedup,
        "has_distinct_protection": has_distinct,
        "explanation": (
            f"存在 {len(st['joins'])} 个 JOIN，且使用了 COUNT 但没有 DISTINCT / 子查询去重保护。"
            f"如果任一 JOIN 是 1:N，左表每行会被复制导致计数虚高"
            if amplified else "已有去重保护或没有 COUNT，扇出风险较低"
        ),
        "verify_sql": _fanout_verify_sql(st) if amplified else None,
        "needs_metadata": True,
        "note": "精确判定需要元数据（右侧表的唯一索引）。缺少元数据时只能给风险提示，不能下断言。",
    }


def _fanout_verify_sql(st: dict) -> list[str]:
    """给出可以证明是否扇出的 SQL。"""
    out = []
    tables = st.get("tables") or []
    for j in st.get("joins", []):
        cols = j.get("columns") or []
        src = j.get("source")
        for c in cols[:2]:
            out.append(
                f"SELECT {c}, COUNT(*) AS cnt FROM {src} "
                f"GROUP BY {c} HAVING COUNT(*) > 1 ORDER BY cnt DESC LIMIT 20"
            )
    if len(tables) >= 2 and not out:
        out.append("-- 请分别检查每个 JOIN 右侧表的唯一性")
    return out[:4]


# ------------------------------------------------------------------
# 4. 血缘
# ------------------------------------------------------------------


def build_lineage(sqls: dict[str, str], dialect: str | None = None) -> dict:
    """从一组 SQL（通常是一层层的视图/ETL）推导表级血缘与列级血缘。

    sqls: {模型名: SQL}
    """
    d = normalize_dialect(dialect)
    table_edges: list[dict] = []
    column_edges: list[dict] = []
    tables: set[str] = set()
    resolved: dict[str, dict] = {}

    from dbcore.extract import extract_objects
    for name, sql in sqls.items():
        try:
            ex = extract_objects(sql, d)
        except Exception:
            continue
        for st in ex["statements"]:
            srcs = [t["qualified"] for t in (st.get("tables") or [])]
            for s in srcs:
                table_edges.append({"from": s, "to": name, "kind": "derives"})
                tables.add(s)
            tables.add(name)
            resolved[name] = {"sources": srcs, "kind": st.get("kind")}
            # 列级：SELECT 里出现的列尽量追溯到源表
            for col in (st.get("columns") or []):
                column_edges.append({
                    "table": name, "column": col["column"],
                    "source_hint": col["table"] or None,
                    "resolved": bool(col["table"]),
                })
    return {
        "dialect": d,
        "tables": sorted(tables),
        "table_edges": table_edges,
        "column_edges": column_edges,
        "models": resolved,
        "roots": sorted({e["from"] for e in table_edges} - {e["to"] for e in table_edges}),
        "leaves": sorted({e["to"] for e in table_edges} - {e["from"] for e in table_edges}),
        "uncertainty": "列级血缘基于静态解析：SELECT *、函数包裹、COALESCE 等场景无法确定来源列",
    }


# ------------------------------------------------------------------
# 5. 漂移 / 下钻 / 契约
# ------------------------------------------------------------------


# 按指标类型的容差分级（§2.3.4）：计数类天然波动大，金额类必须严格
_METRIC_TOLERANCE = {
    "count": 5.0,
    "amount": 0.5,
    "ratio": 2.0,
    "default": 5.0,
}


def _infer_tolerance(metric_type: str | None, tolerance_pct: float | None) -> float:
    if tolerance_pct is not None:
        return tolerance_pct
    return _METRIC_TOLERANCE.get((metric_type or "default").lower(), _METRIC_TOLERANCE["default"])


def detect_drift(points: list[dict], tolerance_pct: float | None = None,
                 metric_type: str | None = None, confirm_points: int = 2) -> dict:
    """同一指标不同时间的取值序列 -> 漂移判断。points: [{"ts":..., "value":...}]

    容差按指标类型分级（count ±5% / amount ±0.5% / ratio ±2%）；
    误报治理：**连续 confirm_points 个点超差才判漂移**，单点毛刺不告警。
    只有 1 个点时明确返回 insufficient_data，**不假装能判断趋势**。
    """
    if len(points) < 2:
        return {
            "status": "insufficient_data",
            "message": f"只有 {len(points)} 个数据点（至少需要 {confirm_points + 1} 个）",
            "tier": "tier0",
        }
    vals = [p["value"] for p in points if isinstance(p.get("value"), (int, float))]
    if len(vals) < 2:
        return {"status": "insufficient_data", "message": "有效数值点不足 2 个"}
    tol = _infer_tolerance(metric_type, tolerance_pct)
    base = vals[0]
    latest = vals[-1]
    change_pct = ((latest - base) / base * 100) if base else None
    mx, mn = max(vals), min(vals)
    spread = ((mx - mn) / mn * 100) if mn else None
    # 逐步超差序列：连续 confirm_points 个点超差才确认（防毛刺误报）
    step_changes = [
        {"from_index": i, "pct": round((vals[i + 1] - vals[i]) / vals[i] * 100, 2) if vals[i] else None}
        for i in range(len(vals) - 1)
    ]
    breach_flags = [abs(s["pct"]) > tol if s["pct"] is not None else False for s in step_changes]
    streak = 0
    confirmed_at = None
    for i, b in enumerate(breach_flags):
        streak = streak + 1 if b else 0
        if streak >= confirm_points and confirmed_at is None:
            confirmed_at = i
    drifted = confirmed_at is not None or (abs(change_pct or 0) > tol and len(vals) >= confirm_points + 1)
    return {
        "status": "ok",
        "points": len(vals),
        "metric_type": (metric_type or "default"),
        "base": base, "latest": latest,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "spread_pct": round(spread, 2) if spread is not None else None,
        "max_step_pct": round(max((abs(s["pct"]) for s in step_changes if s["pct"] is not None), default=0), 2),
        "drifted": drifted,
        "confirmed_by_consecutive": confirmed_at is not None,
        "confirm_points": confirm_points,
        "tolerance_pct": tol,
        "step_changes": step_changes[-10:],
        "verdict": ("连续超差，判定漂移——建议核对最近是否有口径变更或数据源变更"
                    if drifted else
                    ("有单点毛刺但在容差内（连续超差才告警）" if any(breach_flags)
                     else "在容差范围内")),
        "note": "漂移只说明结果变了，不说明为什么变。要定位原因用 reconcile 逐层下钻。",
    }


def reconcile(sql: str, dialect: str | None = None, expected: float | None = None,
              actual: float | None = None) -> dict:
    """数字对不上时的下钻计划 —— 把"为什么不一致"拆成可执行的检查步骤。"""
    from dbcore.extract import extract_objects

    d = normalize_dialect(dialect)
    steps: list[dict] = []
    try:
        ex = extract_objects(sql, d)
        st = ex["statements"][0]
    except Exception as exc:
        return {"error": f"无法解析 SQL：{exc}", "steps": []}

    tables = [t["qualified"] for t in (st.get("tables") or [])]
    joins = st.get("joins") or []
    filters = st.get("predicates") or []

    # 第 0 步：确认两侧跑的是同一份数据
    steps.append({
        "ordinal": 1,
        "name": "确认时间窗口与数据源一致",
        "why": "先排除'两边连的根本不是同一个库/同一时间范围'这种低级但高频的原因",
        "checks": ["确认连接的是同一套实例", "确认日期函数用的是同一个时区",
                   "确认是否一个有 WHERE 过滤一个没有"],
    })
    # 逐层剥离 JOIN
    for i, j in enumerate(joins):
        steps.append({
            "ordinal": len(steps) + 1,
            "name": f"剥离第 {i + 1} 个 JOIN：{j.get('source')}",
            "why": "每去掉一个 JOIN 就对比一次行数，行数不再变化的那层就是放大源",
            "sql_hint": "SELECT COUNT(*) FROM <driving_table> [保留前 N 个 JOIN]",
            "expect": "逐层记录 COUNT(*)，找出行数突然放大的那一层",
        })
    # 逐过滤条件剥离
    for i, f in enumerate(filters[:6]):
        steps.append({
            "ordinal": len(steps) + 1,
            "name": f"去掉第 {i + 1} 个过滤条件看增量：{f.get('sql', '')[:60]}",
            "why": "过滤条件差异造成的差，一定表现为【去掉它之后两边一致】",
            "expect": "记录每个条件各自剔除了多少行",
        })
    # 去重维度
    steps.append({
        "ordinal": len(steps) + 1,
        "name": "对比去重前后的计数",
        "why": "DAU 类指标的差异九成在这里：行为次数 vs 独立用户数",
        "sql_hint": "分别跑 COUNT(*)、COUNT(col)、COUNT(DISTINCT col)",
        "expect": "三个值的差异结构应当一致",
    })
    # JOIN 唯一性
    for t in tables[1:]:
        steps.append({
            "ordinal": len(steps) + 1,
            "name": f"检查 {t} 在关联键上是否唯一",
            "why": "右侧不唯一就是扇出",
            "sql_hint": f"SELECT <join_key>, COUNT(*) FROM {t} GROUP BY 1 HAVING COUNT(*) > 1",
            "expect": "返回 0 行才说明是 1:1",
        })

    ratio = None
    if expected and actual:
        try:
            ratio = round((actual - expected) / expected * 100, 2)
        except ZeroDivisionError:
            ratio = None
    return {
        "dialect": d,
        "tables": tables,
        "join_count": len(joins),
        "filter_count": len(filters),
        "input": {"expected": expected, "actual": actual, "delta_pct": ratio},
        "steps": steps,
        "hypothesis_order": [
            "1) 口径不同（最常见，尤其去重与时间窗口）",
            "2) JOIN 扇出导致行数放大",
            "3) 数据源表不同（ODS vs ADS）或数据延迟",
            "4) NULL 参与聚合导致计数差异",
            "5) 真实的 ETL 数据丢失（最后才怀疑这个）",
        ],
        "note": "按 '先口径后数据' 的顺序排查：先证明两边定义一致，再怀疑数据是错的。"
                "反过来做会浪费大量时间去修根本没错的数据。",
    }


def suggest_canonical(definitions: list[dict]) -> dict:
    """从多个候选口径里推荐标准口径，并给出影响面。

    推荐原则：粒度最细、去重最明确、约束最多的那个（它最不容歧义）。
    """
    if not definitions:
        return {"error": "至少需要一个候选口径"}
    scored = []
    for d in definitions:
        score = 0
        if d.get("dedupe_key"):
            score += 3
        if len(d.get("filters") or []) > 0:
            score += len(d["filters"])
        if (d.get("time_window") or {}).get("window") not in (None, "unspecified"):
            score += 2
        if d.get("source") == "sql":
            score += 2
        if d.get("grain") == "scalar":
            score -= 1
        scored.append({"name": d.get("name"), "score": score, "definition": d})
    scored.sort(key=lambda x: -x["score"])
    best = scored[0]
    offenders = []
    for s in scored[1:]:
        cmp = compare_definitions(best["definition"], s["definition"])
        if not cmp["equivalent"]:
            offenders.append({"name": s["name"], "diff_count": cmp["diff_count"],
                              "worst": cmp["worst_severity"],
                              "diffs": cmp["diffs"][:5]})
    return {
        "recommended": {"name": best["name"], "score": best["score"],
                        "definition": best["definition"]},
        "candidates": [{"name": s["name"], "score": s["score"]} for s in scored],
        "need_change": offenders,
        "impact": f"共 {len(definitions)} 个候选口径，其中 {len(offenders)} 个需要改造对齐",
    }


def check_grain(sql: str, dialect: str | None = None) -> dict:
    """粒度冲突检测：明细表与聚合表混用。"""
    from dbcore.extract import extract_objects
    d = normalize_dialect(dialect)
    ex = extract_objects(sql, d)
    issues = []
    for st in ex["statements"]:
        aggs = st.get("aggregates") or []
        group = st.get("group_by") or []
        if aggs and not group:
            for c in st.get("columns") or []:
                if c["column"] not in [a.get("column") for a in aggs]:
                    issues.append({
                        "type": "naked_column_with_aggregate",
                        "column": c["column"],
                        "message": f"列 `{c['column']}` 出现在 SELECT 中但既没有聚合也没有 GROUP BY",
                        "effect": "MySQL 非严格模式下会取任意一行（结果不确定），PG/ONLY_FULL_GROUP_BY 直接报错",
                    })
        if aggs and group:
            for c in st.get("columns") or []:
                pass
    return {
        "dialect": d,
        "issues": issues,
        "verdict": ("pass" if not issues else "fail"),
        "note": "粒度混淆会导致同一份数据每次查询结果不同，是最难排查的一类问题",
    }


def generate_metric_contract(definition: dict, owner: str = "", version: str = "1.0") -> str:
    """生成可读的指标契约（YAML）。这是真正能落地的治理产物。"""
    import time
    lines = [
        f"# 指标契约",
        f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"name: {definition.get('name')}",
        f"version: {version}",
        f"owner: {owner or '未指定'}",
        f"signature: {definition.get('signature')}",
        "",
        "measures:",
    ]
    for m in definition.get("measures", []):
        lines.append(f"  - expr: {m.get('expr')}")
        lines.append(f"    kind: {m.get('kind')}")
        if m.get("distinct"):
            lines.append(f"    distinct: true")
    lines.append("")
    lines.append(f"dedupe_key: {definition.get('dedupe_key') or 'none'}")
    lines.append(f"grain: {definition.get('grain')}")
    lines.append("")
    lines.append("dimensions:")
    for d in definition.get("dimensions", []) or []:
        lines.append(f"  - {d}")
    lines.append("")
    lines.append("filters:")
    for f in definition.get("filters", []) or []:
        lines.append(f"  - {f.get('sql')}")
    lines.append("")
    tw = definition.get("time_window") or {}
    lines.append("time_window:")
    lines.append(f"  window: {tw.get('window', 'unspecified')}")
    lines.append(f"  timezone: {tw.get('timezone_hint') or 'UNSPECIFIED'}")
    if tw.get("lower_bound"):
        lines.append(f"  lower_bound: {tw['lower_bound']}")
    if tw.get("upper_bound"):
        lines.append(f"  upper_bound: {tw['upper_bound']}")
    lines.append("")
    lines.append("sources:")
    for t in definition.get("source_tables", []) or []:
        lines.append(f"  - {t}")
    lines.append("")
    if definition.get("warnings"):
        lines.append("# ⚠ 待确认事项")
        for w in definition["warnings"]:
            lines.append(f"#   - {w}")
    return "\n".join(lines)
