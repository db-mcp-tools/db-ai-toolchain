"""端到端验证：连真实 PostgreSQL，跑 dbcore 所有需要连库的工具。

与 tests/e2e_mysql.py 对称：这里验证的是「真连 Postgres」这条链路
（只读护栏 / 执行窗口前置检查 / 执行计划 / 改写前后对比 / 行数一致性 / 索引顾问）。

起一个 PostgreSQL（本机 5432 已被其他项目占用，这里映射到宿主机 5433）：
    docker run -d --name dbcore-postgres -e POSTGRES_PASSWORD=root \\
        -e POSTGRES_DB=test -e POSTGRES_USER=postgres -p 5433:5432 postgres:17

运行（PYTHONPATH 指向 packages，与 smoke_test 一致）：
    DBT_TEST_POSTGRES_URL=postgres://postgres:root@127.0.0.1:5433/test \\
    PYTHONPATH=packages python tests/e2e_postgres.py

不设 DBT_TEST_POSTGRES_URL 时默认连 127.0.0.1:5433；连不上则跳过（CI 安全）。

关于全表扫描判定的确定性：
    bad = 对列套表达式（user_id + 0 = 5）：任何索引都用不上 → 必然是 Seq Scan；
    good = 主键等值（id = 5）：必然走主键索引（Index Scan）。
    两者都不依赖优化器「心情」，小表也稳定可复现。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 兜底搜索路径（仅文件直载用，不优先于 pip 安装的 canonical 包）
sys.path.append(str(ROOT / "servers"))
sys.path.append(str(ROOT / "packages"))

from dbcore.connectors import ConnectionSpec, run_readonly, precheck_change_window
from dbcore.guard import GuardError


def _import_sql_review(mod: str, *names: str) -> dict:
    """优先用 canonical 安装包；失败时从 sql_review/<mod>.py 直接文件加载，
    绕过 sql_review/__init__ 对 sqltk-store 的重依赖（本地最小验证用）。"""
    try:
        pkg = __import__("sql_review", fromlist=[mod])
        sub = getattr(pkg, mod)
        return {n: getattr(sub, n) for n in names}
    except Exception:  # noqa: BLE001
        pass
    for base in sys.path:
        cand = os.path.join(base, "sql_review", f"{mod}.py")
        if os.path.isfile(cand):
            spec = importlib.util.spec_from_file_location(f"_sr_{mod}", cand)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return {n: getattr(m, n) for n in names}
    raise ImportError(f"sql_review.{mod}")


try:
    _v = _import_sql_review("verify", "equivalence_check", "plan_compare", "plan_metrics")
    equivalence_check, plan_compare, plan_metrics = (
        _v["equivalence_check"], _v["plan_compare"], _v["plan_metrics"])
    advise_indexes = _import_sql_review("advisor", "advise_indexes")["advise_indexes"]
except ImportError as e:  # noqa: BLE001
    print(f"SKIP: 无法导入 sql_review.verify/advisor（需 pip install -e sqltk-core mcp-sql-review）：{e}")
    sys.exit(0)

URL = os.environ.get("DBT_TEST_POSTGRES_URL", "postgres://postgres:root@127.0.0.1:5433/test")

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + str(detail)) if (detail and not cond) else ''}")
    if not cond:
        FAILED.append(name)


def setup() -> None:
    """用裸 psycopg 建表 + 造数（绕过只读护栏），幂等可重复跑。"""
    import psycopg

    spec = ConnectionSpec(URL)
    conn = psycopg.connect(
        host=spec.host, port=spec.port, dbname=spec.database,
        user=spec.user, password=spec.password, connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    user_id INT NOT NULL,
                    amount NUMERIC(10,2) NOT NULL,
                    user_name VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_user ON orders (user_id)")
            cur.execute("SELECT COUNT(*) FROM orders")
            if cur.fetchone()[0] == 0:
                rows = []
                for i in range(5000):
                    uname = f"abc{i}" if i % 10 == 0 else f"user{i}"
                    ts = f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d} 10:00:00"
                    rows.append((i % 200, (i % 1000) + 0.5, uname, ts))
                cur.executemany(
                    "INSERT INTO orders (user_id, amount, user_name, created_at) VALUES (%s,%s,%s,%s)",
                    rows,
                )
                conn.commit()
                print("  (seeded 5000 rows into orders)")
            else:
                print("  (orders already seeded)")
            cur.execute("ANALYZE orders")
    finally:
        conn.close()


def main() -> None:
    env_url = os.environ.get("DBT_TEST_POSTGRES_URL")
    try:
        spec = ConnectionSpec(URL)
        run_readonly(URL, "SELECT 1", max_rows=1)
    except Exception as exc:  # noqa: BLE001
        # 已显式给定 URL 却连不上 → 视为测试失败（防 CI 假绿）；
        # 完全没给 URL → 视为「无 DB 环境」，优雅跳过。
        if env_url:
            print(f"FAIL: 已设置 DBT_TEST_POSTGRES_URL 但无法连接 PostgreSQL（{URL}）：{type(exc).__name__}: {exc}")
            sys.exit(1)
        print(f"SKIP: 无法连接 PostgreSQL（{URL}）：{exc}；请先启动 PostgreSQL 并设置 DBT_TEST_POSTGRES_URL。")
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
    check("含『复制延迟秒』检查项（PG 专属）", any("复制延迟" in c["name"] for c in pc["checks"]))
    check("含『lock_timeout』检查项", any("lock_timeout" in c["name"] for c in pc["checks"]))
    check("无 critical 级别告警（warn 不算阻断）",
          all(c["status"] != "critical" for c in pc["checks"]),
          [c for c in pc["checks"] if c["status"] == "critical"])
    check("instance_id 与连接一致", pc["instance_id"] == spec.instance_id)

    print("== 3. explain / 全表扫描检测 ==")
    # 表达式包列：任何索引都用不上 → 必然 Seq Scan
    m_bad = plan_metrics(URL, "SELECT * FROM orders WHERE user_id + 0 = 5")
    check("表达式包列触发全表扫描(Seq Scan)", m_bad["full_scan"] is True, m_bad["plan_text"][:120])
    # 主键等值：必然走主键索引 → 非全表扫描
    m_good = plan_metrics(URL, "SELECT * FROM orders WHERE id = 5")
    check("主键等值不走全表扫描(Index Scan)", m_good["full_scan"] is False, m_good["plan_text"][:120])

    print("== 4. plan_compare（改写前坏 → 改写后好）==")
    cmp = plan_compare(
        URL,
        "SELECT * FROM orders WHERE user_id + 0 = 5",
        "SELECT * FROM orders WHERE id = 5",
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
    check("生成可执行 DDL（含 INDEX 关键字）",
          "INDEX" in adv["proposal"]["ddl"], adv["proposal"]["ddl"])
    check("给出选择性数据", "user_name" in adv["selectivity"], adv["selectivity"])

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        sys.exit(1)
    print("ALL E2E PASSED")


if __name__ == "__main__":
    main()
