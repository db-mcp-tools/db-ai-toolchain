#!/usr/bin/env python3
"""dbt —— db-ai-toolchain 的统一命令行入口。

存在的理由：
  * MCP 是给 Agent 用的；CI 和人类需要 CLI
  * 同一个能力在 MCP 和 CLI 里必须是同一份实现（CLI 直接调 server 的函数）
  * CI 门禁只需要一条命令：dbt review --diff

用法：
    dbt review "SELECT * FROM t"                    # 体检单条
    dbt review --file src/main.sql                  # 体检文件
    dbt review --dir ./sql --fail-on high           # 存量扫描 + 门禁
    dbt review --diff                               # 只体检本次 git 改动（CI 用）
    dbt fix "SELECT ..."                            # 给出修法
    dbt risk "ALTER TABLE ..." --rows 50000000      # 变更风险评估
    dbt watch "主从延迟突增"                          # Tier0 症状诊断（无需连库）
    dbt watch --snapshot mysql://...                # 采集现场快照
    dbt migrate --to postgres oracle.sql            # 迁移差异检查
    dbt semantics --compare a.sql b.sql             # 口径比对
    dbt profile --stats                             # 档案库概况
    dbt run --seed sql-review.review_sql --sql "SELECT ..."   # 跨 MCP 自动追链
    dbt run --recipe orchestrator/examples/safe_change.json   # 确定性流水线
    dbt tools                                       # 列出所有 MCP 与工具
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _boot() -> None:
    """保证从任意 cwd 都能 import 到 packages/ 与 servers/。"""
    import os
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for p in (root / "packages", root):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")


_boot()


def _print(result: Any, as_json: bool) -> None:
    d = result.to_dict() if hasattr(result, "to_dict") else result
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
        return
    from dbcore.report import render_result
    print(render_result(d))


def _exit_code(result: Any, fail_on: str = "critical") -> int:
    d = result.to_dict() if hasattr(result, "to_dict") else result
    if not d.get("ok"):
        return 2
    order = ["info", "low", "medium", "high", "critical"]
    if d.get("tool") in ("review_batch", "review_diff", "review_file"):
        return 1 if d["data"].get("gate") == "FAIL" else 0
    verd = d.get("verdict")
    sev = {"fail": "critical", "block": "critical", "warn": "medium", "pass": "info"}.get(verd, "info")
    return 1 if order.index(sev) >= order.index(fail_on) else 0


def cmd_tools(args) -> int:
    import asyncio
    import inspect
    from servers.sql_review import server as SR
    from servers.change_risk import server as CR
    from servers.dbwatch import server as DW
    from servers.migration_diff import server as MD
    from servers.data_semantics import server as DS
    from servers.profile_hub import server as PH

    total = 0
    for S in (SR, CR, DW, MD, DS, PH):
        r = S.mcp.list_tools()
        tools = asyncio.run(r) if inspect.isawaitable(r) else r
        total += len(tools)
        if args.json:
            print(json.dumps({"mcp": S.MCP_NAME, "tools": [t.name for t in tools]},
                             ensure_ascii=False))
        else:
            print(f"{S.MCP_NAME:16s} {len(tools):3d} 个工具")
            for t in tools:
                print(f"  - {t.name}")
    if not args.json:
        print(f"\n合计 {total} 个工具")
    return 0


def cmd_review(args) -> int:
    from servers.sql_review import server as SR
    if args.diff:
        r = SR.review_diff(repo_root=args.path or ".", base=args.base, head=args.head,
                           dialect=args.dialect, fail_on=args.fail_on)
    elif args.dir:
        r = SR.review_file(args.dir, dialect=args.dialect, fail_on=args.fail_on)
    elif args.file:
        r = SR.review_file(args.file, dialect=args.dialect, fail_on=args.fail_on)
    elif args.sql:
        r = SR.review_sql(" ".join(args.sql), dialect=args.dialect,
                          table_rows=args.rows, profile=args.profile,
                          disable=args.disable, enable=args.enable)
    else:
        print("需要 --sql / --file / --dir / --diff 之一", file=sys.stderr)
        return 2
    _print(r, args.json)
    return _exit_code(r, args.fail_on)


def cmd_fix(args) -> int:
    from servers.sql_review import server as SR
    r = SR.suggest_fixes(" ".join(args.sql), dialect=args.dialect,
                         pk_column=args.pk, table_rows=args.rows)
    _print(r, args.json)
    if not args.json and r.data.get("composed_sql"):
        print("\n=== 可直接使用的 SQL ===")
        print(r.data["composed_sql"])
    return 0


def cmd_risk(args) -> int:
    from servers.change_risk import server as CR
    r = CR.assess_change(" ".join(args.sql), dialect=args.dialect,
                         table_rows=args.rows, during_peak=args.peak)
    _print(r, args.json)
    rb = CR.generate_rollback(" ".join(args.sql), dialect=args.dialect)
    if not args.json:
        print("\n=== 回滚方案 ===")
        print(rb.data["rollback_sql"])
    return _exit_code(r, "high")


def cmd_watch(args) -> int:
    from servers.dbwatch import server as DW
    if args.snapshot:
        r = DW.capture_snapshot(args.snapshot, dialect=args.dialect, persist=not args.no_persist)
    elif args.symptom:
        r = DW.explain_symptom(" ".join(args.symptom), dialect=args.dialect)
    elif args.report:
        r = DW.incident_report(symptom=" ".join(args.report))
        if not args.json:
            print(r.data["markdown"])
            return 0
    elif args.slow_log:
        text = open(args.slow_log, "r", encoding="utf-8", errors="ignore").read()
        r = DW.analyze_slow_log(text, dialect=args.dialect)
    else:
        print("需要 --symptom / --snapshot / --report / --slow-log 之一", file=sys.stderr)
        return 2
    _print(r, args.json)
    return 0


def cmd_migrate(args) -> int:
    from servers.migration_diff import server as MD
    sql = open(args.file, "r", encoding="utf-8", errors="ignore").read() if args.file else ""
    if args.sql:
        sql = " ".join(args.sql)
    if not sql:
        print("需要提供 --file 或 --sql", file=sys.stderr)
        return 2
    if args.type_map:
        r = MD.check_type_mapping(args.type_map, target_dialect=args.to)
    elif args.effort:
        from dbcore.scanner import split_sql_statements
        r = MD.estimate_rewrite_effort(split_sql_statements(sql), target_dialect=args.to)
    else:
        r = MD.detect_dialect_gap(sql, target_dialect=args.to, source_dialect=args.from_)
    _print(r, args.json)
    return 0


def cmd_semantics(args) -> int:
    from servers.data_semantics import server as DS
    if args.compare:
        a = open(args.compare[0], encoding="utf-8", errors="ignore").read()
        b = open(args.compare[1], encoding="utf-8", errors="ignore").read()
        r = DS.compare_definitions(a, b, dialect=args.dialect)
    elif args.parse:
        sql = (open(args.parse, encoding="utf-8", errors="ignore").read()
               if args.parse.endswith((".sql", ".txt")) else args.parse)
        r = DS.parse_metric_definition(sql=sql, dialect=args.dialect)
    elif args.fanout:
        sql = (open(args.fanout, encoding="utf-8", errors="ignore").read()
               if args.fanout.endswith((".sql", ".txt")) else args.fanout)
        r = DS.detect_fanout(sql, dialect=args.dialect)
    elif args.drift:
        pts = json.load(open(args.drift, encoding="utf-8"))
        r = DS.detect_drift(pts)
    else:
        print("需要 --compare / --parse / --fanout / --drift 之一", file=sys.stderr)
        return 2
    _print(r, args.json)
    return 0


def cmd_profile(args) -> int:
    from servers.profile_hub import server as PH
    if args.stats:
        r = PH.store_stats_tool()
    elif args.tier:
        r = PH.tier_of_tool(args.tier)
    elif args.export:
        r = PH.export_profile_tool(instance_id=args.export if args.export != "all" else None)
        if not args.json:
            print(json.dumps(r.data, ensure_ascii=False, indent=2))
            return 0
    elif args.import_file:
        data = json.load(open(args.import_file, encoding="utf-8"))
        r = PH.import_profile_tool(data, remap_to=args.remap)
    elif args.instances:
        r = PH.list_instances_tool()
    else:
        r = PH.store_stats_tool()
    _print(r, args.json)
    return 0


def cmd_run(args) -> int:
    """跨 MCP 编排：把剩余参数转交给 orchestrator 引擎执行。"""
    from orchestrator.__main__ import main as orch_main

    argv: list[str] = []
    if args.recipe:
        argv += ["--recipe", args.recipe]
    else:
        if not args.seed:
            print("需要 --seed MCP.tool 或 --recipe", file=sys.stderr)
            return 2
        argv += ["--seed", args.seed]
        if args.sql is not None:
            argv += ["--sql", args.sql]
        argv += ["--dialect", args.dialect]
        for kv in args.arg:
            argv += ["--arg", kv]
        if args.follow_optional:
            argv += ["--follow-optional"]
        if args.correlation_id:
            argv += ["--correlation-id", args.correlation_id]
        if args.instance_id:
            argv += ["--instance-id", args.instance_id]
        argv += ["--max-steps", str(args.max_steps)]
        if args.title:
            argv += ["--title", args.title]
    if args.json:
        argv += ["--json"]
    return orch_main(argv)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dbt", description="db-ai-toolchain CLI")
    p.add_argument("--json", action="store_true", help="输出原始 JSON 信封")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("review", help="SQL 体检（CI 门禁）")
    r.add_argument("sql", nargs="*", help="待体检的 SQL")
    r.add_argument("--file", help="文件路径（.sql / mapper xml / java）")
    r.add_argument("--dir", help="目录（递归扫描）")
    r.add_argument("--diff", action="store_true", help="只体检 git diff 中的 SQL")
    r.add_argument("--path", help="git 仓库路径，默认当前目录")
    r.add_argument("--base", default="HEAD~1")
    r.add_argument("--head", default="HEAD")
    r.add_argument("--dialect", default="mysql")
    r.add_argument("--rows", type=int, default=None, help="表行数（提升风险判定准确度）")
    r.add_argument("--fail-on", default="critical",
                   choices=["critical", "high", "medium", "low"])
    r.add_argument("--profile", default="default",
                   help="规则配置：default / lenient / ci-gate / bank-core")
    r.add_argument("--disable", nargs="*", default=None,
                   help="本次禁用的规则 ID，例如 --disable SEL001 DML004")
    r.add_argument("--enable", nargs="*", default=None,
                   help="本次强制启用的规则 ID（优先级最高）")
    r.set_defaults(func=cmd_review)

    f = sub.add_parser("fix", help="给出修法")
    f.add_argument("sql", nargs="+")
    f.add_argument("--dialect", default="mysql")
    f.add_argument("--pk", default=None, help="主键列名（游标分页用）")
    f.add_argument("--rows", type=int, default=None)
    f.set_defaults(func=cmd_fix)

    k = sub.add_parser("risk", help="变更风险评估")
    k.add_argument("sql", nargs="+")
    k.add_argument("--dialect", default="mysql")
    k.add_argument("--rows", type=int, default=None)
    k.add_argument("--peak", action="store_true", help="是否在业务高峰期执行")
    k.set_defaults(func=cmd_risk)

    w = sub.add_parser("watch", help="值守 / 根因（Tier0 可用）")
    w.add_argument("symptom", nargs="*", help="症状描述，例如「主从延迟突增」")
    w.add_argument("--snapshot", help="连接串，采集现场快照")
    w.add_argument("--report", nargs="+", help="生成事故报告（markdown）")
    w.add_argument("--slow-log", help="慢日志文件路径")
    w.add_argument("--dialect", default="mysql")
    w.add_argument("--no-persist", action="store_true")
    w.set_defaults(func=cmd_watch)

    m = sub.add_parser("migrate", help="迁移差异检查")
    m.add_argument("--to", default="postgres")
    m.add_argument("--from", dest="from_", default="mysql")
    m.add_argument("--file", help="SQL 文件")
    m.add_argument("sql", nargs="*")
    m.add_argument("--type-map", help="类型映射查询，例如 NUMBER")
    m.add_argument("--effort", action="store_true", help="估算改造工作量")
    m.set_defaults(func=cmd_migrate)

    s = sub.add_parser("semantics", help="数据口径治理")
    s.add_argument("--compare", nargs=2, metavar=("A", "B"), help="比较两个 SQL 文件")
    s.add_argument("--parse", help="解析口径（SQL 文本或文件）")
    s.add_argument("--fanout", help="JOIN 扇出检测")
    s.add_argument("--drift", help="漂移检测的 JSON 文件")
    s.add_argument("--dialect", default="mysql")
    s.set_defaults(func=cmd_semantics)

    pf = sub.add_parser("profile", help="档案中枢")
    pf.add_argument("--stats", action="store_true")
    pf.add_argument("--tier", help="查询实例档位")
    pf.add_argument("--export", help="导出知识包（实例 ID 或 all）")
    pf.add_argument("--import-file", help="导入知识包路径")
    pf.add_argument("--remap", help="导入时挂载到该实例 ID")
    pf.add_argument("--instances", action="store_true")
    pf.set_defaults(func=cmd_profile)

    t = sub.add_parser("tools", help="列出所有 MCP 与工具")
    t.set_defaults(func=cmd_tools)

    rn = sub.add_parser("run", help="跨 MCP 编排（自动追 next_actions）")
    rn.add_argument("--seed", help="入口工具，形如 mcp.tool 或纯 tool 名")
    rn.add_argument("--arg", action="append", default=[], metavar="k=v",
                    help="传给入口工具的参数，可重复")
    rn.add_argument("--sql", help="便捷参数：等价于 --arg sql=...")
    rn.add_argument("--dialect", default="mysql")
    rn.add_argument("--follow-optional", action="store_true",
                    help="连「可选」的 next_actions 也一并追")
    rn.add_argument("--recipe", help="确定性流水线文件路径（JSON/YAML 列表）")
    rn.add_argument("--correlation-id", default="", help="复用已有关联 ID")
    rn.add_argument("--instance-id", default="", help="实例 ID（用于串档案）")
    rn.add_argument("--max-steps", type=int, default=50)
    rn.add_argument("--title", default="编排执行报告")
    rn.add_argument("--json", action="store_true", help="输出 trace 的 JSON")
    rn.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
