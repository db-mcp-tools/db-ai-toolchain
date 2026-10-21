"""migration-diff MCP —— 国产化替换的正确性验证。

为什么 Oracle→PG/达梦 不能只靠"翻译一遍能跑"就算完：
  * 语义陷阱：Oracle 空串 = NULL
  * 类型陷阱：NUMBER → numeric 的精度丢失、VARCHAR 的字节 vs 字符语义
  * 排序陷阱：字符集与 collation 不同导致 ORDER BY 结果顺序不同
  * 对象陷阱：序列、同义词、存储过程不会被自动搬迁

本 MCP 不搬运数据（那不该由工具做），只负责：
  1. 扫出所有不被目标库支持的写法
  2. 给出等价改写（改不了的明确指出风险）
  3. 生成客户可在自己环境执行的校验 SQL

工具清单见 DESIGN.md §3.4。

运行： python -m servers.migration_diff
"""

from __future__ import annotations

import time

from servers._shared import register, make_server
from dbcore import migration as MIG
from dbcore import report as RPT
from dbcore.dialect import normalize_dialect
from dbcore.profile_store import record_observation, put_dictionary
from dbcore.protocol import Result, ToolError

MCP_NAME = "migration-diff"
VERSION = "0.1.0"
mcp = make_server(MCP_NAME, VERSION)

_SEV_SCORE = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def detect_dialect_gap(sql: str, target_dialect: str = "postgres",
                       source_dialect: str = "mysql",
                       instance_id: str = "", correlation_id: str = "") -> Result:
    """列出所有目标库不支持的写法，按严重度排序。"""
    gaps = MIG.detect_gaps(sql, target_dialect)
    worst = min((g["severity"] for g in gaps), key=lambda s: _SEV_SCORE.get(s, 9), default=None)
    manual = [g for g in gaps if not g["auto_translatable"]]
    verdict = "fail" if worst in ("critical", "high") else ("warn" if gaps else "pass")
    data = {
        "gaps": sorted(gaps, key=lambda g: _SEV_SCORE.get(g["severity"], 9)),
        "total": len(gaps), "manual_count": len(manual),
        "by_severity": {s: sum(1 for g in gaps if g["severity"] == s)
                        for s in ("critical", "high", "medium", "low")},
        "target_dialect": normalize_dialect(target_dialect),
        "source_dialect": normalize_dialect(source_dialect),
    }
    res = Result(
        ok=True, mcp=MCP_NAME, tool="detect_dialect_gap", data=data,
        verdict=verdict, confidence="medium", tier="tier0",
        summary=f"发现 {len(gaps)} 处不兼容（其中 {len(manual)} 处无法自动改写）",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for g in gaps[:8]:
        res.add_evidence("external", f"migration.{g['rule_id']}", g["description"],
                         f"{g['severity']}｜{'可自动改写' if g['auto_translatable'] else '需人工'}")
    if manual:
        res.warn(f"{len(manual)} 处写法不能自动改写，必须人工处理（尤其是 NULL 语义与存储过程）")
    res.next(MCP_NAME, "translate_sql", sql=sql, source_dialect=source_dialect,
             target_dialect=target_dialect, reason="先尝试自动改写", priority=1, optional=False)
    res.next(MCP_NAME, "estimate_rewrite_effort", target_dialect=target_dialect,
             reason="估算改造工作量", priority=3, optional=True)
    return res


def translate_sql(sql: str, source_dialect: str = "mysql", target_dialect: str = "postgres",
                  instance_id: str = "", correlation_id: str = "") -> Result:
    """确定性改写。**改不动的不硬改，明确说明**"""
    out = MIG.translate_sql(sql, source_dialect, target_dialect)
    unresolved = out.get("unresolved") or []
    res = Result(
        ok=True, mcp=MCP_NAME, tool="translate_sql", data=out,
        verdict="warn" if unresolved else "pass",
        confidence="medium" if out.get("target") else "low", tier="tier0",
        summary=f"{source_dialect}→{target_dialect}："
                + ("已转换（必须验证语义等价）" if out.get("target") else "无法自动转换")
                + f"，残留 {len(unresolved)} 处需人工处理",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("ast", "migration.translate", out.get("method", "none"),
                     "转换方法（决定了可信度）")
    for w in (out.get("warnings") or [])[:3]:
        res.warn(w)
    if out.get("target"):
        res.next(MCP_NAME, "verify_consistency", target_dialect=target_dialect,
                 source_dialect=source_dialect, reason="用数据比对验证语义等价",
                 priority=1, optional=False)
    return res


def check_object_parity(source_dialect: str = "oracle", target_dialect: str = "postgres",
                        instance_id: str = "", correlation_id: str = "") -> Result:
    """生成对象数量与定义比对的 SQL 清单（客户在自己环境执行）。"""
    checks = MIG.object_parity_checks(source_dialect, target_dialect)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="check_object_parity",
        data={"checks": checks, "count": len(checks),
              "source_dialect": source_dialect, "target_dialect": target_dialect},
        verdict="pass", confidence="high", tier="tier0",
        summary=f"{len(checks)} 类对象需要逐一比对（序列起始值是最容易漏的一项）",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for c in checks:
        res.add_evidence("external", f"parity.{c['object']}", c["expect"][:140], "比对要点")
    res.warn("鸭 Nr 注意：序列的起始值必须重设为迁移后的 MAX(id)+1，否则上线后主键冲突")
    return res


def compare_data(tables: list[str], pk: dict[str, str] | None = None,
                 source_dialect: str = "oracle", target_dialect: str = "postgres",
                 sample_ratio: float = 0.05, batch_size: int = 10000,
                 instance_id: str = "", correlation_id: str = "") -> Result:
    """生成两层数据比对计划：L1 全表计数+校验和（快），L2 抽样明细（只在 L1 不通过时做）。"""
    if not tables:
        raise ToolError("E_ARGS", "tables 不能为空")
    plan = MIG.consistency_plan(tables, pk, source_dialect, target_dialect,
                                sample_ratio=sample_ratio, batch_size=batch_size)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="compare_data",
        data={"plan": plan, "total": len(plan), "tables": tables,
              "layer_policy": "L1 全量校验和 → 不一致才进入 L2 抽样下钻"},
        verdict="pass", confidence="high", tier="tier0",
        summary=f"{len(tables)} 张表，生成 {len(plan)} 条比对 SQL"
                + (f"；未指定主键的表按 id 处理" if not pk else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("external", "consistency.L1_L2",
                     f"L1={len([p for p in plan if p['layer'] == 'L1'])} 条，"
                     f"L2={len([p for p in plan if p['layer'] == 'L2'])} 条", "两层校验策略")
    if not pk:
        res.warn("未提供主键字段映射，默认使用 id。请确认各表主键名，否则比对不可靠")
    return res


def verify_consistency(tables: list[str], source_dialect: str = "mysql",
                       target_dialect: str = "postgres", pk: dict[str, str] | None = None,
                       mode: str = "checksum", instance_id: str = "",
                       correlation_id: str = "") -> Result:
    """生成一致性校验 SQL 批次，并明确每条的执行顺序与判据。"""
    plan = MIG.consistency_plan(tables, pk, source_dialect, target_dialect)
    # NULL 语义与排序差异是迁移中最隐蔽的不一致来源
    traps = [
        {"name": "空串 vs NULL", "detail": "Oracle 中 '' = NULL，PG 中不等价",
         "check": "SELECT COUNT(*) FROM t WHERE col = ''  -- 两边各跑一次对比"},
        {"name": "尾部空格", "detail": "CHAR 类型在 PG 会去除尾部空格，MySQL/Oracle 不会",
         "check": "SELECT COUNT(*) FROM t WHERE col <> RTRIM(col)"},
        {"name": "时区和日期精度", "detail": "TIMESTAMP(6) 映射到 PG/MySQL 可能丢精度",
         "check": "SELECT MAX(col) FROM t  -- 对比最大时间戳是否完全一致到微秒"},
        {"name": "字符集与排序", "detail": "collation 不同会导致 ORDER BY 顺序不同",
         "check": "SELECT col FROM t ORDER BY col LIMIT 20  -- 逐行比对顺序"},
    ]
    data = {"plan": plan, "traps": traps, "mode": mode,
            "tables": tables, "total_steps": len(plan) + len(traps)}
    res = Result(
        ok=True, mcp=MCP_NAME, tool="verify_consistency", data=data,
        verdict="pass", confidence="high", tier="tier0",
        summary=f"{len(plan)} 条比对 + {len(traps)} 项语义陷阱检查",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for t in traps:
        res.add_evidence("external", f"semantic_trap.{t['name']}", t["detail"], "必须单独验证")
    res.warn("校验通过 ≠ 迁移成功：还要验证存储过程/触发器在目标库的行为是否一致（那部分无法自动比对）")
    return res


def check_charset_collation(source_charset: str = "AL32UTF8",
                            target_charset: str = "utf8mb4",
                            instance_id: str = "", correlation_id: str = "") -> Result:
    """字符集与排序规则差异检查。"""
    out = MIG.check_charset(source_charset, target_charset)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="check_charset_collation", data=out,
        verdict="warn" if out["needs_attention"] else "pass",
        confidence="medium", tier="tier0",
        summary=f"{source_charset} → {target_charset}："
                + ("存在需要注意的差异" if out["needs_attention"] else "未见已知风险"),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("external", "charset_map", out["note"][:160], "长度单位差异是主要风险")
    return res


def plan_migration(tables: list[str], dependencies: dict[str, list[str]] | None = None,
                   instance_id: str = "", correlation_id: str = "") -> Result:
    """迁移批次编排：按依赖拓扑分批，标注可并行度与回滚批次。"""
    deps = dependencies or {}
    remaining = set(tables)
    batches: list[list[str]] = []
    guard = 0
    while remaining and guard < 50:
        guard += 1
        ready = sorted(t for t in remaining if not (set(deps.get(t, [])) & remaining))
        if not ready:                                   # 存在循环依赖，打破它
            ready = sorted(remaining)[:1]
        batches.append(ready)
        remaining -= set(ready)
    plan = [{"batch": i + 1, "tables": b, "parallel_hint": min(len(b), 8),
             "note": "同一批次内无依赖，可并行；批次之间必须串行"}
            for i, b in enumerate(batches)]
    plan.reverse()                                       # 反向 là rollback 顺序
    rollback_order = [{"batch": p["batch"], "tables": p["tables"]} for p in reversed(plan)]
    res = Result(
        ok=True, mcp=MCP_NAME, tool="plan_migration",
        data={"plan": sorted(plan, key=lambda p: p["batch"]),
              "rollback_order": sorted(rollback_order, key=lambda p: -p["batch"]),
              "batch_count": len(plan), "table_count": len(tables),
              "circular_dependency": guard >= 50},
        verdict="pass", confidence="high", tier="tier0",
        summary=f"{len(tables)} 张表编排为 {len(plan)} 个批次",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("external", "migration.topology", f"{len(plan)} 批", "拓扑排序结果")
    res.warn("回滚顺序必须与迁移顺序严格相反，且每批都要有自己的校验点")
    return res


def check_null_semantics(sql: str, source_dialect: str = "oracle",
                         target_dialect: str = "postgres",
                         instance_id: str = "", correlation_id: str = "") -> Result:
    """NULL / 空串 / 布尔语义差异检查 —— 最容易漏且最难查的一类。"""
    from dbcore.semantics import check_grain
    import re
    issues = []
    if source_dialect == "oracle" and target_dialect in ("postgres", "gaussdb", "kingbase"):
        for m in re.finditer(r"=\s*''|''\s*=|IS\s+NULL", sql, re.I):
            issues.append({
                "type": "empty_string_vs_null", "match": m.group(0),
                "detail": "Oracle 中空字符串等价于 NULL，PG 中 '' 与 NULL 是两回事",
                "action": "逐个改写：把 = '' 改成 IS NULL，或确保写入时不产生空串",
            })
    if re.search(r"\bNVL2\s*\(", sql, re.I) and target_dialect == "postgres":
        issues.append({"type": "nvl2", "match": "NVL2",
                       "detail": "PG 没有 NVL2", "action": "改为 CASE WHEN x IS NOT NULL THEN a ELSE b END"})
    if re.search(r"\bDECODE\s*\(", sql, re.I) and target_dialect in ("postgres", "mysql"):
        issues.append({"type": "decode", "match": "DECODE",
                       "detail": "目标方言不支持 DECODE",
                       "action": "改写为 CASE WHEN ... END，注意 DECODE 的 NULL 比较规则特殊"})
    if re.search(r"\bTINYINT\s*\(\s*1\s*\)", sql, re.I) and target_dialect == "postgres":
        issues.append({"type": "tinyint_bool", "match": "TINYINT(1)",
                       "detail": "MySQL 的 TINYINT(1) 常被当布尔用，PG 需要真正的 boolean",
                       "action": "改为 boolean 并同步调整 ORM 映射"})
    res = Result(
        ok=True, mcp=MCP_NAME, tool="check_null_semantics",
        data={"issues": issues, "count": len(issues),
              "source": source_dialect, "target": target_dialect},
        verdict="fail" if any(i["type"] == "empty_string_vs_null" for i in issues)
                else ("warn" if issues else "pass"),
        confidence="medium", tier="tier0",
        summary=f"发现 {len(issues)} 处语义陷阱"
                or ("发现 %d 处语义陷阱" % len(issues)),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for i in issues[:6]:
        res.add_evidence("external", f"semantic.{i['type']}", i["detail"], i["action"])
    return res


def check_type_mapping(source_type: str = "NUMBER", target_dialect: str = "postgres",
                       instance_id: str = "", correlation_id: str = "") -> Result:
    """类型映射 + 精度溢出预警。"""
    out = MIG.map_type(source_type, target_dialect)
    risky = bool(out.get("risks"))
    res = Result(
        ok=True, mcp=MCP_NAME, tool="check_type_mapping", data=out,
        verdict="warn" if risky else ("pass" if out.get("auto") else "warn"),
        confidence="high" if out.get("target") else "medium", tier="tier0",
        summary=f"{source_type} → {out.get('target') or '无推荐映射'}"
                + (f"｜{out['risks'][0]['description'][:60]}" if risky else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for r in out.get("risks", [])[:5]:
        res.add_evidence("external", f"type_map.{source_type}", r["description"],
                         f"严重度 {r['severity']}")
    if not out.get("auto"):
        res.warn("该类型没有自动映射规则，需要人工确定目标类型")
    return res


def estimate_rewrite_effort(sqls: list[str], target_dialect: str = "postgres",
                            instance_id: str = "", correlation_id: str = "") -> Result:
    """按命中规则估算改造工作量（比拍脑袋有依据）。"""
    if not sqls:
        raise ToolError("E_ARGS", "sqls 不能为空")
    out = MIG.estimate_effort(sqls, target_dialect)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="estimate_rewrite_effort", data=out,
        verdict="warn" if out["manual"] else "pass",
        confidence="medium", tier="tier0",
        summary=f"{out['sql_count']} 条 SQL：{out['gap_count']} 处差异"
                f"（可自动 {out['auto_translatable']} / 需人工 {out['manual']}），"
                f"粗估 {out['estimated_hours']} 小时",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("external", "effort.model", out["assumption"], "估算依据")
    res.warn("工作量估算只能作为谈判起点：存储过程、触发器、定时任务不包含在内")
    return res


def generate_migration_report(tables: list[str] | None = None,
                              sqls: list[str] | None = None,
                              source_dialect: str = "oracle",
                              target_dialect: str = "postgres",
                              instance_id: str = "", correlation_id: str = "") -> Result:
    """迁移就绪度报告 —— 给项目经理看的汇总。"""
    tables = tables or []
    sqls = sqls or []
    gaps = [g for s in sqls for g in MIG.detect_gaps(s, target_dialect)]
    effort = MIG.estimate_effort(sqls, target_dialect) if sqls else {"estimated_hours": 0}
    plan = MIG.consistency_plan(tables, None, source_dialect, target_dialect) if tables else []
    parity = MIG.object_parity_checks(source_dialect, target_dialect)
    blocking = [g for g in gaps if g["severity"] in ("critical", "high") and not g["auto_translatable"]]
    readiness = ("未就绪" if blocking else ("基本就绪" if gaps else "就绪"))
    data = {
        "readiness": readiness, "source_dialect": source_dialect,
        "target_dialect": target_dialect,
        "gap_total": len(gaps), "blocking_count": len(blocking),
        "blocking_items": blocking[:10],
        "tables": tables, "table_count": len(tables),
        "consistency_steps": len(plan), "object_parity_checks": len(parity),
        "estimated_hours": effort.get("estimated_hours", 0),
        "checklist": [
            "对象数量与定义比对（尤其序列起始值）",
            "字符集与排序规则确认",
            "NULL / 空串语义逐个改写",
            "存储过程与触发器人工验证",
            "数据两层一致性校验（L1 校验和 + L2 抽样）",
            "灰度切流预案与回滚批次编排",
        ],
    }
    res = Result(
        ok=True, mcp=MCP_NAME, tool="generate_migration_report", data=data,
        verdict="fail" if blocking else ("warn" if gaps else "pass"),
        confidence="medium", tier="tier0",
        summary=f"迁移就绪度：{readiness}（{len(blocking)} 项阻塞，粗估 {effort.get('estimated_hours', 0)} 小时）",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for b in blocking[:5]:
        res.add_evidence("external", f"blocking.{b['rule_id']}", b["description"],
                         "这是不能上线的原因")
    return res


def record_migration_result(instance_id: str, target_dialect: str, items: dict,
                            correlation_id: str = "") -> Result:
    """把本次迁移的发现写入共享档案 —— 跨项目积累可复用的迁移知识。"""
    key = f"migration:{target_dialect}"
    put_dictionary("migration", key,
                   {"items": items, "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "instance_id": instance_id})
    record_observation(instance_id or "migration", "migration",
                       {"target": target_dialect, "summary": items.get("readiness", "")})
    return Result(
        ok=True, mcp=MCP_NAME, tool="record_migration_result",
        data={"key": key, "bytes": len(str(items))},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"已记录到迁移知识库（{key}），下次同类项目可复用",
        instance_id=instance_id, correlation_id=correlation_id,
    )


TOOLS = {
    "detect_dialect_gap": detect_dialect_gap,
    "translate_sql": translate_sql,
    "check_object_parity": check_object_parity,
    "compare_data": compare_data,
    "verify_consistency": verify_consistency,
    "check_charset_collation": check_charset_collation,
    "plan_migration": plan_migration,
    "check_null_semantics": check_null_semantics,
    "check_type_mapping": check_type_mapping,
    "estimate_rewrite_effort": estimate_rewrite_effort,
    "generate_migration_report": generate_migration_report,
    "record_migration_result": record_migration_result,
}

for _n, _f in TOOLS.items():
    register(mcp, MCP_NAME, _f, _n)


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
