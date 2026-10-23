#!/usr/bin/env python3
"""真实数据库连通性验证：端到端打真实库，不只是 sqlglot 静态解析。

默认用 SQLite（标准库、零基础设施、即真实 ACID 引擎）验证连接器公共路径：
  - 连接串解析（绝对/相对/:memory:）
  - run_readonly：真连库、真 SELECT、结果归一化、行数截断
  - explain：真取执行计划（SQLite 走 EXPLAIN QUERY PLAN，能识别全表扫描 SCAN）
  - run_in_transaction 的 dry-run 真回滚：断言数据未被改动（不是"声称没改"）
  - run_in_transaction 的 commit：断言数据真的落盘
  - 只读护栏在真实库上拦截写操作 / DDL
  - precheck_change_window：SQLite 明确跳过（不假装检查通过）

若提供真实 MySQL / Postgres，额外验证厂商专属分支（变更窗口、EXPLAIN 格式）：
    DBT_TEST_MYSQL_URL=mysql://user:pass@host:3306/db \
    DBT_TEST_POSTGRES_URL=postgres://user:pass@host:5432/db \
    python tests/test_real_db.py

不依赖 pytest。直接运行：python tests/test_real_db.py
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "sqltk-core" / "src"))

os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")

from dbcore.connectors import (  # noqa: E402
    ConnectionSpec,
    explain,
    precheck_change_window,
    run_in_transaction,
    run_readonly,
)
from dbcore.guard import GuardError  # noqa: E402
from dbcore.protocol import ToolError  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  << {detail[:300] if detail else ''}")


def section(title: str) -> None:
    print(f"\n── {title}")


def skip(name: str, reason: str) -> None:
    SKIPPED.append(f"{name}  ({reason})")
    print(f"  ⊘ SKIP {name} —— {reason}")


# ==================================================================
# 准备一个真实的 SQLite 文件库：建表 + 建索引 + 灌数据
# ==================================================================
_tmp = tempfile.mktemp(suffix=".db")
con = sqlite3.connect(_tmp)
con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT, val INTEGER)")
con.execute("CREATE INDEX ix_t_name ON t(name)")  # name 上有索引
con.executemany(
    "INSERT INTO t(id, name, val) VALUES(?,?,?)",
    [(i, f"n{i}", i * 10) for i in range(1, 11)],  # val 上无索引，用来触发全表扫描
)
con.commit()
con.close()

# 连接串用 posix 形式，确保 Windows 上也能被 sqlite3.connect 正确解析
DB_URL = "sqlite:///" + pathlib.Path(_tmp).as_posix()


# ==================================================================
section("A. 连接串解析（sqlite）")
# ==================================================================
_s1 = ConnectionSpec("sqlite://:memory:")
check("':memory:' 解析为 sqlite 驱动", _s1.driver == "sqlite")
check("':memory:' 解析为内存库", _s1.database == ":memory:")

_s2 = ConnectionSpec("sqlite://rel.db")
check("相对路径解析为 sqlite", _s2.driver == "sqlite" and _s2.database == "rel.db")

_s3 = ConnectionSpec(DB_URL)
check("绝对路径 URL 解析为 sqlite 驱动", _s3.driver == "sqlite")
check("绝对路径 URL 库名正确", _s3.database == pathlib.Path(_tmp).as_posix())
check("sqlite instance_id 不带主机端口", _s3.instance_id == f"sqlite://{_s3.database}")

try:
    ConnectionSpec("not-a-url")
    check("非法连接串被拒", False)
except ValueError:
    check("非法连接串被拒", True)


# ==================================================================
section("B. run_readonly：真连库 + 真 SELECT + 结果归一化")
# ==================================================================
r = run_readonly(DB_URL, "SELECT id, name, val FROM t WHERE id <= 3 ORDER BY id")
check("返回列名正确", r["columns"] == ["id", "name", "val"], str(r["columns"]))
check("返回 3 行", len(r["rows"]) == 3, str(len(r["rows"])))
check("首行值正确", list(r["rows"][0]) == [1, "n1", 10], str(r["rows"][0]))
check("整数列未被强转成字符串", isinstance(r["rows"][0][0], int), str(type(r["rows"][0][0])))
check("instance_id 透传", r["instance_id"] == _s3.instance_id)
check("未截断时 truncated=False", r["truncated"] is False)

# 行数截断：10 行里只取 2 行
r2 = run_readonly(DB_URL, "SELECT id FROM t ORDER BY id", max_rows=2)
check("max_rows 截断生效", len(r2["rows"]) == 2, str(len(r2["rows"])))
check("截断标记为真", r2["truncated"] is True)


# ==================================================================
section("C. explain：真取执行计划（SEARCH vs 全表 SCAN）")
# ==================================================================
e_idx = explain(DB_URL, "SELECT * FROM t WHERE id = 1")  # 主键 -> SEARCH
check("EXPLAIN 返回 plan_text", bool(e_idx.get("plan_text")), str(e_idx.get("plan_text")))
check("主键查询走索引（无全表扫描告警）", "scan" not in e_idx["plan_text"].lower(), e_idx["plan_text"])

e_scan = explain(DB_URL, "SELECT * FROM t WHERE val = 50")  # val 无索引 -> SCAN
check("无索引列查询被识别为全表扫描", any("全表扫描" in i for i in e_scan["issues"]), str(e_scan["issues"]))


# ==================================================================
section("D. run_in_transaction dry-run：真回滚（数据未被改动）")
# ==================================================================
before = run_readonly(DB_URL, "SELECT val FROM t WHERE id = 1")["rows"][0][0]
check("回滚前 val=10", before == 10, str(before))

dry = run_in_transaction(
    DB_URL,
    ["UPDATE t SET val = 999 WHERE id = 1"],
    dialect="sqlite",
    commit=False,
)
check("dry-run 标记 rolled_back=True", dry["rolled_back"] is True, str(dry))
check("dry-run 标记 committed=False", dry["committed"] is False)
check("dry-run 单条执行 ok", dry["results"][0]["ok"] is True)
check("dry-run 报告影响行数=1", dry["results"][0]["affected_rows"] == 1, str(dry["results"][0]))

after = run_readonly(DB_URL, "SELECT val FROM t WHERE id = 1")["rows"][0][0]
check("回滚后数据未被改动（真回滚，非声称）", after == 10, f"期望 10，实际 {after}")


# ==================================================================
section("E. run_in_transaction commit：数据真的落盘")
# ==================================================================
committed = run_in_transaction(
    DB_URL,
    ["UPDATE t SET val = 42 WHERE id = 2"],
    dialect="sqlite",
    commit=True,
)
check("commit 标记 committed=True", committed["committed"] is True, str(committed))
check("commit 标记 rolled_back=False", committed["rolled_back"] is False)

persisted = run_readonly(DB_URL, "SELECT val FROM t WHERE id = 2")["rows"][0][0]
check("提交后数据真正落盘", persisted == 42, f"期望 42，实际 {persisted}")


# ==================================================================
section("F. 只读护栏在真实库上拦截写操作 / DDL")
# ==================================================================
try:
    run_readonly(DB_URL, "UPDATE t SET val = 1 WHERE id = 1")
    check("真实库上 UPDATE 被只读护栏拦截", False, "未抛错")
except (GuardError, ToolError):
    check("真实库上 UPDATE 被只读护栏拦截", True)

try:
    run_readonly(DB_URL, "CREATE TABLE should_not_exist(a INT)")
    check("真实库上 DDL 被只读护栏拦截", False, "未抛错")
except (GuardError, ToolError):
    check("真实库上 DDL 被只读护栏拦截", True)


# ==================================================================
section("G. precheck_change_window：SQLite 明确跳过")
# ==================================================================
pc = precheck_change_window(DB_URL, dialect="sqlite")
check("返回 go 字段", "go" in pc, str(pc))
check("SQLite 返回 go=True（跳过而非假装）", pc["go"] is True)
check("SQLite 检查项状态为 unknown", pc["checks"][0]["status"] == "unknown", str(pc["checks"]))
check("SQLite 明确说明不适用", "跳过" in pc["verdict"], pc["verdict"])


# ==================================================================
section("H. 真实 MySQL / Postgres 分支（可选，需提供连接串）")
# ==================================================================
_mysql_url = os.environ.get("DBT_TEST_MYSQL_URL")
_pg_url = os.environ.get("DBT_TEST_POSTGRES_URL")

if _mysql_url:
    try:
        pe = explain(_mysql_url, "SELECT 1", dialect="mysql")
        check("MySQL EXPLAIN 返回 plan_text", bool(pe.get("plan_text")))
        pcm = precheck_change_window(_mysql_url, dialect="mysql")
        check("MySQL 变更窗口返回 checks 结构", isinstance(pcm.get("checks"), list) and pcm["checks"])
        # 只读验证，不在用户真实库上做写操作
    except Exception as exc:  # noqa: BLE001  # 连接/驱动不可用即跳过
        skip("MySQL 分支", f"连接/驱动不可用：{type(exc).__name__}: {exc}")
else:
    skip("MySQL 分支", "未设置 DBT_TEST_MYSQL_URL")

if _pg_url:
    try:
        pe = explain(_pg_url, "SELECT 1", dialect="postgres")
        check("Postgres EXPLAIN 返回 plan_text", bool(pe.get("plan_text")))
        pcp = precheck_change_window(_pg_url, dialect="postgres")
        check("Postgres 变更窗口返回 checks 结构", isinstance(pcp.get("checks"), list) and pcp["checks"])
    except Exception as exc:  # noqa: BLE001  # 连接/驱动不可用即跳过
        skip("Postgres 分支", f"连接/驱动不可用：{type(exc).__name__}: {exc}")
else:
    skip("Postgres 分支", "未设置 DBT_TEST_POSTGRES_URL")


# ==================================================================
print("\n" + "=" * 60)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项，跳过 {len(SKIPPED)} 项")
if FAILED:
    print("\n失败明细：")
    for _f in FAILED:
        print("  ✗", _f)
    raise SystemExit(1)
print("全部通过")
