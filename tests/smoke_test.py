"""冒烟测试：不连真实数据库，覆盖静态体检 / 风险矩阵 / 回滚生成 / 只读护栏。

运行： PYTHONPATH=packages python tests/smoke_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))

from dbcore.guard import GuardError, assert_readonly  # noqa: E402
from dbcore.profile_store import get_history, instance_summary, record_observation  # noqa: E402
from dbcore.risk import assess_change, generate_rollback  # noqa: E402
from dbcore.rules import review_sql  # noqa: E402

tmpdir = tempfile.mkdtemp()
import os  # noqa: E402

os.environ["DBT_STORE"] = os.path.join(tmpdir, "smoke.db")

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + detail) if detail and not cond else ''}")
    if not cond:
        FAILED.append(name)


print("== 1. 静态体检 ==")
bad = "UPDATE user SET name='x'"
r = review_sql(bad, "mysql")
check("UPDATE 无 WHERE 判定 blocker", r.max_severity == "blocker", str(r.to_dict()["findings"]))
check("verdict 为禁止上线", r.verdict == "禁止上线", r.verdict)

r2 = review_sql("select * from orders where substr(order_no,1,3)='A1' and user_name like '%abc%' limit 50000,10", "mysql")
rules_hit = {f.rule_id for f in r2.findings}
check("SELECT * 命中 SEL001", "SEL001" in rules_hit, str(rules_hit))
check("字段上加函数命中 SEL003", "SEL003" in rules_hit, str(rules_hit))
check("前置通配 LIKE 命中 SEL002", "SEL002" in rules_hit, str(rules_hit))
check("深分页命中 SEL005", "SEL005" in rules_hit, str(rules_hit))

r3 = review_sql("SELECT a.id FROM a JOIN b ON a.id=b.id ORDER BY rand() LIMIT 5", "mysql")
check("JOIN 有 ON 不报错", all(f.rule_id != "SEL007" for f in r3.findings))
check("ORDER BY RAND 命中 SEL004", "SEL004" in {f.rule_id for f in r3.findings})

r4 = review_sql("SELECT id FROM a JOIN b", "mysql")
check("笛卡尔积判定 high", any(f.rule_id == "SEL007" and f.severity == "high" for f in r4.findings))

r5 = review_sql("SELECT id, name FROM t WHERE deleted=0 ORDER BY id LIMIT 20", "mysql")
check("健康 SQL 无 blocker/high", r5.max_severity in (None, "low", "info"), str(r5.max_severity))

print("== 2. 变更风险矩阵 ==")
p1 = assess_change("ALTER TABLE orders ADD COLUMN remark varchar(64)", "mysql", table_rows=500_000)
check("加列(小表) 风险<=L1", p1.overall_risk in ("L0", "L1"), p1.overall_risk)
p2 = assess_change("ALTER TABLE orders ADD INDEX idx_uid (uid)", "mysql", table_rows=50_000_000)
check("大表加索引升到 L2/L3", p2.overall_risk in ("L2", "L3"), p2.overall_risk)
check("大表加索引推荐旁路工具", "gh-ost" in (p2.ops[0].recommended_tool or ""), str(p2.ops[0].to_dict()))
p3 = assess_change("alter table orders modify column amount decimal(20,2)", "mysql", table_rows=100)
check("改字段类型 L3", p3.overall_risk == "L3", p3.overall_risk)
p4 = assess_change("CREATE INDEX idx_email ON users (email)", "postgres")
check("PG 建索引推荐 CONCURRENTLY", "CONCURRENTLY" in p4.ops[0].recommended_tool, str(p4.ops[0].to_dict()))
p5 = assess_change("TRUNCATE TABLE logs", "mysql")
check("TRUNCATE 判为 L3 且不可回滚", p5.overall_risk == "L3" and "不可逆" in p5.ops[0].rollback, str(p5.ops[0].to_dict()))
p6 = assess_change("ALTER TABLE t ADD COLUMN c int", "mysql", table_rows=100, during_peak=True)
check("高峰期附加提示", any("高峰" in n for n in p6.ops[0].notes), str(p6.ops[0].notes))

print("== 3. 回滚生成 ==")
check("ADD INDEX 回滚为 DROP INDEX", "DROP INDEX" in generate_rollback("ALTER TABLE t ADD INDEX idx_a (a)", "mysql"))
check("ADD COLUMN 回滚为 DROP COLUMN", "DROP COLUMN" in generate_rollback("ALTER TABLE t ADD COLUMN c int", "postgres"))
check("TRUNCATE 明确不可逆", "不可逆" in generate_rollback("TRUNCATE TABLE t", "mysql"))
check("RENAME 双向", "RENAME" in generate_rollback("RENAME TABLE a TO b", "mysql").upper())

print("== 4. 只读护栏 ==")
for sql, should_pass in [
    ("SELECT id FROM t", True),
    ("EXPLAIN SELECT id FROM t", True),
    ("SHOW PROCESSLIST", True),
    ("DELETE FROM t WHERE id=1", False),
    ("SELECT 1; DROP TABLE t", False),
    ("ALTER TABLE t ADD COLUMN c int", False),
]:
    try:
        assert_readonly(sql, "mysql")
        ok = True
    except GuardError:
        ok = False
    check(f"护栏 {'放行' if should_pass else '拦截'}: {sql[:34]}", ok == should_pass)

print("== 5. 共享实例档案 ==")
iid = "mysql://127.0.0.1:3306/shop"
record_observation(iid, "sql_review", {"verdict": "禁止上线"})
record_observation(iid, "sql_review", {"verdict": "可上线"})
record_observation(iid, "change_risk", {"overall_risk": "L3"})
check("档案累计 3 条观测", instance_summary(iid)["total_observations"] == 3)
check("按 kind 过滤", len(get_history(iid, "sql_review", limit=10)) == 2)

print("== 6. 修正引擎（给解决办法，不只是提问题） ==")
from dbcore.advisor import plan_index  # noqa: E402
from dbcore.fix import suggest_fixes  # noqa: E402

f1 = suggest_fixes("SELECT * FROM orders WHERE DATE(created_at) = '2026-01-01'", "mysql")
date_fix = next(i for i in f1["items"] if i["rule_id"] == "SEL003")
check("DATE() 改写为范围查询", date_fix["fixed_sql"] is not None and ">= '2026-01-01'" in date_fix["fixed_sql"], str(date_fix))
check("改写后范围右开到次日", "< '2026-01-02'" in (date_fix["fixed_sql"] or ""), str(date_fix["fixed_sql"]))
check("修正带闭环保验证且已解决", (date_fix.get("recheck") or {}).get("rule_resolved") is True, str(date_fix.get("recheck")))

f2 = suggest_fixes("SELECT id FROM orders WHERE YEAR(created_at) = 2026", "mysql")
year_fix = next((i for i in f2["items"] if i["rule_id"] == "SEL003"), None)
check("YEAR() 改写为全年范围", year_fix and "'2027-01-01'" in (year_fix["fixed_sql"] or ""), str(year_fix))

f3 = suggest_fixes("SELECT id FROM orders WHERE SUBSTR(order_no,1,3)='A12'", "mysql")
sub_fix = next(i for i in f3["items"] if i["rule_id"] == "SEL003")
check("SUBSTR 改写为 LIKE 前缀", sub_fix["fixed_sql"] and "LIKE 'A12%'" in sub_fix["fixed_sql"], str(sub_fix["fixed_sql"]))

f4 = suggest_fixes("SELECT id,name FROM user WHERE id > 0 ORDER BY id LIMIT 10 OFFSET 50000", "mysql")
off_fix = next(i for i in f4["items"] if i["rule_id"] == "SEL005")
check("深分页改写为游标分页", off_fix["fixed_sql"] and ":last_val" in off_fix["fixed_sql"], str(off_fix["fixed_sql"]))

f5 = suggest_fixes("UPDATE user SET name='x'", "mysql")
dml_fix = next(i for i in f5["items"] if i["rule_id"] == "DML001")
check("无 WHERE 的 DML 给出 COUNT 兜底", any("COUNT(*)" in s for s in dml_fix["follow_up_sql"]), str(dml_fix))
f5b = suggest_fixes("UPDATE user SET name='x'", "mysql", predicate="id < 100")
dml_fix_b = next(i for i in f5b["items"] if i["rule_id"] == "DML001")
check("传入 predicate 可直接修复 DML", dml_fix_b["fixed_sql"] and "WHERE id < 100" in dml_fix_b["fixed_sql"], str(dml_fix_b["fixed_sql"]))

f6 = suggest_fixes("SELECT * FROM orders", "mysql")
star_fix = next(i for i in f6["items"] if i["rule_id"] == "SEL001")
check("缺 schema 时 SELECT * 给查字段清单 SQL", star_fix["sql_kind"] == "needs_metadata" and bool(star_fix["follow_up_sql"]))
f6b = suggest_fixes("SELECT * FROM orders", "mysql", columns=["id", "amount"])
star_fix_b = next(i for i in f6b["items"] if i["rule_id"] == "SEL001")
check("给字段清单后可自动展开", star_fix_b["fixed_sql"] == "SELECT id, amount FROM orders", str(star_fix_b["fixed_sql"]))

f7 = suggest_fixes("INSERT INTO user VALUES (1,'a')", "mysql", columns=["id", "name"])
ins_fix = next(i for i in f7["items"] if i["rule_id"] == "INS001")
check("INSERT 补字段名", ins_fix["fixed_sql"] and "id, name" in ins_fix["fixed_sql"], str(ins_fix["fixed_sql"]))

f8 = suggest_fixes("SELECT id FROM t WHERE a=1 OR b=2", "mysql")
or_fix = next(i for i in f8["items"] if i["rule_id"] == "SEL008")
check("OR 拆 UNION ALL 且标注去重风险", or_fix["fixed_sql"] and "UNION ALL" in or_fix["fixed_sql"] and any("去重" in c for c in or_fix["caveats"]), str(or_fix))

f9 = suggest_fixes("SELECT * FROM orders WHERE user_name LIKE '%abc'", "mysql")
like_fix = next(i for i in f9["items"] if i["rule_id"] == "SEL002")
check("后缀匹配给倒序索引方案", like_fix["fixed_sql"] and like_fix["side_ddl"], str(like_fix))

print("== 8. 修正叠加（多条问题一次改完） ==")
multi = suggest_fixes(
    "SELECT * FROM orders WHERE DATE(created_at)='2026-01-01' AND uid>0 ORDER BY id LIMIT 10 OFFSET 50000",
    "mysql", columns=["id", "amount"])
kinds = {i["rule_id"]: i for i in multi["items"]}
check("多条修正同时产出", {"SEL001", "SEL003", "SEL005"} <= set(kinds), str(list(kinds)))
check("composed_sql 存在", bool(multi["composed_sql"]), str(multi.get("composed_sql")))
check("叠加后 DATE() 已消失", "DATE(" not in (multi["composed_sql"] or ""))
check("叠加后不再有 SELECT *", "SELECT *" not in (multi["composed_sql"] or ""))
check("叠加后仍是游标分页", ":last_val" in (multi["composed_sql"] or ""))
check("游标分页已去掉 OFFSET", "OFFSET" not in (multi["composed_sql"] or "").upper(), str(multi["composed_sql"]))
check("剩余问题数下降", set(multi["remaining_all"]) <= {"SEL005"}, str(multi["remaining_all"]))
check("已解决清单正确", {"SEL001", "SEL003"} <= set(multi["resolved_all"]), str(multi["resolved_all"]))

deep_no_col = suggest_fixes("SELECT id FROM t WHERE a=1 ORDER BY RAND() LIMIT 5 OFFSET 90000", "mysql")
off2 = next(i for i in deep_no_col["items"] if i["rule_id"] == "SEL005")
check("ORDER BY 表达式时不拿它当游标", off2["sql_kind"] == "needs_metadata", str(off2))
deep_with_col = suggest_fixes("SELECT id FROM t WHERE a=1 ORDER BY RAND() LIMIT 5 OFFSET 90000", "mysql", cursor_column="id")
off3 = next(i for i in deep_with_col["items"] if i["rule_id"] == "SEL005")
check("指定 cursor_column 后可改写", off3["fixed_sql"] and ":last_val" in off3["fixed_sql"], str(off3.get("fixed_sql")))

rand_case = suggest_fixes("SELECT id FROM orders WHERE amount>0 ORDER BY RAND() LIMIT 10", "mysql")
rand_fix = next(i for i in rand_case["items"] if i["rule_id"] == "SEL004")
check("RAND 排序给出主键取样方案", rand_fix["fixed_sql"] and "RAND()" not in (rand_fix["fixed_sql"].split("LIMIT")[0].replace("CEIL(RAND()", "")), str(rand_fix)[:160])

print("== 7. 索引建议决策逻辑 ==")
existing = [{"name": "idx_a", "columns": ["a"], "unique": False}, {"name": "idx_ab", "columns": ["a", "b"], "unique": False}]
r = plan_index(existing, ["a", "b"], "orders")
check("重复索引被劝退", r["should_create"] is False and r["redundant_with"] == "idx_ab", str(r))
r2 = plan_index([{"name": "idx_abc", "columns": ["a", "b", "c"], "unique": False}], ["a", "b"], "orders")
check("前缀已覆盖被劝退", r2["should_create"] is False and r2["covered_by_prefix"] == "idx_abc", str(r2))
r2b = plan_index([{"name": "idx_qab", "columns": ["q", "a", "b"], "unique": False}], ["a", "b"], "orders")
check("首位字段不同不算覆盖", r2b["should_create"] is True, str(r2b))
r3 = plan_index(existing, ["status", "created_at"], "orders", order_column="id")
check("排序字段放最后", r3["proposed_columns"][-1] == "id", str(r3))
check("PG 用 CONCURRENTLY", "CONCURRENTLY" in plan_index(existing, ["k"], "orders", dialect="postgres")["ddl"])

print()
if FAILED:
    print(f"FAILED: {len(FAILED)} -> {FAILED}")
    sys.exit(1)
print("ALL PASSED")
