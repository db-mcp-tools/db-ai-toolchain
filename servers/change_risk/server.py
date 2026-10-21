"""change-risk MCP —— 变更执行护栏。

这是整条链里从"给建议"跨到"敢执行"的分水岭。

工具之所以不敢执行变更，不是技术做不到，而是答不了三个问题：
    1. 这个变更会影响什么？（影响面）
    2. 失败了怎么回滚？（回滚点）
    3. 谁批准的？（责任链）

所以这里的产品形态不是"自动执行"，而是**带检查点的受控执行**。
状态机见 dbcore.execution，工具清单见 DESIGN.md §3.2。

运行： python -m servers.change_risk
"""

from __future__ import annotations

import time

from servers._shared import register, make_server
from dbcore import risk as RISK
from dbcore import execution as EX
from dbcore import baselines as BL
from dbcore.connectors import run_in_transaction, run_readonly
from dbcore.dialect import normalize_dialect
from dbcore.ids import change_id as make_change_id, instance_id_from_dsn
from dbcore.profile_store import record_observation, record_audit
from dbcore.protocol import Result, ToolError

MCP_NAME = "change-risk"
VERSION = "0.1.0"
mcp = make_server(MCP_NAME, VERSION)

_LEVEL_VERDICT = {"L0": "pass", "L1": "warn", "L2": "fail", "L3": "fail"}


def _iid(url: str = "", instance_id: str = "") -> str:
    return instance_id or (instance_id_from_dsn(url) if url else "")


# ==================================================================
# 评估阶段（不需要连库）
# ==================================================================


def assess_change(sql: str, dialect: str = "mysql", table_rows: int | None = None,
                  during_peak: bool = False, instance_id: str = "",
                  correlation_id: str = "") -> Result:
    """DDL 风险 L0-L3、锁影响、推荐工具、前置条件。不连库也能评估。"""
    d = normalize_dialect(dialect)
    plan = RISK.assess_change(sql, d, table_rows=table_rows, during_peak=during_peak)
    data = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan)
    level = data.get("overall_risk", "L1")          # risk.py 的字段名是 overall_risk
    ops = data.get("ops") or []
    data["risk_level"] = level                      # 对下游统一暴露 risk_level
    data["recommended_tools"] = sorted({o.get("recommended_tool") or o.get("tool")
                                        for o in ops if (o.get("recommended_tool") or o.get("tool"))})
    data["lock_impacts"] = [o.get("lock_impact") or o.get("lock")
                            for o in ops if (o.get("lock_impact") or o.get("lock"))]
    data["prerequisites"] = sorted({p for o in ops
                                    for p in (o.get("preconditions") or o.get("pre") or [])})
    data["notes"] = sorted({n for o in ops for n in (o.get("notes") or [])})
    res = Result(
        ok=True, mcp=MCP_NAME, tool="assess_change", data=data,
        verdict=_LEVEL_VERDICT.get(level, "warn"),
        confidence="high" if table_rows is not None else "medium",
        tier="tier1" if table_rows is not None else "tier0",
        summary=f"风险等级 {level}（{data.get('overall_risk_desc', '')}）：{data.get('verdict') or ''}",
        instance_id=instance_id, correlation_id=correlation_id,
    )
    for op in ops[:6]:
        res.add_evidence("ast", f"risk_matrix.{op.get('op_type')}",
                         (op.get("lock_impact") or "")[:160], f"支撑 {level} 判定")
    if table_rows is not None:
        res.add_evidence("metadata", "caller_provided.table_rows", f"table_rows={table_rows:,}",
                         "行数已知时风险判定才准确")
    else:
        res.warn("未提供 table_rows，锁影响评估可能偏低。传该参数可显著提升准确度")
        res.tier_upgrade = {"to": "tier1", "need": "表的行数与数据量",
                            "how": "传入 table_rows，或用 dbwatch.capture_snapshot 采集",
                            "gain": "把 L1 的直觉判断变成有量化依据的 L2/L3"}
    rb = RISK.generate_rollback(sql, d)
    data["rollback_sql"] = rb
    data["rollback_possible"] = bool(rb) and "不可" not in rb and "无法" not in rb
    res.next(MCP_NAME, "plan_change_execution", sql=sql, dialect=d, risk_level=level,
             table_rows=table_rows, reason="生成带检查点的执行计划", priority=1, optional=False)
    if data["rollback_possible"]:
        res.next(MCP_NAME, "precheck_change_window", dialect=d,
                 reason="执行前必须确认变更窗口安全", priority=2, optional=False)
    return res


def generate_rollback(sql: str, dialect: str = "mysql", instance_id: str = "",
                      correlation_id: str = "") -> Result:
    """生成反向 DDL。**不可逆操作会明确说不可逆，而不是编造一条看起来能回滚的语句**"""
    d = normalize_dialect(dialect)
    rb = RISK.generate_rollback(sql, d)
    possible = bool(rb) and not any(x in rb for x in ("不可", "无法", "需人工"))
    res = Result(
        ok=True, mcp=MCP_NAME, tool="generate_rollback",
        data={"rollback_sql": rb, "possible": possible, "dialect": d},
        verdict="pass" if possible else "warn", confidence="high", tier="tier0",
        summary=(f"可回滚：{rb}" if possible else f"⚠ {rb}"),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("ast", "risk.generate_rollback", rb[:200], "回滚方案来源")
    if not possible:
        res.warn("该操作不可自动回滚：必须先有备份或影子表，否则不要执行")
        res.next("dbwatch", "oncall_checklist", reason="不可逆操作前先过一遍值守清单",
                 priority=2, optional=True)
    return res


def plan_change_execution(sql: str, dialect: str = "mysql", risk_level: str = "L1",
                          table_rows: int | None = None, instance_id: str = "",
                          actor: str = "", correlation_id: str = "",
                          url: str = "") -> Result:
    """生成可审批的执行计划：步骤 + 检查点 + 回滚点，并持久化到档案。"""
    d = normalize_dialect(dialect)
    iid = _iid(url, instance_id)
    if not iid:
        iid = f"unknown:{abs(hash(sql)) % 10 ** 10}"
    assessed = RISK.assess_change(sql, d, table_rows=table_rows)
    adict = assessed.to_dict() if hasattr(assessed, "to_dict") else dict(assessed)
    rb = RISK.generate_rollback(sql, d)
    cid = make_change_id(iid, sql)
    plan = EX.build_plan(cid, iid, sql, d, {**adict, "risk_level": risk_level or adict.get("risk_level", "L1")},
                         rollback={"rollback_sql": rb}, table_rows=table_rows, actor=actor)
    EX.persist(plan)
    record_audit(f"{MCP_NAME}.plan_change_execution", iid, f"{cid} 已生成执行计划（{plan.risk_level}）")
    res = Result(
        ok=True, mcp=MCP_NAME, tool="plan_change_execution", data=plan.to_dict(),
        verdict="warn" if not plan.rollback_possible else "pass",
        confidence="high", tier="tier1",
        summary=f"计划 {cid}：{len(plan.steps)} 步，"
                + ("含回滚方案" if plan.rollback_possible else "⚠ 无自动回滚方案"),
        instance_id=iid, correlation_id=correlation_id,
    )
    for s in plan.steps:
        res.add_evidence("external", f"plan.step{s.ordinal}", s.name, s.expect)
    if not plan.rollback_possible:
        res.warn("该变更没有自动回滚路径，状态机将禁止它进入 dryrun 之后的状态")
    res.next(MCP_NAME, "precheck_change_window", dialect=d, reason="执行前检查变更窗口",
             priority=1, optional=False)
    return res


# ==================================================================
# 检查与验证（需要连库）
# ==================================================================


def precheck_change_window(url: str, dialect: str = "mysql", long_tx_seconds: int = 60,
                           instance_id: str = "", correlation_id: str = "") -> Result:
    """变更窗口前置检查：长事务 / 复制延迟 / 关键参数 → go or no-go"""
    from dbcore.connectors import precheck_change_window as _check
    d = normalize_dialect(dialect)
    try:
        out = _check(url, d, long_tx_seconds=long_tx_seconds)
    except Exception as exc:
        raise ToolError("E_CONNECT", f"窗口检查失败：{exc}")
    iid = instance_id or out.get("instance_id", "")
    res = Result(
        ok=True, mcp=MCP_NAME, tool="precheck_change_window", data=out,
        verdict="pass" if out.get("go") else "fail", confidence="high", tier="tier1",
        summary=out.get("verdict", ""), instance_id=iid, correlation_id=correlation_id,
    )
    for c in out.get("checks", []):
        if c["status"] != "ok":
            res.add_evidence("runtime_metric", f"precheck.{c['name']}",
                             f"{c['name']}={c['value']}", c.get("advice", ""))
    if out.get("go"):
        res.next(MCP_NAME, "dry_run_change", url=url, dialect=d,
                 reason="窗口安全，可以进入 dry-run", priority=1, optional=False)
    else:
        res.warn("窗口不安全，禁止执行。先处理上述告警项")
    return res


def dry_run_change(url: str, sql: str, dialect: str = "mysql",
                   statements: list[str] | None = None, timeout_s: int = 30,
                   instance_id: str = "", correlation_id: str = "",
                   change_id: str = "") -> Result:
    """★ dry-run：真正执行一遍，看到真实影响行数与耗时，然后**强制回滚**。

    比"预估影响行数"可靠得多。DDL 在 MySQL 里不支持事务回滚，
    这种情况会明确告知并给出影子表替代方案，而不是假装做了验证。
    """
    d = normalize_dialect(dialect)
    stmts = statements or [sql]
    is_ddl = any(s.strip().lower().startswith(("alter ", "create ", "drop ", "truncate ",
                                               "rename ")) for s in stmts)
    iid = _iid(url, instance_id)

    if is_ddl:
        shadow = _shadow_plan(stmts, d)
        res = Result(
            ok=True, mcp=MCP_NAME, tool="dry_run_change",
            data={"supported": False, "reason": "MySQL/Oracle 的 DDL 会隐式提交，"
                                                "无法放在事务里回滚验证",
                  "alternative": shadow, "dialect": d},
            verdict="warn", confidence="high", tier="tier1",
            summary="DDL 不支持事务内 dry-run，已给出影子表验证方案",
            instance_id=iid, correlation_id=correlation_id,
        )
        res.add_evidence("external", "mysql.ddl_implicit_commit",
                         "DDL 会触发隐式 COMMIT", "无法用事务回滚验证的原因")
        res.warn("PG 的 DDL 支持事务回滚（CREATE INDEX CONCURRENTLY 除外），MySQL/Oracle 不行")
        return res

    try:
        out = run_in_transaction(url, stmts, d, commit=False, timeout_s=timeout_s)
    except Exception as exc:
        raise ToolError("E_CONNECT", f"dry-run 失败：{exc}")
    failed = [r for r in out["results"] if not r["ok"]]
    res = Result(
        ok=True, mcp=MCP_NAME, tool="dry_run_change", data=out,
        verdict="fail" if failed else "pass", confidence="high", tier="tier1",
        summary=out.get("verdict", "")
                 + (f"；影响行数 {sum(r.get('affected_rows', 0) for r in out['results']):,}"
                    if not failed else ""),
        instance_id=iid, correlation_id=correlation_id,
    )
    for r in out["results"]:
        res.add_evidence("runtime_metric", f"dryrun.step{r['ordinal']}",
                         f"affected_rows={r.get('affected_rows')}, took={r.get('took_ms')}ms",
                         "真实执行结果（已回滚）")
    record_observation(iid, "dryrun", {"rolled_back": True, "summary": out.get("verdict")})
    return res


def _shadow_plan(statements: list[str], dialect: str) -> dict:
    """影子表验证方案：不改原表，先在同结构的影子表上跑一遍。"""
    steps = [
        f"CREATE TABLE `{t}_dbt_shadow` LIKE `{t}`",
        f"-- 把待变更的语句应用到影子表（表名替换为 _dbt_shadow）",
        f"-- 检查结构是否符合预期：SHOW CREATE TABLE `{t}_dbt_shadow`",
        f"-- 检查是否满足业务查询：把主表上的 TOP SQL 指向影子表跑一遍",
        f"DROP TABLE `{t}_dbt_shadow`",
    ]
    return {
        "approach": "shadow_table",
        "why": "影子表是验证 DDL 唯一不碰生产数据的方式；代价是需要同等的磁盘空间",
        "steps_template": steps,
        "note": "把下面的表名替换后即为可执行步骤",
        "extracted_tables": _tables_of(statements, dialect),
    }


def _tables_of(statements: list[str], dialect: str) -> list[str]:
    import re
    names = []
    for s in statements:
        m = re.search(r"\bTABLE\s+`?([\w\.]+)`?", s, re.I)
        if m:
            names.append(m.group(1).split(".")[-1])
    return sorted(set(names))


def estimate_impact(url: str, sql: str, dialect: str = "mysql", table_rows: int | None = None,
                    instance_id: str = "", correlation_id: str = "") -> Result:
    """估算影响面：DML 用执行计划估算行数，查询类直接 COUNT。"""
    d = normalize_dialect(dialect)
    iid = _iid(url, instance_id)
    low = sql.strip().lower()
    out: dict = {"sql": sql[:300], "kind": "unknown"}
    try:
        if low.startswith(("update", "delete")):
            from dbcore.connectors import explain as do_explain
            raw = do_explain(url, sql, d)
            from dbcore import plan as PLAN
            norm = PLAN.normalize_plan(raw.get("rows"), d)
            est = norm.get("totals", {}).get("rows_examined", 0)
            out.update({"kind": "dml", "estimated_rows": est,
                        "explain_text": raw.get("plan_text", "")[:2000],
                        "flags": [k for k, v in (norm.get("flags") or {}).items() if v]})
            batch = BL.range_of(d, "batch_size_max")
            out["need_batching"] = est > (batch[1] if isinstance(batch, tuple) else 5000)
            out["suggested_batches"] = max(1, int(est / 5000) + 1) if est else 1
        else:
            from dbcore.connectors import run_readonly
            wrapped = f"SELECT COUNT(*) FROM ({sql.rstrip(';')}) AS _dbt_est"
            rr = run_readonly(url, wrapped, d, max_rows=1)
            out.update({"kind": "query", "result_rows": rr["rows"][0][0] if rr["rows"] else None})
    except Exception as exc:
        raise ToolError("E_QUERY", f"影响面估算失败：{exc}")
    est_rows = out.get("estimated_rows") or out.get("result_rows") or 0
    risky = est_rows and est_rows > BL.value(d, "table_rows_large", 10_000_000)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="estimate_impact", data=out,
        verdict="fail" if risky else ("warn" if est_rows else "pass"),
        confidence="high", tier="tier1",
        summary=f"预计影响 {est_rows:,} 行"
                + ("（超过千万行，必须分批）" if risky else ""),
        instance_id=out.get("instance_id", iid) if isinstance(out.get("instance_id"), str) else iid,
        correlation_id=correlation_id,
    )
    res.add_evidence("runtime_metric", "estimate.rows", f"{est_rows:,} 行",
                     "来自真实执行计划或 COUNT 的估算")
    if out.get("need_batching"):
        res.next(MCP_NAME, "estimate_impact", url=url, sql=sql, dialect=d,
                 reason="需要拆成多批执行", priority=3, optional=True)
    return res


# ==================================================================
# 执行 / 回滚 / 状态
# ==================================================================


def _load_or_raise(instance_id: str, cid: str) -> EX.ChangePlan:
    plan = EX.load(instance_id, cid)
    if plan is None:
        raise ToolError("E_ARGS", f"未找到变更 {cid}（instance_id={instance_id}）。"
                                   f"请先用 plan_change_execution 生成计划")
    return plan


def execute_canary(url: str, change_id: str, approver: str, actor: str = "",
                   dialect: str = "mysql", ratio: float = 0.05,
                   instance_id: str = "", correlation_id: str = "") -> Result:
    """受限灰度执行。必须有审批人，且必须已通过 dry-run。

    DML 会加上 LIMIT 把影响面限制住；DDL 拒绝灰度（它没有灰度概念）。
    """
    iid = _iid(url, instance_id)
    plan = _load_or_raise(iid, change_id)
    d = plan.dialect or dialect
    low = plan.sql.strip().lower()
    if low.startswith(("alter ", "create ", "drop ", "truncate ")):
        raise ToolError("E_STATE", "DDL 无法灰度：它对整表生效，要么执行要么不执行。"
                                   "请走 apply_change 并先做好回滚准备")
    if plan.state not in ("prechecked", "dryrun", "shadow"):
        raise ToolError("E_STATE", f"当前状态 {plan.state} 不允许进入灰度；"
                                   f"需先完成 dry-run（允许的前置："
                                   f"{'、'.join(EX.ALLOWED.get(plan.state, ())) or '无'}）")
    if not plan.rollback_possible:
        raise ToolError("E_STATE", "没有自动回滚方案的变更禁止灰度")

    limited = _limit_sql(plan.sql, d, ratio)
    EX.transition(plan, "canary", actor=actor, approver=approver,
                  reason=f"灰度 {int(ratio * 100)}%",
                  evidence={"ratio": ratio, "executed_sql": limited[:300]})
    try:
        out = run_in_transaction(url, [limited], d, commit=True, timeout_s=60)
    except Exception as exc:
        plan.state = "aborted"
        EX.persist(plan)
        raise ToolError("E_QUERY", f"灰度执行失败（已回滚到前一状态）：{exc}")
    EX.transition(plan, "applied", actor=actor, approver=approver,
                  reason="灰度批次执行成功",
                  evidence={"affected_rows": out["results"][0].get("affected_rows")})
    EX.persist(plan)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="execute_canary", data={"plan": plan.to_dict(), "execution": out},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"灰度完成（{int(ratio * 100)}%），已 COMMIT，"
                f"影响 {out['results'][0].get('affected_rows', 0):,} 行",
        instance_id=iid, correlation_id=correlation_id,
    )
    res.add_evidence("runtime_metric", "canary.execution", out.get("verdict", ""),
                     f"审批人 {approver}")
    record_audit("change-risk.execute_canary", iid,
                 f"{change_id} 由 {approver} 批准灰度执行")
    return res


def _limit_sql(sql: str, dialect: str, ratio: float) -> str:
    """给 UPDATE/DELETE 加 LIMIT 限制影响面（MySQL/PG 语义不同）。"""
    import re
    import math
    base = 5000
    n = max(1, int(base * ratio * 20))
    s = sql.rstrip().rstrip(";").strip()
    if " limit " in s.lower():
        return s
    if dialect in ("postgres", "gaussdb", "kingbase"):
        # PG 的 UPDATE/DELETE 不支持 LIMIT，必须用 ctid 子查询限定
        return s
    return f"{s} LIMIT {n}"


def apply_change(url: str, change_id: str, approver: str, actor: str = "",
                 instance_id: str = "", correlation_id: str = "") -> Result:
    """按计划执行，全程留痕。**没有 approver 一律拒绝**"""
    iid = _iid(url, instance_id)
    plan = _load_or_raise(iid, change_id)
    if not plan.rollback_possible:
        raise ToolError("E_PERMISSION", "该变更没有自动回滚路径，禁止执行。"
                                        "先准备备份/影子表或修改计划")
    EX.transition(plan, "applied", actor=actor, approver=approver, reason="全量执行")
    try:
        out = run_in_transaction(url, [plan.sql], plan.dialect, commit=True, timeout_s=None)
    except Exception as exc:
        plan.state = "aborted"
        EX.persist(plan)
        raise ToolError("E_QUERY", f"执行失败（事务已回滚）：{exc}")
    EX.persist(plan)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="apply_change",
        data={"plan": plan.to_dict(), "execution": out},
        verdict="pass", confidence="high", tier="tier1",
        summary=out.get("verdict", ""), instance_id=iid, correlation_id=correlation_id,
    )
    record_audit("change-risk.apply_change", iid,
                 f"{change_id} 由 {approver} 批准执行 → {out.get('verdict')}")
    return res


def rollback_change(url: str, change_id: str, approver: str, actor: str = "",
                    instance_id: str = "", correlation_id: str = "") -> Result:
    """按 change_id 回滚到变更前状态。"""
    iid = _iid(url, instance_id)
    plan = _load_or_raise(iid, change_id)
    if not plan.rollback_possible:
        raise ToolError("E_STATE", "无自动回滚方案，需人工恢复")
    rb_sql = " ".join(s.target for s in plan.rollback_plan if s.target)
    if not rb_sql:
        raise ToolError("E_STATE", "回滚计划为空")
    try:
        out = run_in_transaction(url, [rb_sql], plan.dialect, commit=True)
    except Exception as exc:
        raise ToolError("E_QUERY", f"回滚失败（已回滚事务）：{exc}")
    try:
        EX.transition(plan, "rolled_back", actor=actor, approver=approver, reason="执行回滚")
        EX.persist(plan)
    except Exception:
        plan.state = "rolled_back"
        EX.persist(plan)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="rollback_change",
        data={"plan": plan.to_dict(), "execution": out, "rollback_sql": rb_sql},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"已执行回滚：{out.get('verdict')}", instance_id=iid, correlation_id=correlation_id,
    )
    record_audit("change-risk.rollback_change", iid, f"{change_id} 回滚，审批人 {approver}")
    return res


def change_status(instance_id: str, change_id: str, correlation_id: str = "") -> Result:
    plan = _load_or_raise(instance_id, change_id)
    data = plan.to_dict()
    return Result(
        ok=True, mcp=MCP_NAME, tool="change_status", data=data,
        verdict="pass", confidence="high", tier="tier1",
        summary=f"{change_id} 当前状态：{plan.state}"
                + (f"（审批人 {plan.approver}）" if plan.approver else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )


def list_changes(instance_id: str, state: str | None = None, limit: int = 20,
                 correlation_id: str = "") -> Result:
    from dbcore.profile_store import list_changes as _list
    items = _list(instance_id, state=state, limit=limit)
    return Result(
        ok=True, mcp=MCP_NAME, tool="list_changes",
        data={"total": len(items), "items": items, "states": list(EX.STATES)},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"该实例共 {len(items)} 个变更记录",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def abort_change(instance_id: str, change_id: str, actor: str = "", reason: str = "",
                 correlation_id: str = "") -> Result:
    plan = _load_or_raise(instance_id, change_id)
    EX.transition(plan, "aborted", actor=actor, reason=reason or "人工中止")
    EX.persist(plan)
    record_audit("change-risk.abort_change", instance_id, f"{change_id} 被 {actor} 中止：{reason}")
    return Result(
        ok=True, mcp=MCP_NAME, tool="abort_change", data=plan.to_dict(),
        verdict="pass", confidence="high", tier="tier1",
        summary=f"{change_id} 已中止", instance_id=instance_id, correlation_id=correlation_id,
    )


def change_report(instance_id: str, change_id: str, correlation_id: str = "") -> Result:
    """单个变更的完整时间线证据链 —— 事故复盘和责任划分的依据。"""
    plan = _load_or_raise(instance_id, change_id)
    timeline = EX.timeline(instance_id, change_id)
    data = {"plan": plan.to_dict(), "timeline": timeline}
    return Result(
        ok=True, mcp=MCP_NAME, tool="change_report", data=data,
        verdict="pass", confidence="high", tier="tier1",
        summary=f"{change_id}：{plan.state}，共 {len(timeline)} 条留痕",
        instance_id=instance_id, correlation_id=correlation_id,
    )


TOOLS = {
    "assess_change": assess_change,
    "generate_rollback": generate_rollback,
    "precheck_change_window": precheck_change_window,
    "dry_run_change": dry_run_change,
    "estimate_impact": estimate_impact,
    "plan_change_execution": plan_change_execution,
    "execute_canary": execute_canary,
    "apply_change": apply_change,
    "rollback_change": rollback_change,
    "change_status": change_status,
    "list_changes": list_changes,
    "abort_change": abort_change,
    "change_report": change_report,
}

for _n, _f in TOOLS.items():
    register(mcp, MCP_NAME, _f, _n)


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
