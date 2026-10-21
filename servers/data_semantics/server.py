"""data-semantics MCP —— 数据口径治理。

解决的那个经典问题：**"两个部门算出来的 DAU 不一样"**。

绝大多数不是数据错了，是口径没对齐：
  * 时间窗口不同（自然日 vs 近 24 小时 vs 业务日）
  * 去重维度不同（COUNT(user_id) vs COUNT(DISTINCT user_id)）
  * 过滤条件不同（有没有剔除测试账号、退款订单）
  * JOIN 扇出（一对多关联后 COUNT 被放大 —— 最常见）
  * 粒度不一致（明细表 vs 聚合表混算）
  * 时区（UTC vs Asia/Shanghai，边界整体偏移）

做事顺序：结构化 → 结构化 diff → 自动下钻 → 固化为契约。
工具清单见 DESIGN.md §3.5。

运行： python -m servers.data_semantics
"""

from __future__ import annotations

from servers._shared import register, make_server
from dbcore import semantics as SEM
from dbcore.dialect import normalize_dialect
from dbcore.profile_store import put_dictionary, get_dictionary, list_dictionary
from dbcore.protocol import Result, ToolError

MCP_NAME = "data-semantics"
VERSION = "0.1.0"
mcp = make_server(MCP_NAME, VERSION)


def parse_metric_definition(sql: str | None = None, text: str | None = None,
                            name: str = "", dialect: str = "mysql",
                            instance_id: str = "", correlation_id: str = "") -> Result:
    """把口径抽成结构化对象。**没法比对的口径等于没有治理**"""
    if not sql and not text:
        raise ToolError("E_ARGS", "sql 与 text 至少提供一个（推荐给 SQL，字段级细节才够）")
    d = normalize_dialect(dialect)
    defn = SEM.parse_metric_definition(sql=sql, text=text, name=name, dialect=d)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="parse_metric_definition", data=defn,
        verdict="warn" if defn.get("warnings") else "pass",
        confidence="high" if sql else "low", tier="tier0",
        summary=f"口径 `{defn['name']}`：{len(defn['measures'])} 个度量，"
                f"{len(defn['dimensions'])} 个维度，"
                f"去重键 {defn.get('dedupe_key') or '无'}"
                + (f"｜{len(defn['warnings'])} 项待确认" if defn.get("warnings") else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for w in (defn.get("warnings") or [])[:5]:
        res.warn(w)
    res.add_evidence("ast", "semantics.parse", defn["signature"], "口径签名（同签名才可能等价）")
    return res


def _defn(value: dict | str | None, dialect: str, name: str = "") -> dict:
    """允许入参是已解析好的定义对象，或一段 SQL。"""
    if isinstance(value, dict) and "signature" in value:
        return value
    if isinstance(value, str):
        return SEM.parse_metric_definition(sql=value, name=name, dialect=dialect)
    raise ToolError("E_ARGS", "需要传入已解析的 definition 对象或一段 SQL")


def compare_definitions(a: dict | str, b: dict | str, dialect: str = "mysql",
                        name_a: str = "A", name_b: str = "B",
                        instance_id: str = "", correlation_id: str = "") -> Result:
    """★ 结构化 diff —— 不是让人肉眼对着两段 SQL 看，而是逐项列出不等价点。"""
    da = _defn(a, dialect, name_a)
    db = _defn(b, dialect, name_b)
    out = SEM.compare_definitions(da, db)
    out["definition_a"] = {k: da.get(k) for k in ("name", "signature", "measures", "dimensions",
                                                  "filters", "dedupe_key", "grain")}
    out["definition_b"] = {k: db.get(k) for k in ("name", "signature", "measures", "dimensions",
                                                  "filters", "dedupe_key", "grain")}
    worst = out["worst_severity"]
    res = Result(
        ok=True, mcp=MCP_NAME, tool="compare_definitions", data=out,
        verdict="pass" if out["equivalent"] else ("fail" if worst in ("critical", "high") else "warn"),
        confidence="high", tier="tier0",
        summary=out["summary"],
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for dd in out["diffs"][:8]:
        res.add_evidence("ast", f"semantics.diff.{dd['aspect']}", dd["detail"][:160],
                         f"{dd['severity']}｜{dd['impact'][:60]}")
    if not out["equivalent"]:
        res.next(MCP_NAME, "reconcile", dialect=dialect,
                 reason="口径不一致时用下钻定位到具体差异层", priority=1, optional=False)
    return res


def detect_fanout(sql: str, dialect: str = "mysql", instance_id: str = "",
                  correlation_id: str = "") -> Result:
    """JOIN 扇出检测 —— 数字虚高的头号原因。"""
    d = normalize_dialect(dialect)
    out = SEM.detect_fanout(sql, d)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="detect_fanout", data=out,
        verdict="fail" if out.get("risk") else "pass",
        confidence=out.get("confidence", "medium"), tier="tier0",
        summary=out.get("explanation", ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("ast", "semantics.fanout", f"{out.get('join_count', 0)} 个 JOIN",
                     out.get("explanation", "")[:120])
    if out.get("verify_sql"):
        for s in out["verify_sql"][:3]:
            res.add_evidence("sql", "semantics.fanout_verify", s[:160], "可证伪的验证 SQL")
        res.warn("缺少元数据，无法证明 JOIN 是 1:1。执行上面的验证 SQL 才能确认")
        res.tier_upgrade = {"to": "tier1", "need": "表的索引/唯一约束元数据",
                            "how": "dbwatch.capture_snapshot 采集后自动可用",
                            "gain": "能直接判定 JOIN 右侧是否唯一，而不只是提示风险"}
    res.next("sql-review", "review_sql", sql=sql, dialect=d,
             reason="确认是否还有其他问题", priority=3, optional=True)
    return res


def reconcile(sql: str, dialect: str = "mysql", expected: float | None = None,
              actual: float | None = None, instance_id: str = "",
              correlation_id: str = "") -> Result:
    """数字对不上时的下钻计划 —— 按"先口径后数据"的顺序排查。"""
    d = normalize_dialect(dialect)
    out = SEM.reconcile(sql, d, expected=expected, actual=actual)
    if out.get("error"):
        raise ToolError("E_SQL_PARSE", out["error"])
    res = Result(
        ok=True, mcp=MCP_NAME, tool="reconcile", data=out,
        verdict="warn", confidence="medium", tier="tier0",
        summary=f"{len(out['steps'])} 步下钻计划"
                + (f"；你已经提供了期望值与实际值（差异 {out['input']['delta_pct']}%）"
                   if out["input"].get("delta_pct") is not None else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for h in out["hypothesis_order"]:
        res.add_evidence("external", "semantics.reconcile_order", h, "排查的优先级")
    res.warn("顺序很重要：先证明定义一致，再怀疑数据是错的。反过来做会浪费大量时间")
    res.next(MCP_NAME, "compare_definitions", dialect=d,
             reason="先确认两边的口径定义是否相同", priority=1, optional=False)
    res.next(MCP_NAME, "detect_fanout", sql=sql, dialect=d,
             reason="检查是否 JOIN 扇出导致放大", priority=2, optional=True)
    return res


def build_lineage(sqls: dict[str, str], dialect: str = "mysql",
                  instance_id: str = "", correlation_id: str = "") -> Result:
    """从一组 ETL/视图 SQL 推导表级与列级血缘。"""
    if not isinstance(sqls, dict) or not sqls:
        raise ToolError("E_ARGS", "sqls 必须是 {模型名: SQL} 形式的字典")
    d = normalize_dialect(dialect)
    out = SEM.build_lineage(sqls, d)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="build_lineage", data=out,
        verdict="pass", confidence="medium", tier="tier0",
        summary=f"{len(out['tables'])} 张表，{len(out['table_edges'])} 条依赖关系；"
                f"根表 {out['roots']}，末端 {out['leaves']}",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("ast", "lineage.static", out["uncertainty"], "血缘的可靠性边界")
    res.warn(out["uncertainty"])
    return res


def detect_drift(points: list[dict], tolerance_pct: float = 5.0,
                 instance_id: str = "", correlation_id: str = "") -> Result:
    """同一指标的时间序列漂移检测。**只有 1 个点时明确说不能判断**"""
    if not points:
        raise ToolError("E_ARGS", "points 不能为空，格式：[{'ts':..., 'value':...}]")
    out = SEM.detect_drift(points, tolerance_pct)
    insufficient = out.get("status") == "insufficient_data"
    res = Result(
        ok=True, mcp=MCP_NAME, tool="detect_drift", data=out,
        verdict="unknown" if insufficient else ("fail" if out.get("drifted") else "pass"),
        confidence="low" if insufficient else "high",
        tier="tier0" if insufficient else "tier2",
        summary=out.get("message") if insufficient
                else f"{out['points']} 个数据点，整体变化 {out.get('change_pct')}%"
                     f"，最大单步 {out.get('max_step_pct')}%",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    if insufficient:
        res.warn("数据点不足，无法判断趋势 —— 这里不会用一个点假装能判断漂移")
        res.tier_upgrade = {"to": "tier2", "need": "至少 2 个时间点的数据",
                            "how": "对该指标持续采样并记录，或用 dbwatch.capture_snapshot 积累",
                            "gain": "可以发现突然的口径变化或数据源变更"}
    else:
        res.add_evidence("history", "drift.series", f"{out['points']} 个样本",
                         f"最大单步变化 {out['max_step_pct']}%")
        res.next(MCP_NAME, "reconcile", dialect="mysql",
                 reason="漂移说明结果变了但不知道为什么，需要下钻", priority=2, optional=True)
    return res


def check_grain(sql: str, dialect: str = "mysql", instance_id: str = "",
                correlation_id: str = "") -> Result:
    """粒度冲突检测：明细 vs 聚合混用。"""
    d = normalize_dialect(dialect)
    out = SEM.check_grain(sql, d)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="check_grain", data=out,
        verdict=out["verdict"], confidence="high", tier="tier0",
        summary=f"{len(out['issues'])} 处粒度问题"
                + (f"：{out['issues'][0]['message'][:60]}" if out["issues"] else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for i in out["issues"][:5]:
        res.add_evidence("ast", "semantics.grain", i["message"][:140], i["effect"])
    return res


def check_timezone_window(sql_a: str, sql_b: str = "", dialect: str = "mysql",
                          instance_id: str = "", correlation_id: str = "") -> Result:
    """时区与日期窗口边界差异检查 —— 表现为"每天的数的都不一样"。"""
    d = normalize_dialect(dialect)
    da = SEM.parse_metric_definition(sql=sql_a, dialect=d, name="A")
    db = (SEM.parse_metric_definition(sql=sql_b, dialect=d, name="B")
          if sql_b else da)
    wa, wb = da.get("time_window") or {}, db.get("time_window") or {}
    diffs = []
    if (wa.get("timezone_hint") or None) != (wb.get("timezone_hint") or None):
        diffs.append({"aspect": "timezone",
                      "detail": f"A={wa.get('timezone_hint')} / B={wb.get('timezone_hint')}",
                      "impact": "UTC 与本地时区会让每日边界错位 8 小时",
                      "fix": "统一存储时区，统计时显式转换"})
    if (wa.get("window") or None) != (wb.get("window") or None):
        diffs.append({"aspect": "window", "detail": f"A={wa.get('window')} / B={wb.get('window')}",
                      "impact": "自然日 vs 滚动 24 小时，每天的统计结果系统性不同",
                      "fix": "明确业务日的切分点并写进指标契约"})
    if wa.get("timezone_unknown") or wb.get("timezone_unknown"):
        diffs.append({"aspect": "timezone_unspecified",
                      "detail": "至少一侧没有时区线索",
                      "impact": "无法判断是否跨时区，建议显式写明",
                      "fix": "在 SQL 中显式 CONVERT_TZ / AT TIME ZONE，不要依赖会话时区"})
    res = Result(
        ok=True, mcp=MCP_NAME, tool="check_timezone_window",
        data={"diffs": diffs, "a": wa, "b": wb, "count": len(diffs)},
        verdict="fail" if any(x["aspect"] == "timezone" for x in diffs)
                else ("warn" if diffs else "pass"),
        confidence="high" if sql_b else "medium", tier="tier0",
        summary=f"{len(diffs)} 处时间维度的差异"
                if diffs else "时间窗口定义一致",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for x in diffs:
        res.add_evidence("ast", f"semantics.time.{x['aspect']}", x["detail"], x["impact"])
    if not sql_b:
        res.warn("只提供了一段 SQL，只能检查它自身的窗口定义是否明确；"
                 "提供两段才能比对差异")
    return res


def suggest_canonical(definitions: list[dict], instance_id: str = "",
                      correlation_id: str = "") -> Result:
    """从多个候选口径里推荐标准口径，并列出需要改造的那些。"""
    if not definitions:
        raise ToolError("E_ARGS", "definitions 不能为空")
    out = SEM.suggest_canonical(definitions)
    if out.get("error"):
        raise ToolError("E_ARGS", out["error"])
    res = Result(
        ok=True, mcp=MCP_NAME, tool="suggest_canonical", data=out,
        verdict="warn" if out["need_change"] else "pass",
        confidence="medium", tier="tier0",
        summary=out["impact"],
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for o in out["need_change"][:5]:
        res.add_evidence("ast", f"canonical.gap.{o['name']}",
                         f"{o['diff_count']} 处差异（最严重 {o['worst']}）", "需要改造的依据")
    res.next(MCP_NAME, "generate_metric_contract",
             reason="把推荐口径固化成契约", priority=1, optional=False)
    return res


def generate_metric_contract(definition: dict | str, owner: str = "",
                             version: str = "1.0", dialect: str = "mysql",
                             instance_id: str = "", correlation_id: str = "") -> Result:
    """生成可读的指标契约 YAML —— 这是真正能落地的治理产物。"""
    d = normalize_dialect(dialect)
    if isinstance(definition, str):
        definition = SEM.parse_metric_definition(sql=definition, dialect=d)
    yaml_text = SEM.generate_metric_contract(definition, owner=owner, version=version)
    return Result(
        ok=True, mcp=MCP_NAME, tool="generate_metric_contract",
        data={"yaml": yaml_text, "name": definition.get("name"),
              "signature": definition.get("signature"), "lines": len(yaml_text.splitlines())},
        verdict="pass", confidence="high", tier="tier0",
        summary=f"契约 `{definition.get('name')}` 已生成（{len(yaml_text.splitlines())} 行）",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def record_definition(name: str, definition: dict | str, dialect: str = "mysql",
                      instance_id: str = "", correlation_id: str = "") -> Result:
    """写入指标字典（共享档案的一部分）。"""
    d = normalize_dialect(dialect)
    if isinstance(definition, str):
        definition = SEM.parse_metric_definition(sql=definition, name=name, dialect=d)
    definition["name"] = name
    put_dictionary("metric", name, definition)
    return Result(
        ok=True, mcp=MCP_NAME, tool="record_definition",
        data={"key": name, "signature": definition.get("signature")},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"口径 `{name}` 已写入指标字典（签名 {definition.get('signature')}）",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def get_definition(name: str, instance_id: str = "", correlation_id: str = "") -> Result:
    d = get_dictionary("metric", name)
    if d is None:
        raise ToolError("E_ARGS", f"指标字典中没有 `{name}`")
    return Result(
        ok=True, mcp=MCP_NAME, tool="get_definition", data=d,
        verdict="pass", confidence="high", tier="tier1",
        summary=f"`{name}`：{(d.get('grain'))}，去重键 {d.get('dedupe_key')}",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def list_definitions(limit: int = 50, instance_id: str = "",
                     correlation_id: str = "") -> Result:
    items = list_dictionary("metric", limit=limit)
    return Result(
        ok=True, mcp=MCP_NAME, tool="list_definitions",
        data={"items": items, "total": len(items)},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"指标字典共 {len(items)} 条定义",
        instance_id=instance_id, correlation_id=correlation_id,
    )


TOOLS = {
    "parse_metric_definition": parse_metric_definition,
    "compare_definitions": compare_definitions,
    "build_lineage": build_lineage,
    "detect_drift": detect_drift,
    "reconcile": reconcile,
    "suggest_canonical": suggest_canonical,
    "check_grain": check_grain,
    "detect_fanout": detect_fanout,
    "check_timezone_window": check_timezone_window,
    "generate_metric_contract": generate_metric_contract,
    "record_definition": record_definition,
    "get_definition": get_definition,
    "list_definitions": list_definitions,
}

for _n, _f in TOOLS.items():
    register(mcp, MCP_NAME, _f, _n)


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
