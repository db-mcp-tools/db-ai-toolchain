"""端到端验证：连真实 MySQL，跑 dbcore 所有需要连库的工具。

与 tests/smoke_test.py（纯离线）互补：这里验证的是「真连库」这条链路
（只读护栏 / 执行窗口前置检查 / 执行计划 / 改写前后对比 / 行数一致性 / 索引顾问）。

起一个 MySQL（本机 3306 已被本地 5.7 占用，这里映射到宿主机 3307）：
    docker run -d --name dbcore-mysql -e MYSQL_ROOT_PASSWORD=root \\
        -e MYSQL_DATABASE=test -p 3307:3306 mysql:8.0

运行（PYTHONPATH 指向 packages，与 smoke_test 一致）：
    DBT_TEST_MYSQL_URL=mysql://root:root@127.0.0.1:3307/test \\
    PYTHONPATH=packages python tests/e2e_mysql.py

不设 DBT_TEST_MYSQL_URL 时默认连 127.0.0.1:3307；连不上则跳过（CI 安全）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

from dbcore.advisor import advise_indexes  # noqa: E402
from dbcore.connectors import ConnectionSpec, run_readonly  # noqa: E402
from dbcore.guard import GuardError  # noqa: E402
from dbcore.verify import equivalence_check, plan_compare, plan_metrics  # noqa: E402
from dbcore.connectors import precheck_change_window  # noqa: E402

URL = os.environ.get("DBT_TEST_MYSQL_URL", "mysql://root:root@127.0.0.1:3307/test")

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + str(detail)) if (detail and not cond) else ''}")
    if not cond:
        FAILED.append(name)


def setup() -> None:
    """用裸 pymysql 建表 + 造数（绕过只读护栏），幂等可重复跑。"""
    import pymysql

    spec = ConnectionSpec(URL)
    conn = pymysql.connect(
        host=spec.host, port=spec.port, user=spec.user, password=spec.password,
        database=spec.database, connect_timeout=5, charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS orders (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    user_id INT NOT NULL,
                    amount DECIMAL(10,2) NOT NULL,
                    user_name VARCHAR(64) NOT NULL,
                    created_at DATETIME NOT NULL,
                    KEY idx_user (user_id)
                ) ENGINE=InnoDB"""
            )
            cur.execute("SELECT COUNT(*) FROM orders")
            if cur.fetchone()[0] == 0:
                for i in range(5000):
                    uname = f"abc{i}" if i % 10 == 0 else f"user{i}"
                    cur.execute(
                        "INSERT INTO orders (user_id, amount, user_name, created_at) VALUES (%s,%s,%s,%s)",
                        (i % 200, (i % 1000) + 0.5, uname,
                         f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d} 10:00:00"),
                    )
                conn.commit()
                print("  (seeded 5000 rows into orders)")
    finally:
        conn.close()


def main() -> None:
    try:
        spec = ConnectionSpec(URL)
        run_readonly(URL, "SELECT 1", max_rows=1)
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: 无法连接 MySQL（{URL}）：{type(exc).__name__}: {exc}")
        print("      请先启动 MySQL 并设置 DBT_TEST_MYSQL_URL；无 DB 时跳过本测试。")
        sys.exit(0)

    print(f"== 连接 {spec.instance_id} ==")
    setup()

    print("== 1. 只读护栏拦截写语句 ==")
    blocked = False
    try:
        run_readonly(URL, "DELETE FROM orders WHERE id=1")
    except GuardError:
        blocked = True
    check("DELETE 被只读护栏拦截", blocked)

    print("== 2. precheck_change_window（执行窗口前置检查）==")
    pc = precheck_change_window(URL)
    check("返回 checks 非空", bool(pc["checks"]), pc.get("verdict"))
    check("含『长事务数』检查项", any(c["name"] == "长事务数" for c in pc["checks"]))
    check("含 online_alter 上限检查", any("online_alter" in c["name"] for c in pc["checks"]))
    check("独立实例默认 go=True", pc["go"] is True, pc.get("verdict"))
    check("instance_id 与连接一致", pc["instance_id"] == spec.instance_id)

    print("== 3. explain / 全表扫描检测 ==")
    m_bad = plan_metrics(URL, "SELECT * FROM orders WHERE amount > 100")
    check("无索引字段过滤触发全表扫描", m_bad["full_scan"] is True, m_bad["plan_text"][:100])
    m_good = plan_metrics(URL, "SELECT * FROM orders WHERE user_id = 5")
    check("命中 idx_user 不走全表扫描", m_good["full_scan"] is False, m_good.get("key"))

    print("== 4. plan_compare（改写前坏 → 改写后好）==")
    cmp = plan_compare(
        URL,
        "SELECT * FROM orders WHERE user_id + 0 = 5",
        "SELECT * FROM orders WHERE user_id = 5",
    )
    check("全表扫描被消除", "全表扫描已消除" in cmp["gains"], cmp["gains"])
    check("verdict=改写确实有效", cmp["verdict"] == "改写确实有效", cmp["verdict"])

    print("== 5. equivalence_check（相等 vs 不相等）==")
    eq_ok = equivalence_check(
        URL,
        "SELECT id FROM orders WHERE user_id = 5",
        "SELECT id FROM orders WHERE 5 = user_id",
    )
    check("语义相等两式行数一致", eq_ok["row_count_equal"] is True, eq_ok["verdict"])
    eq_bad = equivalence_check(
        URL,
        "SELECT id FROM orders WHERE user_id = 5",
        "SELECT id FROM orders WHERE user_id > 100",
    )
    check("语义不等两式被判定不一致", eq_bad["row_count_equal"] is False, eq_bad["verdict"])

    print("== 6. advise_indexes（读真实索引 + 选择性）==")
    adv = advise_indexes(URL, "orders", ["user_name", "amount"])
    existing_names = {i["name"] for i in adv["existing_indexes"]}
    check("读到已有 idx_user", "idx_user" in existing_names, adv["existing_indexes"])
    check("user_name 无覆盖→建议新建", adv["proposal"]["should_create"] is True, adv["proposal"]["verdict"])
    check("生成可执行 DDL", "ADD INDEX" in adv["proposal"]["ddl"], adv["proposal"]["ddl"])
    check("给出选择性数据", "user_name" in adv["selectivity"], adv["selectivity"])

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        sys.exit(1)
    print("ALL E2E PASSED")


if __name__ == "__main__":
    main()
