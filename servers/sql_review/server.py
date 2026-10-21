"""sql-review MCP —— SQL 体检与修正。

闭环：发现问题 → 给出修法 → 复验证明改对了。
工具清单见 DESIGN.md §3.1。

运行： python -m servers.sql_review
"""

from __future__ import annotations

from typing import Any

from servers._shared import register, make_server
from dbcore import fix as FIX
from dbcore import verify as VERIFY
from dbcore import advisor as ADV
from dbcore import rules as RULES
from dbcore import plan as PLAN
from dbcore import scanner as SCAN
from dbcore import migration as MIG
from dbcore.dialect import normalize_dialect
from dbcore.extract import extract_objects as extract_objects_impl
from dbcore.guard import GuardError
from dbcore.ids import sql_fingerprint, normalize_sql_for_fingerprint, instance_id_from_dsn
from dbcore.profile_store import record_observation
from dbcore.protocol import Result, ToolError, severity_to_verdict

MCP_NAME = "sql-review"
VERSION = "0.1.0"
mcp = make_server(MCP_NAME, VERSION)

_SEV_MAP = {"blocker": "critical", "high": "high", "medium": "medium",
            "low": "low", "info": "info"}


def _norm_sev(s: str) -> str:
    return _SEV_MAP.get(s, "medium")


def _verdict_of(findings: list[dict]) -> tuple[str, str]:
    if not findings:
        return "pass", "info"
    worst = min(findings, key=lambda f: RULES.SEVERITY_ORDER.get(f["severity"], 9))
    sev = _norm_sev(worst["severity"])
    return severity_to_verdict(sev), sev


_SEV_NOUN = {"critical": "blocker", "high": "high", "medium": "medium", "low": "low", "info": "info"}


# ==================================================================
# 1. review_sql —— 单段 SQL 静态体检
# ==================================================================


def review_sql(sql: str, dialect: str = "mysql", table_rows: int | None = None,
               instance_id: str = "", correlation_id: str = "",
               deep_offset_threshold: int = 1000) -> Result:
    """静态体检。不需要连库，最适合挂 CI。返回 verdict + findings。"""
    d = normalize_dialect(dialect)
    r = RULES.review_sql(sql, d, table_rows=table_rows,
                         deep_offset_threshold=deep_offset_threshold)
    data = r.to_dict()
    data["findings"] = [{**f, "severity_norm": _norm_sev(f["severity"])} for f in data["findings"]]
    verdict, sev = _verdict_of(data["findings"])
    fp = sql_fingerprint(sql)

    res = Result(
        ok=True, mcp=MCP_NAME, tool="review_sql", data=data,
        verdict=verdict, confidence="medium", tier="tier0",
        summary=f"体检 {data['statement_count']} 条语句，发现 {len(data['findings'])} 个问题"
                + (f"，最严重：{sev}" if sev != "info" else "，未发现明显问题"),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for f in data["findings"][:10]:
        res.add_evidence("ast", f"rule.{f['rule_id']}", f["message"],
                         f"严重度 {f['severity']}｜建议：{f['suggestion'][:80]}")
    if data["findings"]:
        res.next(MCP_NAME, "suggest_fixes", sql=sql, dialect=d,
                 reason="这些问题可以自动给出改法", priority=1, optional=False)
    if any(f["severity"] in ("blocker", "high") for f in data["findings"]):
        res.warn("存在 blocker/high 级问题，不建议直接上线")
    return res


# ==================================================================
# 2. review_batch —— CI 门禁
# ==================================================================


def review_batch(sqls: list[str], dialect: str = "mysql", fail_on: str = "critical",
                 instance_id: str = "", correlation_id: str = "") -> Result:
    """批量体检，输出 PASS/FAIL 门禁结论。fail_on 可选 critical|high|medium|low"""
    results = []
    for s in sqls:
        try:
            dd = RULES.review_sql(s, dialect).to_dict()
        except Exception as exc:
            dd = {"verdict": "unknown", "statement_count": 0, "unparsable": [s[:100]],
                  "findings": [{"rule_id": "PARSE000", "severity": "info", "line": None,
                                "table": None, "message": f"体检异常：{exc}", "suggestion": ""}]}
        worst = "info"
        for f in dd["findings"]:
            if RULES.SEVERITY_ORDER.get(f["severity"], 9) < RULES.SEVERITY_ORDER.get(worst, 9):
                worst = f["severity"]
        results.append({"sql": s[:300], "fingerprint": sql_fingerprint(s),
                        "verdict": dd.get("verdict"), "findings": dd["findings"],
                        "worst_severity": _norm_sev(worst)})

    order = ["info", "low", "medium", "high", "critical"]
    threshold = order.index(fail_on) if fail_on in order else order.index("critical")
    failed = [r for r in results if order.index(r["worst_severity"]) >= threshold]
    passed = not failed

    res = Result(
        ok=True, mcp=MCP_NAME, tool="review_batch",
        data={"total": len(results), "failed": len(failed),
              "gate": "PASS" if passed else "FAIL", "fail_on": fail_on, "items": results},
        verdict="pass" if passed else "fail", confidence="medium", tier="tier0",
        summary=f"共 {len(results)} 条，{len(failed)} 条未通过（阈值 {fail_on}）",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("ast", "batch_review", f"阈值 {fail_on}，未通过 {len(failed)} 条",
                     "CI 门禁判定依据")
    for f in failed[:5]:
        res.next(MCP_NAME, "suggest_fixes", sql=f["sql"], dialect=dialect,
                 reason="未通过门禁，需要修正", priority=1, optional=False)
    return res


# ==================================================================
# 3. review_diff / review_file —— CI 与存量扫描
# ==================================================================


def review_diff(repo_root: str = ".", base: str = "HEAD~1", head: str = "HEAD",
                dialect: str = "mysql", fail_on: str = "critical",
                instance_id: str = "", correlation_id: str = "") -> Result:
    """只体检 git diff 中新增/修改的 SQL —— CI 门禁的真实用法。"""
    import os
    try:
        items = SCAN.diff_items(repo_root, base, head)
    except Exception as exc:
        raise ToolError("E_QUERY", f"读取 git diff 失败：{exc}")
    if os.path.isdir(repo_root) and not items:
        pass
    batch = review_batch([i.sql for i in items], dialect=dialect, fail_on=fail_on)
    batch.tool = "review_diff"
    batch.data["sources"] = [{"file": i.file, "line": i.line, "source": i.source,
                              "statement": i.statement, "sql": i.sql[:200]} for i in items]
    batch.data["diff_range"] = f"{base}..{head}"
    batch.summary = f"{base}..{head} 提取到 {len(items)} 条 SQL，{batch.data['failed']} 条未通过"
    batch.instance_id = instance_id
    batch.correlation_id = correlation_id
    return batch


def review_file(path: str, dialect: str = "mysql", fail_on: str = "critical",
                instance_id: str = "", correlation_id: str = "") -> Result:
    """扫描文件/目录中的 SQL。支持 .sql / MyBatis XML / Java 注解与字符串常量。"""
    import os
    if os.path.isdir(path):
        items = SCAN.extract_from_dir(path)
    elif os.path.exists(path):
        items = SCAN.extract_from_file(path)
    else:
        raise ToolError("E_ARGS", f"路径不存在：{path}")
    batch = review_batch([i.sql for i in items], dialect=dialect, fail_on=fail_on)
    batch.tool = "review_file"
    batch.data["sources"] = [{"file": i.file, "line": i.line, "source": i.source,
                              "statement": i.statement, "sql": i.sql[:200]} for i in items]
    batch.summary = f"{path} 提取到 {len(items)} 条 SQL，{batch.data['failed']} 条未通过"
    batch.instance_id = instance_id
    batch.correlation_id = correlation_id
    return batch


# ==================================================================
# 4. suggest_fixes —— 给出修法（闭环第二步）
# ==================================================================


def suggest_fixes(sql: str, dialect: str = "mysql", columns: list[str] | None = None,
                  pk_column: str | None = None, table_rows: int | None = None,
                  instance_id: str = "", correlation_id: str = "") -> Result:
    """对每条问题给出可执行改法，并把改写后的 SQL 重新体检一遍证明改对了。"""
    d = normalize_dialect(dialect)
    out = FIX.suggest_fixes(sql, d, columns=columns, pk_column=pk_column, table_rows=table_rows)
    data = dict(out)
    data["fingerprint"] = sql_fingerprint(sql)
    actionable = data.get("actionable", 0)
    total = data.get("total", 0)

    if actionable == 0:
        verdict, conf = ("fail" if total else "pass"), "medium"
    elif total and len(data.get("resolved_all") or []) >= actionable:
        verdict, conf = "pass", "high"
    else:
        verdict, conf = "warn", "medium"

    res = Result(
        ok=True, mcp=MCP_NAME, tool="suggest_fixes", data=data,
        verdict=verdict, confidence=conf, tier="tier0",
        summary=f"{total} 个问题中 {actionable} 个已给出改法"
                + (f"；合并后可直接使用：{data['composed_sql'][:100]}"
                   if data.get("composed_sql") else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for it in data.get("items", [])[:8]:
        if it.get("fixed_sql"):
            res.add_evidence("ast", f"fix.{it['rule_id']}", (it.get("action") or "")[:120],
                             f"改写类型 {it.get('sql_kind')}")
    if data.get("composed_sql"):
        res.next(MCP_NAME, "verify_fix", original_sql=sql, fixed_sql=data["composed_sql"],
                 dialect=d, reason="改写后必须先验证等价性再上线", priority=1, optional=False)
        res.next("change-risk", "assess_change", sql=data["composed_sql"], dialect=d,
                 reason="若涉及 DDL 需评估变更风险", priority=3, optional=True)
    return res


# ==================================================================
# 5. 验证三件套
# ==================================================================


def verify_fix(original_sql: str, fixed_sql: str, dialect: str = "mysql",
               url: str = "", instance_id: str = "", correlation_id: str = "") -> Result:
    """验证改写是否等价。给 url 用真实行数比对；不给则先做静态结构比对。"""
    d = normalize_dialect(dialect)
    iid = instance_id or (instance_id_from_dsn(url) if url else "")
    if url:
        try:
            out = VERIFY.equivalence_check(url, original_sql, fixed_sql, d)
        except Exception as exc:
            raise ToolError("E_CONNECT", f"等价性校验失败：{exc}")
        tier = "tier1"
        equivalent = bool(out.get("equivalent"))
    else:
        out = static_equivalence(original_sql, fixed_sql, d)
        tier = "tier0"
        equivalent = bool(out.get("equivalent"))
    res = Result(
        ok=True, mcp=MCP_NAME, tool="verify_fix", data=out,
        verdict="pass" if equivalent else "fail",
        confidence="high" if url else "medium", tier=tier,
        summary=out.get("message") or ("等价性校验通过" if equivalent else "存在不等价风险，禁止直接替换"),
        instance_id=iid, correlation_id=correlation_id,
    )
    res.add_evidence("plan" if url else "ast", "equivalence_check",
                     str(out.get("evidence") or out.get("checks") or "")[:200], "等价性判定依据")
    if not url:
        res.warn("未连接数据库，只做了静态结构比对；真实等价性需提供 url 后重跑")
        res.tier_upgrade = {"to": "tier1", "need": "数据库连接串（只读账号即可）",
                            "how": "传入 url 参数", "gain": "用真实行数验证改写没有改变结果"}
    return res


def static_equivalence(a: str, b: str, dialect: str) -> dict:
    """不看数据也能做的等价性检查：投影/去重/分组/排序/分页差异。"""
    ea = extract_objects_impl(a, dialect)["statements"]
    eb = extract_objects_impl(b, dialect)["statements"]
    sa = ea[0] if ea else {}
    sb = eb[0] if eb else {}
    diffs = []
    if sa.get("has_distinct") != sb.get("has_distinct"):
        diffs.append({"aspect": "distinct", "detail": "去重语义变化（DISTINCT 被增删）"})
    ta = {t["qualified"] for t in (sa.get("tables") or [])}
    tb = {t["qualified"] for t in (sb.get("tables") or [])}
    if ta != tb:
        diffs.append({"aspect": "tables", "detail": f"涉及表不同：{sorted(ta)} vs {sorted(tb)}"})
    ga, gb = sorted(sa.get("group_by") or []), sorted(sb.get("group_by") or [])
    if ga != gb:
        diffs.append({"aspect": "group_by", "detail": f"分组维度不同：{ga} vs {gb}"})
    oa = [o["expr"] for o in (sa.get("order_by") or [])]
    ob = [o["expr"] for o in (sb.get("order_by") or [])]
    if oa != ob:
        diffs.append({"aspect": "order_by", "detail": f"排序不同：{oa} vs {ob}"})
    la = (sa.get("limit_offset") or {}).get("limit")
    lb = (sb.get("limit_offset") or {}).get("limit")
    if la != lb:
        diffs.append({"aspect": "limit", "detail": f"返回行数上限不同：{la} vs {lb}"})
    agg_a = {(x["func"], x["column"]) for x in (sa.get("aggregates") or [])}
    agg_b = {(x["func"], x["column"]) for x in (sb.get("aggregates") or [])}
    if agg_a != agg_b:
        diffs.append({"aspect": "aggregates",
                      "detail": f"聚合不同：{sorted(map(str, agg_a))} vs {sorted(map(str, agg_b))}"})
    return {
        "equivalent": not diffs, "checks": diffs, "diff_count": len(diffs),
        "method": "static_ast",
        "follow_up_sql": [f"SELECT COUNT(*) FROM ({a.rstrip(';')}) AS _orig",
                          f"SELECT COUNT(*) FROM ({b.rstrip(';')}) AS _fixed"],
        "message": ("静态结构一致（仍需真实数据校验）" if not diffs
                    else f"发现 {len(diffs)} 处结构差异，不能直接替换"),
    }


def compare_plans(before_sql: str, after_sql: str, url: str = "", dialect: str = "mysql",
                  plan_before: Any = None, plan_after: Any = None,
                  instance_id: str = "", correlation_id: str = "",
                  analyze: bool = False) -> Result:
    """对比改写前后的执行计划。**扫描行数反而上升会明确判为不可采用**"""
    d = normalize_dialect(dialect)
    if (plan_before is None or plan_after is None) and not url:
        raise ToolError("E_ARGS", "需要提供 url，或同时提供 plan_before/plan_after")
    if url:
        try:
            out = VERIFY.plan_compare(url, before_sql, after_sql, d, analyze=analyze)
        except Exception as exc:
            raise ToolError("E_CONNECT", f"执行计划对比失败：{exc}")
        iid = instance_id or instance_id_from_dsn(url)
        tier = "tier1"
        gains = out.get("gains", {})
    else:
        nb = PLAN.normalize_plan(plan_before, d)
        na = PLAN.normalize_plan(plan_after, d)
        gains = PLAN.diff_plans(nb, na)
        out = {"gains": gains, "verdict": gains["verdict"], "before": nb, "after": na,
               "summary": gains["summary"], "regressions": gains["regressions"]}
        iid = instance_id
        tier = "tier0"
    verdict = {"improved": "pass", "neutral": "warn", "regressed": "fail"}.get(
        out.get("verdict", "neutral"), "warn")
    res = Result(
        ok=True, mcp=MCP_NAME, tool="compare_plans", data=out,
        verdict=verdict, confidence="high" if url else "medium", tier=tier,
        summary=out.get("summary", ""), instance_id=iid, correlation_id=correlation_id,
    )
    for k in ("full_scan_removed", "filesort_removed", "index_gained", "covering_gained"):
        if gains.get(k):
            res.add_evidence("plan", f"plan_diff.{k}", k, "改写产生的正面变化")
    for rg in gains.get("regressions", []):
        res.add_evidence("plan", "plan_diff.regression", rg, "★ 不可采用的理由")
    return res


def advise_indexes(table: str, candidates: list[str], url: str = "",
                   dialect: str = "mysql", existing_indexes: list[dict] | None = None,
                   table_rows: int | None = None, instance_id: str = "",
                   correlation_id: str = "") -> Result:
    """索引建议。已有重复索引或前缀已覆盖会直接劝退。"""
    d = normalize_dialect(dialect)
    if url:
        try:
            out = ADV.advise_indexes(url, table, candidates, d, table_rows=table_rows)
        except Exception as exc:
            raise ToolError("E_CONNECT", f"索引建议失败：{exc}")
        tier, iid = "tier1", instance_id or instance_id_from_dsn(url)
    else:
        out = ADV.plan_index(existing_indexes or [], candidates, table, d)
        tier, iid = "tier0", instance_id
        out["warning"] = ("未提供连接信息，只基于给定的现有索引列表做去重判断；"
                          "真实场景建议连库读取 Cardinality 与实际索引")
    should = out.get("should_create", False)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="advise_indexes", data=out,
        verdict="pass" if should else "warn",
        confidence="high" if url else "medium", tier=tier,
        summary=out.get("reason") or (f"建议创建索引：{out.get('ddl')}" if should
                                      else "不建议创建该索引"),
        instance_id=iid, correlation_id=correlation_id,
    )
    if out.get("conflicts"):
        res.add_evidence("metadata", "index_conflict", str(out["conflicts"])[:200],
                         "与已有索引冲突，劝退依据")
    if should:
        res.next("change-risk", "assess_change", sql=out.get("ddl", ""), dialect=d,
                 table_rows=table_rows, reason="建索引属于 DDL，需评估锁影响与推荐工具",
                 priority=1, optional=False)
    return res


def explain_sql(sql: str, url: str, dialect: str = "mysql", analyze: bool = False,
                instance_id: str = "", correlation_id: str = "") -> Result:
    """取执行计划并归一化，标注可疑点。"""
    from dbcore.connectors import explain as do_explain
    d = normalize_dialect(dialect)
    try:
        raw = do_explain(url, sql, d, analyze=analyze)
    except GuardError as exc:
        raise ToolError("E_READONLY_VIOLATION", str(exc))
    except Exception as exc:
        raise ToolError("E_CONNECT", f"获取执行计划失败：{exc}")
    iid = instance_id or instance_id_from_dsn(url)
    text = raw.get("plan_text", "")
    norm = PLAN.normalize_plan(raw.get("rows") if d in ("mysql", "mariadb", "oceanbase") else text, d)
    data = {"plan_text": text, "normalized": norm, "issues": raw.get("issues", [])}
    flags = [k for k, v in (norm.get("flags") or {}).items() if v is True]
    verdict = "fail" if ("full_scan" in flags or "bad_filtered" in flags) else (
        "warn" if ("filesort" in flags or "temp_table" in flags or "no_index" in flags) else "pass")
    res = Result(
        ok=True, mcp=MCP_NAME, tool="explain_sql", data=data,
        verdict=verdict, confidence="high", tier="tier1",
        summary=f"扫描行数 {norm.get('totals', {}).get('rows_examined', 0):,}"
                + ("｜标记：" + "、".join(norm.get("human_flags", [])) if flags else "｜未见明显问题"),
        instance_id=iid, correlation_id=correlation_id,
    )
    for k in flags:
        res.add_evidence("plan", f"plan_flag.{k}", PLAN._FLAGS.get(k, k), "执行计划可疑点")
    if verdict != "pass":
        res.next(MCP_NAME, "review_sql", sql=sql, dialect=d,
                 reason="结合静态规则找可优化点", priority=2, optional=True)
    record_observation(iid or "unknown", "explain",
                       {"fingerprint": sql_fingerprint(sql), "totals": norm.get("totals")})
    return res


# ==================================================================
# 6. 辅助 / 元工具
# ==================================================================


def normalize_sql(sql: str, target_dialect: str = "postgres", source_dialect: str = "mysql",
                  instance_id: str = "", correlation_id: str = "") -> Result:
    """方言转换（内部复用 migration-diff 的规则库）。"""
    out = MIG.translate_sql(sql, source_dialect, target_dialect)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="normalize_sql", data=out,
        verdict="warn" if not out.get("target") else "pass", confidence="medium", tier="tier0",
        summary=f"{source_dialect} → {target_dialect}："
                + ("转换完成（需验证语义等价）" if out.get("target") else "无法自动转换"),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    if out.get("target"):
        res.next("migration-diff", "detect_dialect_gap", sql=out["target"],
                 target_dialect=target_dialect, source_dialect=source_dialect,
                 reason="转换后仍需检查残留的不兼容写法", priority=1, optional=False)
    return res


def fingerprint_sql(sql: str, dialect: str = "mysql",
                    instance_id: str = "", correlation_id: str = "") -> Result:
    """生成 SQL 指纹与归一化形式（跨 MCP 关联键之一）。"""
    return Result(
        ok=True, mcp=MCP_NAME, tool="fingerprint_sql",
        data={"fingerprint": sql_fingerprint(sql),
              "normalized": normalize_sql_for_fingerprint(sql),
              "dialect": normalize_dialect(dialect)},
        verdict="pass", confidence="high", tier="tier0",
        summary="已生成 SQL 指纹（字面量不同也得到同一指纹）",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def extract_objects(sql: str, dialect: str = "mysql",
                    instance_id: str = "", correlation_id: str = "") -> Result:
    """把 SQL 抽成结构化对象，供其它 MCP 直接消费（内部的分 Claim 提取实现在 dbcore.extract）。"""
    out = extract_objects_impl(sql, dialect)
    return Result(
        ok=True, mcp=MCP_NAME, tool="extract_objects", data=out,
        verdict="pass", confidence="high", tier="tier0",
        summary=f"{out['statement_count']} 条语句，涉及表：{out['all_tables']}",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def list_rules(category: str | None = None, severity: str | None = None,
               dialect: str | None = None, instance_id: str = "",
               correlation_id: str = "") -> Result:
    return Result(
        ok=True, mcp=MCP_NAME, tool="list_rules",
        data={"rules": RULES.list_rules(category, severity, dialect),
              "categories": RULES.rule_categories(),
              "total_registered": len(RULES.RULES)},
        verdict="pass", confidence="high", tier="tier0",
        summary=f"共 {len(RULES.RULES)} 条规则已注册，{len(RULES.RULE_META)} 条有元数据说明",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def explain_rule(rule_id: str, instance_id: str = "", correlation_id: str = "") -> Result:
    info = RULES.explain_rule(rule_id)
    if info is None:
        raise ToolError("E_ARGS", f"未知规则 ID：{rule_id}")
    r = Result(
        ok=True, mcp=MCP_NAME, tool="explain_rule", data=info,
        verdict="pass", confidence="high", tier="tier0",
        summary=f"{info['name']}（{info['severity']}）：{info['why']}",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    r.add_evidence("external", f"rules.{rule_id}", info["why"], "规则存在的原因")
    return r




# ==================================================================
# 注册
# ==================================================================

TOOLS = {
    "review_sql": review_sql,
    "review_batch": review_batch,
    "review_diff": review_diff,
    "review_file": review_file,
    "suggest_fixes": suggest_fixes,
    "verify_fix": verify_fix,
    "compare_plans": compare_plans,
    "advise_indexes": advise_indexes,
    "explain_sql": explain_sql,
    "normalize_sql": normalize_sql,
    "fingerprint_sql": fingerprint_sql,
    "extract_objects": extract_objects,
    "list_rules": list_rules,
    "explain_rule": explain_rule,
}

for _name, _fn in TOOLS.items():
    register(mcp, MCP_NAME, _fn, _name)


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
