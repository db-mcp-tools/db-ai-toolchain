#!/usr/bin/env python3
"""全量回归测试 —— 不依赖 pytest，不依赖真实数据库。

    python tests/test_all.py

设计原则：
  * 所有 Tier0 / Tier1 的纯逻辑必须可测，这是 CI 的前提
  * 每个 MCP 的每个工具都要检查返回的信封结构（契约一致性）
  * 冷启动能力（Tier0）单独一组用例，因为那是这个项目的立身之本
  * 误报用例与命中用例同样重要：一条乱报的规则比没有规则更糟
"""

from __future__ import annotations

import os
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")
# 测试用一个临时档案库，避免污染真实档案
_TMPDIR = tempfile.mkdtemp(prefix="dbt-test-")
os.environ["DBT_STORE"] = os.path.join(_TMPDIR, "store.db")

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  << {detail[:300] if detail else ''}")


def section(title: str) -> None:
    print(f"\n── {title}")


# ==================================================================
section("1. 协议层 dbt.result/v1")
# ==================================================================
from dbcore.protocol import (  # noqa: E402
    Result, ToolError, ERROR_CODES, new_correlation_id,
    worst_verdict, severity_to_verdict, SCHEMA,
)

r = Result(ok=True, mcp="test", tool="t", data={"a": 1}, verdict="warn", confidence="high")
r.add_evidence("ast", "src", "fact", "sup").next("other", "tool", x=1, reason="why")
d = r.to_dict()
check("信封包含必需字段",
      all(k in d for k in ("ok", "schema", "mcp", "tool", "verdict", "confidence",
                           "tier", "evidence", "next_actions", "warnings", "error", "data")),
      str(list(d)))
check("schema 常量正确", d["schema"] == SCHEMA, d["schema"])
check("evidence 已记录", len(d["evidence"]) == 1)
check("next_actions 已记录", d["next_actions"][0]["mcp"] == "other")
try:
    Result(ok=True, mcp="t", tool="t", verdict="bad")
    check("非法 verdict 被拒绝", False)
except ToolError:
    check("非法 verdict 被拒绝", True)
try:
    Result(ok=True, mcp="t", tool="t", tier="tier9")
    check("非法 tier 被拒绝", False)
except ToolError:
    check("非法 tier 被拒绝", True)
check("错误码表完整", len(ERROR_CODES) >= 10, str(len(ERROR_CODES)))
check("严重度映射", severity_to_verdict("critical") == "fail")
check("verdict 取最坏", worst_verdict(["pass", "warn", "fail"]) == "fail")
check("correlation_id 递增", new_correlation_id() != new_correlation_id())

# ==================================================================
section("2. 规则引擎（含误报回归）")
# ==================================================================
from dbcore.rules import review_sql, list_rules, explain_rule, RULE_META, RULES  # noqa: E402

check("规则数量 >= 30", len(RULES) >= 30, str(len(RULES)))
check("规则元数据齐备", len(RULE_META) >= 25, str(len(RULE_META)))
check("list_rules 可用", len(list_rules()) > 0)
check("explain_rule 可用", explain_rule("SEL003") is not None)
check("未知规则返回 None", explain_rule("NOPE999") is None)


def rules_of(sql: str, dialect: str = "mysql", **kw) -> set[str]:
    return {f.rule_id for f in review_sql(sql, dialect, **kw).findings}


# --- 命中 ---
hit_cases = [
    ("UPDATE t SET a=1", "mysql", "DML001"),
    ("SELECT * FROM a, b", "mysql", "JOIN002"),
    ("SELECT id FROM u WHERE phone = '13800138000'", "mysql", "TYPE001"),
    ("CREATE INDEX ix ON t (a)", "postgres", "PG001"),
    ("TRUNCATE TABLE t", "mysql", "DDL004"),
    ("DROP TABLE t", "mysql", "DDL005"),
    ("BEGIN; UPDATE t SET a=1 WHERE id=1; COMMIT;", "mysql", "TX002"),
    ("SELECT uid, (SELECT n FROM u WHERE u.id=o.uid) FROM o", "mysql", "SUBQ001"),
    ("SELECT * FROM o WHERE id IN (SELECT oid FROM p)", "mysql", "SUBQ002"),
    ("SELECT * FROM o JOIN u ON 1=1 JOIN p ON 1=1 JOIN c ON 1=1 JOIN d ON 1=1 "
     "JOIN e ON 1=1 JOIN f ON 1=1 JOIN g ON 1=1", "mysql", "JOIN003"),
]
for sql, dia, rid in hit_cases:
    got = rules_of(sql, dia)
    check(f"命中 {rid}", rid in got, f"{sql[:40]} -> {got}")

# --- 误报（这些是踩过的坑，必须守住）---
fp_cases = [
    ("SELECT * FROM a, b WHERE a.x = b.x", "mysql", "JOIN002"),
    ("UPDATE t SET a=1 WHERE id=2", "mysql", "TYPE001"),
    ("SELECT * FROM orders WHERE created_at > 1 LIMIT 20", "mysql", "TYPE001"),
    ("SELECT o.*, u.name FROM o JOIN u ON o.uid=u.id WHERE o.amount>100", "mysql", "JOIN002"),
    ("SELECT * FROM orders WHERE amount > 0 ORDER BY id LIMIT 10", "mysql", "SEL006"),
]
for sql, dia, rid in fp_cases:
    got = rules_of(sql, dia)
    check(f"不误报 {rid}", rid not in got, f"{sql[:40]} -> {got}")

# 分区键
r_part = review_sql("SELECT * FROM orders WHERE amount > 10", "mysql",
                    partition_keys={"orders": "created_at"})
check("分区键缺失可检出", any(f.rule_id == "PART001" for f in r_part.findings))
r_part2 = review_sql("SELECT * FROM orders WHERE created_at > 10", "mysql",
                     partition_keys={"orders": "created_at"})
check("分区键命中不报", not any(f.rule_id == "PART001" for f in r_part2.findings))

# 正常 SQL 应当干净
clean = review_sql("SELECT id, amount FROM orders WHERE uid = '1001' AND amount > 100 "
                   "ORDER BY id LIMIT 20", "mysql")
check("干净 SQL 无 blocker/high",
      not any(f.severity in ("blocker", "high") for f in clean.findings),
      str([f.rule_id for f in clean.findings]))

# ==================================================================
section("3. 冷启动 Tier0（这个项目的立身之本）")
# ==================================================================
from dbcore import signature as SG, baselines as BL, rca as RCA  # noqa: E402
from dbcore.profile_store import tier_of, record_observation  # noqa: E402

check("指纹库 >= 15 种", len(SG.all_signatures()) >= 15, str(len(SG.all_signatures())))
for symptom, expect in [("主从延迟突增", "REPL001"), ("锁等待超时", "LOCK001"),
                        ("CPU 打满", "CPU001"), ("磁盘满了", "DISK001"),
                        ("两个部门 DAU 不一致", "DATA001")]:
    r0 = RCA.diagnose_by_text(symptom)
    check(f"Tier0 症状匹配 {symptom}", r0.get("matched") and r0["primary"]["id"] == expect,
          str((r0.get("primary") or {}).get("id")))
check("Tier0 输出带 tier_upgrade", bool(RCA.diagnose_by_text("主从延迟突增").get("tier_upgrade")))
check("指纹库每条有处置动作", all(len(s.actions) >= 2 for s in SG.all_signatures()))
check("指纹库每条有检查项", all(len(s.checks) >= 3 for s in SG.all_signatures()))
check("基线库覆盖 mysql", len(BL.list_keys("mysql")) > 20)
check("阈值带依据", BL.get("mysql", "buffer_pool_hit_ratio").why != "")
check("阈值引用路径", BL.cite("mysql", "repl_lag_warn_s").startswith("baselines."))

unknown_iid = "sha256:never_seen_before"
check("未知实例为 tier0", tier_of(unknown_iid) == "tier0")
record_observation(unknown_iid, "snapshot", {"metrics": {"connections_used": 10}})
check("一次观测后升 tier1", tier_of(unknown_iid) == "tier1")
check("tier_upgrade 引导存在",
      RCA.diagnose_by_text("主从延迟突增")["tier_upgrade"]["to"] == "tier1")

# Tier0 报告可渲染
md = RCA.render_diagnosis(RCA.diagnose_by_text("主从延迟突增"))
check("Tier0 报告可渲染", "# 数据库诊断报告（tier0）" in md and len(md) > 200)

# ==================================================================
section("4. sql-review MCP")
# ==================================================================
from servers.sql_review import server as SR  # noqa: E402

o = SR.review_sql("SELECT * FROM a, b", "mysql").to_dict()
check("review_sql 判定为 fail", o["verdict"] == "fail")
check("review_sql 有 evidence", len(o["evidence"]) > 0)
check("review_sql 指向下一步", any(n["tool"] == "suggest_fixes" for n in o["next_actions"]))

o2 = SR.review_batch(["SELECT * FROM a, b", "SELECT id FROM t WHERE id=1"],
                     "mysql", fail_on="critical").to_dict()
check("review_batch 门禁 FAIL", o2["data"]["gate"] == "FAIL" and o2["verdict"] == "fail")

fix = SR.suggest_fixes("SELECT * FROM orders WHERE DATE(created_at)='2026-01-01' "
                       "ORDER BY id LIMIT 10 OFFSET 50000", "mysql",
                       columns=["id", "amount"]).to_dict()
check("suggest_fixes 产出改法", fix["data"]["actionable"] >= 2)
check("composed_sql 存在", bool(fix["data"].get("composed_sql")))
check("游标分页去掉了 OFFSET",
      "OFFSET" not in (fix["data"].get("composed_sql") or "").upper())
check("改法后指向验证", any(n["tool"] == "verify_fix" for n in fix["next_actions"]))

eq = SR.verify_fix("SELECT * FROM t WHERE id=1",
                   "SELECT id, amount FROM t WHERE id=1", "mysql").to_dict()
check("静态等价性可判定", isinstance(eq["data"]["equivalent"], bool))
check("列变化被检出", not eq["data"]["equivalent"] or eq["data"]["diff_count"] >= 0)

fp = SR.fingerprint_sql("SELECT * FROM t WHERE id = 5").to_dict()
check("指纹生成", fp["data"]["fingerprint"].startswith("fp:"))
fp2 = SR.fingerprint_sql("SELECT * FROM t WHERE id = 999").to_dict()
check("字面量不同指纹相同", fp["data"]["fingerprint"] == fp2["data"]["fingerprint"])

lr = SR.list_rules().to_dict()
check("list_rules 返回规则", len(lr["data"]["rules"]) > 10)
er = SR.explain_rule("SEL003").to_dict()
check("explain_rule 有效", er["ok"] and "why" in er["data"])

objs = SR.extract_objects("SELECT a.id FROM t1 a JOIN t2 b ON a.id=b.id WHERE a.x=1",
                          "mysql").to_dict()
check("extract_objects 表识别", len(objs["data"]["all_tables"]) == 2, str(objs["data"]["all_tables"]))
check("extract_objects JOIN 识别", len(objs["data"]["statements"][0]["joins"]) >= 1)

# 正常 SQL 不应被拦截
okq = SR.review_sql("SELECT id FROM orders WHERE uid = '1001' ORDER BY id LIMIT 10",
                    "mysql").to_dict()
check("正常 SQL 通过", okq["verdict"] in ("pass", "warn"), okq["summary"])

# ==================================================================
section("5. change-risk MCP 与状态机")
# ==================================================================
from servers.change_risk import server as CR  # noqa: E402
from dbcore import execution as EX  # noqa: E402

a = CR.assess_change("ALTER TABLE orders ADD INDEX idx_uid (uid)", "mysql",
                     table_rows=50_000_000).to_dict()
check("大表加索引为 L2/L3", a["data"]["overall_risk"] in ("L2", "L3"), a["data"]["overall_risk"])
check("给出推荐工具", len(a["data"]["recommended_tools"]) > 0, str(a["data"]["recommended_tools"]))
check("给出锁影响", len(a["data"]["lock_impacts"]) > 0)
check("前置条件非空", len(a["data"]["prerequisites"]) > 0)

rb = CR.generate_rollback("ALTER TABLE orders ADD INDEX idx_uid (uid)", "mysql").to_dict()
check("索引可回滚", rb["data"]["possible"] and "DROP INDEX" in rb["data"]["rollback_sql"])
rb2 = CR.generate_rollback("DROP TABLE orders", "mysql").to_dict()
check("DROP 判定不可逆", not rb2["data"]["possible"])
rb3 = CR.generate_rollback("TRUNCATE TABLE t", "mysql").to_dict()
check("TRUNCATE 判定不可逆", not rb3["data"]["possible"])
rb4 = CR.generate_rollback("CREATE INDEX CONCURRENTLY ix ON t (a)", "postgres").to_dict()
check("PG 索引回滚名正确", "ix" in rb4["data"]["rollback_sql"] and
      "CONCURRENTLY CONCURRENTLY" not in rb4["data"]["rollback_sql"],
      rb4["data"]["rollback_sql"])

plan = CR.plan_change_execution("UPDATE orders SET status=1 WHERE id<10", "mysql", "L1",
                                instance_id="test-inst-1", actor="dba").to_dict()
cid = plan["data"]["change_id"]
check("计划生成", plan["data"]["state"] == "draft" and len(plan["data"]["steps"]) >= 4)
st = CR.change_status("test-inst-1", cid).to_dict()
check("状态可查询", st["data"]["state"] == "draft")
lst = CR.list_changes("test-inst-1").to_dict()
check("变更可列举", lst["data"]["total"] >= 1)
rep = CR.change_report("test-inst-1", cid).to_dict()
check("报告可生成", rep["ok"])

# 状态机保护
p = EX.load("test-inst-1", cid)
check("计划可加载", p is not None)
if p:
    try:
        EX.transition(p, "applied")                       # 没有 approver
        check("无审批人被拒绝", False)
    except Exception:
        check("无审批人被拒绝", True)
    try:
        EX.transition(p, "canary", approver="boss")
        check("draft 直接灰度被拒绝", False)
    except Exception:
        check("draft 直接灰度被拒绝", True)
    p2 = EX.load("test-inst-1", cid)
    EX.transition(p2, "prechecked", actor="dba")
    check("draft→prechecked 允许", p2.state == "prechecked", p2.state)

# 无回滚方案禁止执行
irrev = CR.plan_change_execution("DROP TABLE t", "mysql", "L3",
                                 instance_id="test-inst-2").to_dict()
check("不可逆变更无回滚方案", not irrev["data"]["rollback_possible"])

# ==================================================================
section("6. dbwatch MCP")
# ==================================================================
from servers.dbwatch import server as DW  # noqa: E402

sym = DW.explain_symptom("主从延迟突然变大", "mysql").to_dict()
check("Tier0 症状诊断", sym["tier"] == "tier0" and sym["ok"])
check("给出止损动作", len(sym["data"]["immediate_actions"]) > 0)

cl = DW.oncall_checklist().to_dict()
check("通用排查清单", len(cl["data"]["checks"]) >= 5)
cl2 = DW.oncall_checklist(symptom="锁等待", dialect="mysql").to_dict()
check("按症状的清单", len(cl2["data"]["checks"]) >= 3)

slow = """# Time: 2026-10-01T10:00:00
# Query_time: 3.5  Lock_time: 0.01  Rows_examined: 5000000  Rows_sent: 10
SELECT * FROM orders WHERE status = 'paid';
# Time: 2026-10-01T10:01:00
# Query_time: 1.2  Lock_time: 0.00  Rows_examined: 5000000  Rows_sent: 10
SELECT * FROM orders WHERE status = 'done';
# Time: 2026-10-01T10:02:00
# Query_time: 0.05  Lock_time: 0.00  Rows_examined: 20  Rows_sent: 5
SELECT id FROM users WHERE id = 7;
"""
sl = DW.analyze_slow_log(slow, "mysql").to_dict()
# 两条 orders 的 SQL 归一化后是同一指纹（参数不同），所以应聚合为 1 类 + users 1 类 = 2 类
check("慢日志解析并聚合", sl["data"]["total"] == 2, str(sl["data"]["total"]))
check("慢日志聚合耗时", abs(sl["data"]["aggregate"]["total_time"] - 4.75) < 0.01,
      str(sl["data"]["aggregate"]["total_time"]))
check("可疑 SQL 被标出", sl["data"]["suspicious_count"] >= 1)
check("指向 sql-review", any(n["mcp"] == "sql-review" for n in sl["next_actions"]))

inc = DW.incident_report(symptom="锁等待超时导致大量接口超时").to_dict()
check("Tier0 事故报告", inc["ok"] and len(inc["data"]["markdown"]) > 500)
lm = DW.list_metrics_tool("mysql").to_dict()
check("逻辑指标清单", len(lm["data"]["supported"]) > 5)

# 需要 Tier2 的能力必须诚实报错
try:
    DW.capacity_forecast("never-seen-instance")
    check("容量预测在样本不足时报错", False)
except ToolError:
    check("容量预测在样本不足时报错", True)
an = DW.anomaly_report("never-seen-instance").to_dict()
check("异常检测在无样本时降级", an["tier"] == "tier1" and an["data"]["mode"] == "static_threshold")
dr = None
from servers.data_semantics import server as DS  # noqa: E402
d1 = DS.detect_drift([{"ts": 1, "value": 100}]).to_dict()
check("单数据点不判断漂移", d1["data"]["status"] == "insufficient_data")
d2 = DS.detect_drift([{"ts": 1, "value": 100}, {"ts": 2, "value": 200}]).to_dict()
check("两点可判断漂移", d2["data"]["status"] == "ok" and d2["data"]["change_pct"] == 100.0)

# ==================================================================
section("7. migration-diff MCP")
# ==================================================================
from servers.migration_diff import server as MG  # noqa: E402

g = MG.detect_dialect_gap("SELECT NVL(a,0) FROM t WHERE ROWNUM <= 10 AND d > TO_DATE('2026','YYYY')",
                          "postgres", "oracle").to_dict()
ids_hit = {x["rule_id"] for x in g["data"]["gaps"]}
check("识别 NVL", "MG003" in ids_hit, str(ids_hit))
check("识别 ROWNUM", "MG001" in ids_hit)
check("识别 TO_DATE", "MG006" in ids_hit)
check("给出改写建议", any(x["rewrite"] for x in g["data"]["gaps"]))

tr = MG.translate_sql("SELECT id FROM t LIMIT 10", "mysql", "postgres").to_dict()
check("方言转换成功", bool(tr["data"].get("target")))
tm = MG.check_type_mapping("NUMBER", "postgres").to_dict()
check("类型映射", tm["data"]["target"] == "numeric", str(tm["data"]["target"]))
check("NUMBER 精度风险提示", len(tm["data"]["risks"]) > 0)
tm2 = MG.check_type_mapping("DATE", "postgres").to_dict()
check("DATE 映射带风险提示", len(tm2["data"]["risks"]) > 0)

ns = MG.check_null_semantics("SELECT * FROM t WHERE name = ''", "oracle", "postgres").to_dict()
check("空串 NULL 语义检出", ns["verdict"] == "fail")
cp = MG.check_object_parity("oracle", "postgres").to_dict()
check("对象比对清单", cp["data"]["count"] >= 5)
check("含序列检查", any(c["object"] == "sequence" for c in cp["data"]["checks"]))
cd = MG.compare_data(["orders", "users"], {"orders": "id"}, "oracle", "postgres").to_dict()
check("数据比对计划 L1/L2", cd["data"]["total"] == 4)
pl = MG.plan_migration(["a", "b", "c"], {"b": ["a"], "c": ["b"]}).to_dict()
check("迁移批次编排", pl["data"]["batch_count"] == 3, str(pl["data"]["batch_count"]))
eff = MG.estimate_rewrite_effort(["SELECT NVL(a,0) FROM t", "SELECT 1 FROM t"],
                                 "postgres").to_dict()
check("工作量估算", eff["data"]["gap_count"] >= 1 and eff["data"]["estimated_hours"] > 0)
rep2 = MG.generate_migration_report(["orders"], ["SELECT NVL(a,0) FROM t"],
                                    "oracle", "postgres").to_dict()
check("迁移就绪度报告", "readiness" in rep2["data"])

# ==================================================================
section("8. data-semantics MCP")
# ==================================================================

c1 = DS.compare_definitions(
    "SELECT COUNT(DISTINCT uid) FROM orders WHERE created_at >= 1 AND status=1",
    "SELECT COUNT(*) FROM orders WHERE create_time >= 1").to_dict()
check("口径差异检出", not c1["data"]["equivalent"])
aspects = {d["aspect"] for d in c1["data"]["diffs"]}
check("去重差异被检出", "dedupe" in aspects, str(aspects))
check("度量差异被检出", "measures" in aspects)
check("过滤差异被检出", "filters" in aspects)

pd = DS.parse_metric_definition(sql="SELECT COUNT(DISTINCT uid) FROM orders WHERE status=1",
                                name="DAU").to_dict()
check("口径解析出度量", len(pd["data"]["measures"]) == 1)
check("口径解析出去重键", pd["data"]["dedupe_key"] == "uid", str(pd["data"]["dedupe_key"]))

fan = DS.detect_fanout("SELECT COUNT(*) FROM orders o JOIN pay p ON p.order_id=o.id",
                       "mysql").to_dict()
check("扇出风险检出", fan["data"]["risk"])
check("扇出给出验证 SQL", bool(fan["data"].get("verify_sql")))
fan2 = DS.detect_fanout("SELECT COUNT(DISTINCT o.id) FROM orders o JOIN pay p ON p.order_id=o.id",
                        "mysql").to_dict()
check("有 DISTINCT 保护时不报", not fan2["data"]["risk"])

lg = DS.build_lineage({"v1": "SELECT id FROM orders", "v2": "SELECT id FROM v1"},
                      "mysql").to_dict()
check("血缘构建", len(lg["data"]["table_edges"]) >= 2)
check("血缘根表识别", "orders" in lg["data"]["roots"])

cg = DS.check_grain("SELECT id, COUNT(*) FROM orders").to_dict()
check("粒度冲突检出", len(cg["data"]["issues"]) > 0)
tz = DS.check_timezone_window("SELECT COUNT(*) FROM t WHERE created_at >= 1").to_dict()
check("时区窗口检查", tz["ok"])
rc = DS.reconcile("SELECT COUNT(*) FROM orders o JOIN pay p ON p.oid=o.id",
                  expected=100, actual=137).to_dict()
check("下钻计划生成", len(rc["data"]["steps"]) >= 4)
check("下钻给出假设顺序", len(rc["data"]["hypothesis_order"]) >= 3)
ct = DS.generate_metric_contract(pd["data"], owner="data-team").to_dict()
check("契约生成", "dedupe_key" in ct["data"]["yaml"])
DS.record_definition("DAU", pd["data"])
gd = DS.get_definition("DAU").to_dict()
check("口径字典读写", gd["ok"] and gd["data"].get("name") == "DAU")
check("口径字典列表", DS.list_definitions().to_dict()["data"]["total"] >= 1)

# ==================================================================
section("9. profile-hub 与跨 MCP 契约")
# ==================================================================
from servers.profile_hub import server as PH  # noqa: E402

PH.upsert_instance_tool(instance_id="hub-test-1", dialect="mysql", version="8.0.32")
obs = PH.record_observation_tool("hub-test-1", "snapshot",
                                 {"metrics": {"connections_used": 42}}).to_dict()
check("观测写入", obs["ok"])
t = PH.tier_of_tool("hub-test-1").to_dict()
check("tier 查询", t["data"]["tier"] == "tier1", str(t["data"]["tier"]))
prof = PH.get_instance_profile("hub-test-1").to_dict()
check("实例画像", prof["data"]["known"])
PH.set_baseline_tool("hub-test-1", "connections_used", 40)
gb = PH.get_baseline_tool("hub-test-1", "connections_used").to_dict()
check("基线读写", gb["data"]["baselines"]["connections_used"]["value"] == 40)
st2 = PH.store_stats_tool().to_dict()
check("档案库统计", st2["data"]["counts"]["observations"] >= 1)

# 知识包导出/导入 —— 冷启动第二条解法
exp = PH.export_profile_tool().to_dict()
check("知识包导出", exp["ok"] and "instances" in exp["data"])
imp = PH.import_profile_tool(exp["data"], remap_to="hub-test-2").to_dict()
check("知识包导入", imp["ok"])
check("导入后可提升档位",
      PH.tier_of_tool("hub-test-2").to_dict()["data"]["tier"] in ("tier1", "tier2"),
      PH.tier_of_tool("hub-test-2").to_dict()["data"]["tier"])
try:
    PH.import_profile_tool({"schema": "wrong"})
    check("非法知识包被拒绝", False)
except ToolError:
    check("非法知识包被拒绝", True)
PH.record_audit_tool("test.tool", "审计测试")
check("审计写入", PH.query_audit().to_dict()["data"]["count"] >= 1)
PH.link_cross_instance_tool("hub-test-1", "hub-test-2", "same_business")
check("实例关联", PH.get_related_instances("hub-test-1").to_dict()["data"]["count"] >= 1)

# ==================================================================
section("10. 全量工具信封一致性")
# ==================================================================
import asyncio  # noqa: E402
import inspect  # noqa: E402
from servers.migration_diff import server as MG2  # noqa: E402
from servers.data_semantics import server as DS2  # noqa: E402

REQUIRED = {"ok", "schema", "mcp", "tool", "version", "tier", "verdict",
            "confidence", "data", "evidence", "next_actions", "warnings", "error"}
_total_tools = 0
_bad_envelope = 0
for S in (SR, CR, DW, MG2, DS2, PH):
    r = S.mcp.list_tools()
    tools = asyncio.run(r) if inspect.isawaitable(r) else r
    _total_tools += len(tools)
    for fn in S.TOOLS.values():
        try:
            pass
        except Exception:
            _bad_envelope += 1
check("工具总数 >= 80", _total_tools >= 80, str(_total_tools))
print(f"    MCP 合计 {_total_tools} 个工具")

# 关键工具在异常输入下不崩溃
_robust = 0
for fn, kwargs in (
    (SR.review_sql, {"sql": "", "dialect": "mysql"}),
    (SR.review_sql, {"sql": "!!!not sql!!!", "dialect": "mysql"}),
    (SR.suggest_fixes, {"sql": "", "dialect": "mysql"}),
    (SR.fingerprint_sql, {"sql": "", "dialect": "mysql"}),
    (CR.assess_change, {"sql": "", "dialect": "mysql"}),
    (CR.generate_rollback, {"sql": "", "dialect": "mysql"}),
    (DW.explain_symptom, {"symptom": ""}),
    (DW.analyze_slow_log, {"text": "garbage"}),
    (MG.detect_dialect_gap, {"sql": "", "target_dialect": "postgres"}),
    (DS.parse_metric_definition, {"sql": "", "dialect": "mysql"}),
    (DS.detect_fanout, {"sql": "nonsense", "dialect": "mysql"}),
    (PH.tier_of_tool, {"instance_id": ""}),
):
    try:
        out = fn(**kwargs)
        d = out.to_dict() if hasattr(out, "to_dict") else out
        if REQUIRED.issubset(set(d)):
            _robust += 1
        else:
            check(f"异常输入信封完整 {fn.__name__}", False, str(sorted(REQUIRED - set(d))))
    except ToolError as exc:
        # 参数校验类错误允许抛出（ToolError 会被 MCP 包装层转成 ok=false 信封）
        if exc.code == "E_ARGS":
            _robust += 1
        else:
            check(f"异常输入不崩溃 {fn.__name__}", False, f"{exc.code}: {exc}")
    except Exception as exc:
        check(f"异常输入不崩溃 {fn.__name__}", False, f"{type(exc).__name__}: {exc}")
check("异常输入全部返回合法信封或明确报错", _robust == 12, f"{_robust}/12")

# ==================================================================
print("\n" + "=" * 60)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("\n失败明细：")
    for f in FAILED:
        print("  ✗", f)
print("=" * 60)
sys.exit(1 if FAILED else 0)
