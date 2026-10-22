"""orchestrator 命令行入口。

用法：
    # 自动追链：体检 →（自动）suggest_fixes →（自动）verify_fix / assess_change
    python -m orchestrator --seed sql-review.review_sql --sql "SELECT * FROM t WHERE 1=1" --dialect mysql

    # 追链时连「可选」后续也跑
    python -m orchestrator --seed sql-review.review_sql --sql "..." --follow-optional

    # 确定性流水线（适合 CI / 受控变更）
    python -m orchestrator --recipe pipeline.json

    # 仅输出机器可读的 trace
    python -m orchestrator --seed ... --json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

# 保证从任意 cwd 都能 import 到 packages/ 与 servers/
_root = pathlib.Path(__file__).resolve().parent.parent
for _p in (_root / "packages", _root):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")


def _boot() -> None:
    from .engine import Orchestrator
    from .report import render_trace
    return Orchestrator, render_trace


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="orchestrator", description="跨 MCP 编排引擎")
    ap.add_argument("--seed", help="入口工具，形如 mcp.tool 或纯 tool 名")
    ap.add_argument("--arg", action="append", default=[], metavar="k=v",
                    help="传给入口工具的参数，可重复；k=v")
    ap.add_argument("--sql", help="便捷参数：等价于 --arg sql=...")
    ap.add_argument("--dialect", default="mysql", help="便捷参数：等价 --arg dialect=")
    ap.add_argument("--follow-optional", action="store_true",
                    help="连「可选」的 next_actions 也一并追")
    ap.add_argument("--recipe", help="确定性流水线文件路径（JSON 或 YAML 列表）")
    ap.add_argument("--correlation-id", default="", help="复用已有关联 ID")
    ap.add_argument("--instance-id", default="", help="实例 ID（用于串档案）")
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument("--json", action="store_true", help="只输出 trace 的 JSON")
    ap.add_argument("--title", default="编排执行报告")
    return ap.parse_args(argv)


def _seed_args(ns: argparse.Namespace) -> dict:
    args: dict = {}
    for kv in ns.arg:
        if "=" not in kv:
            raise SystemExit(f"--arg 必须是 k=v 形式：{kv}")
        k, v = kv.split("=", 1)
        args[k] = _coerce(v)
    if ns.sql is not None:
        args["sql"] = ns.sql
    args.setdefault("dialect", ns.dialect)
    return args


def _coerce(v: str):
    if v == "true":
        return True
    if v == "false":
        return False
    if v.isdigit():
        return int(v)
    return v


def _load_recipe(path: str) -> list[dict]:
    text = pathlib.Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
            return yaml.safe_load(text)
        except ImportError:
            # 没有 yaml 时，允许 YAML 退化为「每行一个 tool 名」的最简格式
            return [{"tool": ln.strip()} for ln in text.splitlines()
                    if ln.strip() and not ln.startswith("#")]
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    Orchestrator, render_trace = _boot()

    if ns.recipe:
        steps = _load_recipe(ns.recipe)
        orch = Orchestrator(max_steps=ns.max_steps)
        trace = orch.run_recipe(steps, instance_id=ns.instance_id,
                                correlation_id=ns.correlation_id)
    elif ns.seed:
        mcp, _, tool = ns.seed.partition(".")
        if not tool:                      # 纯 tool 名
            mcp, tool = None, ns.seed
        args = _seed_args(ns)
        orch = Orchestrator(max_steps=ns.max_steps,
                             follow_optional=ns.follow_optional)
        trace = orch.run(mcp or None, tool, args,
                         instance_id=ns.instance_id,
                         correlation_id=ns.correlation_id,
                         follow_optional=ns.follow_optional if ns.follow_optional else None)
    else:
        print("需要 --seed MCP.tool 或 --recipe path", file=sys.stderr)
        return 2

    if ns.json:
        print(json.dumps(trace.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(render_trace(trace, ns.title))
    failed = [s for s in trace.steps if not s.ok and not s.skipped]
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
