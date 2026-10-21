"""执行计划归一化。

不同引擎的执行计划长得完全不一样：
  * MySQL 8 有 FORMAT=JSON（推荐）、也有竖线表格
  * PG 有 FORMAT JSON 或纯文本 indented tree
  * Oracle 是 DBMS_XPLAN 的表格文本
  * ClickHouse 是 JSON AST

compare_plans 必须先把它们压成同一个结构，否则跨引擎无法比较，
也无法解释为什么"改完更快"。

统一结构 PlanNode（纯 dict，可序列化、可进档案）：
    id / operation / table / access_type / index / rows_examined / rows_produced /
    filtered / cost / extra(list) / children(list)
"""

from __future__ import annotations

import json
import re
from typing import Any

# 公认的"坏味道"标记 -> 中文说明
_FLAGS = {
    "full_scan": "全表扫描",
    "full_index_scan": "全索引扫描（未过滤）",
    "filesort": "额外排序（未利用索引有序性）",
    "temp_table": "使用临时表",
    "no_index": "未使用任何索引",
    "covering": "覆盖索引（好）",
    "range_scan": "范围扫描",
    "index_lookup": "索引查找（好）",
    "join_buffer": "使用 join buffer（关联未走索引）",
    "impossible_where": "WHERE 恒假",
    "bad_filtered": "存储引擎返回大量行后由 server 层过滤",
    "loose_scan": "松散索引扫描",
    "mrr": "使用 MRR 优化",
    "batched_key_access": "使用 BKA 优化",
}


def _i(v: Any, default: int = 0) -> int:
    try:
        f = float(v)
        return int(f)
    except (TypeError, ValueError):
        return default


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# MySQL
# --------------------------------------------------------------------------


def _mysql_node(node: dict, parent_id: str = "", idx: list[int] = [0]) -> dict:
    idx[0] += 1
    nid = parent_id + (f".{idx[0]}" if parent_id else str(idx[0]))
    extra_raw = node.get("attached_condition") or node.get("Extra") or []
    if isinstance(extra_raw, str):
        extra_raw = [extra_raw]
    out = {
        "id": nid,
        "operation": node.get("operation", "unknown"),
        "table": node.get("table_name") or node.get("table"),
        "access_type": node.get("access_type"),
        "index": node.get("key") or node.get("key_parts") and None,
        "possible_keys": node.get("possible_keys") or [],
        "rows_examined": _i(node.get("rows_examined_per_scan", node.get("rows"))),
        "rows_produced": _i(node.get("rows_produced_per_join", node.get("rows"))),
        "filtered": _f(node.get("filtered", 100.0)),
        "cost": _f(node.get("cost_info", {}).get("query_cost"))
            if isinstance(node.get("cost_info"), dict) else 0.0,
        "extra": list(extra_raw),
        "children": [],
    }
    kp = node.get("key_parts")
    if not out["index"] and isinstance(kp, list):
        out["index"] = ",".join(kp)
    for key in ("query_block", "ordering_operation", "grouping_operation",
                "nested_loop", "duplicates_removal", "materialized_from_subquery",
                "union_result", "window_functions"):
        sub = node.get(key)
        if isinstance(sub, dict):
            subs = sub.get("select_list_subqueries") or []
            _collect_tables(sub, out["children"], nid, idx)
            for s in subs:
                if isinstance(s, dict):
                    _collect_tables(s.get("query_block", s), out["children"], nid, idx)
            if key in ("query_block", "ordering_operation", "grouping_operation"):
                break
    return out


def _collect_tables(blk: dict, children: list, parent_id: str, idx: list[int]) -> None:
    """从 MySQL JSON plan 的一个 block 里收集 table / union subqueries。"""
    if not isinstance(blk, dict):
        return
    t = blk.get("table")
    if isinstance(t, dict) and t.get("table_name"):
        children.append(_mysql_node(t, parent_id, idx))
    nested = blk.get("nested_loop")
    if isinstance(nested, list):
        for item in nested:
            if isinstance(item, dict) and item.get("table_name"):
                children.append(_mysql_node(item, parent_id, idx))
            else:
                _collect_tables(item, children, parent_id, idx)
    for key in ("union_result", "materialized_from_subquery", "ordering_operation",
                "grouping_operation", "duplicates_removal"):
        sub = blk.get(key)
        if isinstance(sub, dict):
            if isinstance(sub.get("table_name"), str) or sub.get("table_name"):
                pass
            _collect_tables(sub.get("query_block", sub), children, parent_id, idx)


def parse_mysql(plan: Any) -> dict:
    """接受 dict / json str / list（表格式 EXPLAIN 的 rows 列表）。"""
    if isinstance(plan, (str, bytes)):
        try:
            plan = json.loads(plan)
        except Exception:
            return {"engine": "mysql", "nodes": [], "flags": {}, "totals": {},
                    "parse_error": "无法按 JSON 解析 MySQL 执行计划"}
    if isinstance(plan, dict):
        root = plan.get("query_block", plan)
        top = {
            "id": "1",
            "operation": root.get("select_id") and "SELECT" or "SELECT",
            "table": root.get("table_name") or root.get("table"),
            "access_type": root.get("access_type"),
            "index": root.get("key"),
            "possible_keys": root.get("possible_keys") or [],
            "rows_examined": _i(root.get("rows_examined_per_scan", root.get("rows"))),
            "rows_produced": _i(root.get("rows_produced_per_join", root.get("rows"))),
            "filtered": _f(root.get("filtered", 100.0)),
            "cost": _f((root.get("cost_info") or {}).get("query_cost")),
            "extra": [root.get("message")] if root.get("message") else [],
            "children": [],
        }
        _collect_tables(root, top["children"], "1", [1])
        if root.get("table_name") and not root.get("children"):
            pass
        return {"engine": "mysql", "root": top, "nodes": _flatten([top]),
                "flags": {}, "totals": {}}
    if isinstance(plan, list):                                # EXPLAIN 表格式结果
        nodes = []
        for i, r in enumerate(plan):
            nodes.append({
                "id": str(r.get("id", i + 1)),
                "operation": r.get("select_type") or "SELECT",
                "table": r.get("table"),
                "access_type": r.get("type"),
                "index": r.get("key"),
                "possible_keys": (r.get("possible_keys") or "").split(",") if r.get("possible_keys") else [],
                "rows_examined": _i(r.get("rows")),
                "rows_produced": _i(r.get("rows")),
                "filtered": _f(r.get("filtered", 100.0)),
                "cost": 0.0,
                "extra": [e.strip() for e in (r.get("Extra") or "").split(";") if e.strip()],
                "children": [],
            })
        return {"engine": "mysql", "root": None, "nodes": nodes, "flags": {}, "totals": {}}
    return {"engine": "mysql", "nodes": [], "flags": {}, "totals": {}, "parse_error": "未知格式"}


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------

_PG_RE = re.compile(
    r"^\s*(?P<pfx>[- >|]*?)(?P<op>[A-Za-z][A-Za-z0-9 ]*?)\s+on\s+(?P<tbl>[\w\.\"]+)"
    r"(?:\s+(?P<using>[\w]+))?\s*(?:\(cost=.*?\))?",
)


def parse_postgres(plan: Any) -> dict:
    """接受 plan 字典序列，或 EXPLAIN 的纯文本输出。"""
    nodes: list[dict] = []

    def walk(obj: Any, depth: int, parent: str, idx: list[int]) -> None:
        if isinstance(obj, dict):
            idx[0] += 1
            nid = f"{parent}.{idx[0]}" if parent else str(idx[0])
            ct = obj.get("Actual Rows", obj.get("Plan Rows"))
            rows = _i(ct)
            nodes.append({
                "id": nid,
                "operation": obj.get("Node Type", "unknown"),
                "table": (obj.get("Relation Name") or obj.get("Subplan Name") or ""),
                "access_type": "Index" if "Index" in str(obj.get("Node Type", "")) else "Scan",
                "index": obj.get("Index Name"),
                "possible_keys": [],
                "rows_examined": _i(obj.get("Actual Rows", obj.get("Plan Rows"))),
                "rows_produced": _i(obj.get("Actual Rows", obj.get("Plan Rows"))),
                "filtered": 100.0,
                "cost": _f(obj.get("Total Cost")),
                "extra": _pg_extra(obj),
                "children": [],
            })
            for child in obj.get("Plans", []) or []:
                walk(child, depth + 1, nid, idx)
        elif isinstance(obj, list):
            for c in obj:
                walk(c, depth, parent, idx)

    if isinstance(plan, (str, bytes)):
        text = plan.decode() if isinstance(plan, bytes) else plan
        lines = [l for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            m = _PG_RE.match(line)
            if not m:
                continue
            depth = len(line) - len(line.lstrip(" ->"))
            op = m.group("op").strip()
            t = m.group("tbl").strip('"')
            extra = []
            for kw in ("Sort Method", "Filter", "Join Filter", "Hash Cond", "Rows Removed",
                       "Heap Fetches", "Buckets", "Batches", "Memory Usage"):
                if kw.lower() in line.lower():
                    extra.append(line[line.lower().find(kw.lower()):].strip(" )")[:120])
            nodes.append({
                "id": str(i + 1),
                "operation": op,
                "table": t,
                "access_type": "Index" if "Index" in op else ("Seq" if "Seq" in op else op),
                "index": m.group("using") if "Index" in op else None,
                "possible_keys": [],
                "rows_examined": _pg_rows(line),
                "rows_produced": _pg_rows(line),
                "filtered": 100.0,
                "cost": _pg_cost(line),
                "extra": extra,
                "children": [],
            })
        return {"engine": "postgres", "root": None, "nodes": nodes, "flags": {}, "totals": {}}

    if isinstance(plan, list):
        for p in plan:
            walk(p.get("Plan", p), 0, "", [0])
    elif isinstance(plan, dict):
        walk(plan.get("Plan", plan), 0, "", [0])
    return {"engine": "postgres", "root": None, "nodes": nodes, "flags": {}, "totals": {}}


def _pg_extra(obj: dict) -> list[str]:
    out = []
    for k in ("Filter", "Join Filter", "Hash Cond", "Sort Key", "Group Key",
              "Sort Method", "Rows Removed by Filter", "Heap Fetches"):
        if obj.get(k) is not None:
            out.append(f"{k}: {obj[k]}")
    if obj.get("Actual Rows") and obj.get("Plan Rows"):
        try:
            ratio = float(obj["Actual Rows"]) / max(float(obj["Plan Rows"]), 1.0)
            if ratio > 10 or ratio < 0.1:
                out.append(f"估算偏差: 预估 {obj['Plan Rows']} vs 实际 {obj['Actual Rows']}")
        except (TypeError, ValueError):
            pass
    return out


def _pg_rows(line: str) -> int:
    m = re.search(r"rows=(\d+)", line)
    if m:
        return int(m.group(1))
    m = re.search(r"\(cost=[\d.]+..[\d.]+\s+rows=(\d+)", line)
    return int(m.group(1)) if m else 0


def _pg_cost(line: str) -> float:
    m = re.search(r"cost=[\d.]+..([\d.]+)", line)
    return float(m.group(1)) if m else 0.0


# --------------------------------------------------------------------------
# Oracle
# --------------------------------------------------------------------------


def parse_oracle(text: str) -> dict:
    nodes = []
    lines = [l for l in (text or "").splitlines() if l.strip()]
    started = False
    for i, line in enumerate(lines):
        if "-" in set(line.replace(" ", "")[:6]) and len(line) > 20 and not started:
            started = True
            continue
        if not started:
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        parts = [p.strip() for p in parts]
        op = parts[1]
        if not op:
            continue
        nodes.append({
            "id": parts[0],
            "operation": op,
            "table": parts[3] if len(parts) > 3 and parts[3] else None,
            "access_type": None,
            "index": parts[4] if len(parts) > 4 and parts[4] else None,
            "possible_keys": [],
            "rows_examined": _i(parts[5].replace(",", "")) if len(parts) > 5 else 0,
            "rows_produced": _i(parts[5].replace(",", "")) if len(parts) > 5 else 0,
            "filtered": 100.0,
            "cost": _f(parts[-1].replace(",", "")) if parts[-1] else 0.0,
            "extra": [],
            "children": [],
        })
    return {"engine": "oracle", "root": None, "nodes": nodes, "flags": {}, "totals": {}}


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

_PARSERS = {
    "mysql": parse_mysql,
    "mariadb": parse_mysql,
    "postgres": parse_postgres,
    "gaussdb": parse_postgres,
    "kingbase": parse_postgres,
    "oracle": parse_oracle,
    "dameng": parse_oracle,
}


def _flatten(nodes: list[dict]) -> list[dict]:
    out = []
    for n in nodes:
        out.append(n)
        out.extend(_flatten(n.get("children") or []))
    return out


def normalize_plan(plan: Any, dialect: str = "mysql") -> dict:
    """把任意来源的执行计划压成统一结构，并计算 flags / totals。"""
    parser = _PARSERS.get(dialect, parse_mysql)
    try:
        parsed = parser(plan)
    except Exception as exc:                                   # 计划解析失败不能拖垮整个调用
        return {"engine": dialect, "nodes": [], "flags": {}, "totals": {},
                "parse_error": f"{type(exc).__name__}: {exc}"}

    nodes = _flatten([parsed.get("root")] if parsed.get("root") else parsed.get("nodes", []))
    if parsed.get("root") is None and parsed.get("nodes"):
        nodes = parsed["nodes"]

    flags: dict[str, bool | dict] = {}
    access = [str(n.get("access_type") or "").upper() for n in nodes]
    all_extra = " | ".join(e for n in nodes for e in (n.get("extra") or []))
    all_ops = " | ".join(str(n.get("operation") or "") for n in nodes)

    flags["full_scan"] = any(a == "ALL" for a in access) or "Seq Scan" in all_ops
    flags["full_index_scan"] = any(a == "INDEX" for a in access)
    flags["no_index"] = not any(n.get("index") for n in nodes)
    flags["filesort"] = ("filesort" in all_extra.lower()) or ("Sort Method" in all_extra)
    flags["temp_table"] = ("temporary" in all_extra.lower()) or ("temporary" in all_ops.lower())
    flags["covering"] = "using index" in all_extra.lower() and "using index condition" not in all_extra.lower()
    flags["index_lookup"] = any(a in ("REF", "EQ_REF", "CONST", "RANGE", "REF_OR_NULL") for a in access)
    flags["range_scan"] = "RANGE" in access
    flags["join_buffer"] = ("join buffer" in all_extra.lower()) or ("Nested Loop" in all_ops)
    flags["impossible_where"] = "impossible where" in all_extra.lower()
    flags["bad_filtered"] = any(n.get("filtered", 100.0) < 20.0 and n.get("rows_examined", 0) > 10000
                                for n in nodes)

    totals = {
        "rows_examined": sum(n.get("rows_examined", 0) for n in nodes),
        "rows_produced": max((n.get("rows_produced", 0) for n in nodes), default=0),
        "max_cost": max((n.get("cost", 0.0) for n in nodes), default=0.0),
        "node_count": len(nodes),
        "table_count": len({n.get("table") for n in nodes if n.get("table")}),
    }
    parsed["nodes"] = nodes
    parsed["flags"] = flags
    parsed["totals"] = totals
    parsed["join_order"] = [n.get("table") for n in nodes if n.get("table")]
    parsed["human_flags"] = [f"{v}" for k, v in flags.items() if v is True and k in _FLAGS]
    return parsed


def diff_plans(before: dict, after: dict) -> dict:
    """比较两个归一化的计划，给出**可量化**的收益判定。"""
    b, a = before.get("totals", {}), after.get("totals", {})
    rb, ra = b.get("rows_examined", 0) or 0, a.get("rows_examined", 0) or 0
    cb, ca = b.get("max_cost", 0.0) or 0.0, a.get("max_cost", 0.0) or 0.0

    def pct(x: float, y: float) -> float | None:
        if x == 0:
            return None
        return round((y - x) / x * 100, 2)

    gains = {
        "rows_examined_before": rb,
        "rows_examined_after": ra,
        "rows_change_pct": pct(rb, ra),
        "cost_before": round(cb, 3),
        "cost_after": round(ca, 3),
        "cost_change_pct": pct(cb, ca) if cb else None,
        "full_scan_removed": before.get("flags", {}).get("full_scan") and not after.get("flags", {}).get("full_scan"),
        "filesort_removed": before.get("flags", {}).get("filesort") and not after.get("flags", {}).get("filesort"),
        "temp_table_removed": before.get("flags", {}).get("temp_table") and not after.get("flags", {}).get("temp_table"),
        "index_gained": (not before.get("flags", {}).get("index_lookup")
                         and after.get("flags", {}).get("index_lookup")),
        "covering_gained": (not before.get("flags", {}).get("covering")
                            and after.get("flags", {}).get("covering")),
    }
    regressions = []
    if isinstance(gains["rows_change_pct"], float) and gains["rows_change_pct"] > 5:
        regressions.append(f"扫描行数上升 {gains['rows_change_pct']}%，改写不可采用")
    if isinstance(gains["cost_change_pct"], float) and gains["cost_change_pct"] > 5:
        regressions.append(f"代价估算上升 {gains['cost_change_pct']}%，改写不可采用")
    if after.get("flags", {}).get("full_scan") and not before.get("flags", {}).get("full_scan"):
        regressions.append("改写后出现了新的全表扫描")

    improved = (gains["full_scan_removed"] or gains["filesort_removed"]
                or gains["index_gained"] or gains["covering_gained"]
                or (isinstance(gains["rows_change_pct"], float) and gains["rows_change_pct"] < -20))
    if regressions:
        verdict = "regressed"
    elif improved:
        verdict = "improved"
    else:
        verdict = "neutral"
    gains["verdict"] = verdict
    gains["regressions"] = regressions
    gains["summary"] = (
        f"扫描行数 {rb:,} → {ra:,}"
        + (f"（{gains['rows_change_pct']:+}%）" if gains["rows_change_pct"] is not None else "")
        + (f"，代价 {cb:,.1f} → {ca:,.1f}" if cb or ca else "")
    )
    return gains
