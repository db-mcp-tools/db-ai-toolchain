"""索引顾问：给出能直接执行的索引方案，并先检查是不是已经被人建过了。

拆分原则：纯逻辑（决策部分）与取数（连库部分）分离，前者可单测。
"""

from __future__ import annotations

import re
from typing import Any

from dbcore.connectors import ConnectionSpec, MissingDriver, run_readonly
from dbcore.dialect import normalize_dialect


def rank_candidates(candidates: list[str], stats: dict[str, Any], table_rows: int | None = None,
                    write_freq: float | None = None) -> list[dict[str, Any]]:
    """候选索引收益-代价排序（§2.5）。纯逻辑，可单测。

    收益：等值查询预估扫描行数 = rows × (1 − selectivity)，收益 = 全表扫描省下的行数。
    代价：写放大 ≈ 写频率 × 索引列数（Tier0 无写频率数据时标注未知）。
    Tier0（无行数/选择性数据）→ 输出候选但标注「收益未验证，建议 EXPLAIN 确认」。
    """
    out: list[dict[str, Any]] = []
    for c in candidates:
        sel = stats.get(c)
        item: dict[str, Any] = {"column": c, "selectivity": sel}
        if sel is not None and table_rows:
            est = max(1, int(table_rows * (1.0 - min(sel, 1.0))))
            item["est_rows_examined"] = est
            item["benefit_rows_saved"] = max(0, table_rows - est)
            item["verified"] = True
        else:
            item["est_rows_examined"] = None
            item["benefit_rows_saved"] = None
            item["verified"] = False
            item["note"] = "收益未验证（缺少行数/选择性数据），建议 EXPLAIN 确认"
        if write_freq is not None:
            item["write_amplification"] = round(write_freq * 1.0, 2)   # 单列候选
            item["cost_note"] = f"写放大 ≈ 写频率 {write_freq}/s × 1 列"
        else:
            item["write_amplification"] = None
            item["cost_note"] = "写放大未知（Tier2 接入写频率数据后可量化）"
        out.append(item)
    # 已验证的按收益降序排前；未验证的保持相对顺序排后
    verified = [x for x in out if x["verified"]]
    unverified = [x for x in out if not x["verified"]]
    verified.sort(key=lambda x: -(x["benefit_rows_saved"] or 0))
    return verified + unverified


def plan_index(existing: list[dict[str, Any]], candidates: list[str], table: str,
               order_column: str | None = None, schema_map: dict | None = None,
               dialect: str = "mysql",
               selectivity: dict[str, Any] | None = None,
               table_rows: int | None = None,
               write_freq: float | None = None) -> dict:
    """纯逻辑：给候选字段排序并检查冗余。不连库，可单测。

    existing: [{"name":"idx_a","columns":["a","b"],"unique":False}]
    candidates: 等值/过滤字段
    """
    existing_prop = [c.lower() for c in candidates if c]
    if order_column and order_column.lower() not in existing_prop:
        ordered = existing_prop + [order_column.lower()]
    else:
        ordered = existing_prop

    redundant = None
    covered_prefix = None
    for e in existing:
        cols = [c.lower() for c in e["columns"]]
        if cols == ordered:
            redundant = e["name"]
        elif cols[: len(ordered)] == ordered:
            covered_prefix = e["name"]
        elif ordered[: len(cols)] == cols:
            covered_prefix = e["name"]

    name = "idx_" + "_".join(ordered)[:40]
    if dialect == "mysql":
        ddl = f"ALTER TABLE {table} ADD INDEX {name} ({', '.join(ordered)});"
    else:
        ddl = f"CREATE INDEX CONCURRENTLY {name} ON {table} ({', '.join(ordered)});"

    return {
        "proposed_columns": ordered,
        "index_name": name,
        "ddl": ddl,
        "redundant_with": redundant,
        "covered_by_prefix": covered_prefix,
        "should_create": redundant is None and covered_prefix is None,
        "verdict": (
            f"与已有索引 {redundant} 完全重复，无需新建" if redundant
            else (f"已有索引 {covered_prefix} 的前缀已覆盖，通常无需新建（确认选择性后再决定）" if covered_prefix
                  else "可以新建")
        ),
        "ranking": rank_candidates(ordered, selectivity or {}, table_rows, write_freq),
        "order_rule": "等值字段放前面、按选择性从高到低；范围/排序字段放最后",
    }


def advise_indexes(url: str, table: str, candidates: list[str], dialect: str | None = None,
                   order_column: str | None = None) -> dict:
    """连库版：拉现有索引 + 选择性数据，再调用 plan_index 给结论。"""
    spec = ConnectionSpec(url)
    d = dialect or spec.driver
    schema = spec.database

    existing: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    notes: list[str] = []
    total_rows: int | None = None

    if d == "mysql":
        rows = run_readonly(
            url,
            f"SELECT index_name, seq_in_index, column_name, non_unique FROM information_schema.statistics "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' ORDER BY index_name, seq_in_index",
            d,
        )["rows"]
        bucket: dict[str, list[str]] = {}
        uniq: dict[str, bool] = {}
        for name, seq, col, non_unique in rows:
            bucket.setdefault(name, []).append(col)
            uniq[name] = not non_unique
        existing = [{"name": k, "columns": v, "unique": uniq.get(k, False)} for k, v in bucket.items()]

        try:
            card_rows = run_readonly(
                url,
                f"SELECT column_name, cardinality FROM information_schema.statistics "
                f"WHERE table_schema = '{schema}' AND table_name = '{table}' AND seq_in_index = 1",
                d,
            )["rows"]
            total = run_readonly(
                url,
                f"SELECT table_rows FROM information_schema.tables WHERE table_schema = '{schema}' "
                f"AND table_name = '{table}'",
                d,
            )["rows"]
            total_rows = int(total[0][0]) if total and total[0][0] else 0
            for col, card in card_rows:
                try:
                    stats[col] = round(float(card or 0) / total_rows, 4) if total_rows else None
                except (TypeError, ValueError):
                    stats[col] = None
            notes.append(f"表估算行数 {total_rows:,}（information_schema 估值，非精确值）")
        except Exception as exc:
            notes.append(f"选择性数据获取失败：{type(exc).__name__}")
    else:
        rows = run_readonly(
            url, f"SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = '{table}'", d
        )["rows"]
        for name, definition in rows:
            m = re.search(r"\(([^)]*)\)", definition or "")
            cols = [c.strip() for c in (m.group(1).split(",") if m else [])]
            existing.append({"name": name, "columns": cols, "unique": "UNIQUE" in (definition or "").upper()})
        try:
            stats_rows = run_readonly(
                url,
                f"SELECT attname, n_distinct FROM pg_stats WHERE schemaname = 'public' AND tablename = '{table}'",
                d,
            )["rows"]
            for col, n_distinct in stats_rows:
                try:
                    stats[col] = None if n_distinct is None else (1 / abs(float(n_distinct)) if float(n_distinct) else 1)
                except (TypeError, ValueError):
                    stats[col] = None
        except Exception as exc:
            notes.append(f"统计信息获取失败：{type(exc).__name__}")

    ranked = sorted(candidates, key=lambda c: -(stats.get(c) or 0))
    proposal = plan_index(existing, ranked, table, order_column=order_column, dialect=d,
                          selectivity=stats, table_rows=total_rows)

    return {
        "instance_id": spec.instance_id,
        "table": table,
        "existing_indexes": existing,
        "selectivity": {c: stats.get(c) for c in candidates},
        "proposal": proposal,
        "notes": notes,
        "next_step": "新建前调用 precheck_change_window 检查长事务与复制延迟",
    }
