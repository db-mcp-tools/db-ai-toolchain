#!/usr/bin/env python3
"""用真实 MCP 客户端把 6 个 server 各挂一次，走一遍 stdio 协议。

    python scripts/smoke-mcp.py              # 全部
    python scripts/smoke-mcp.py --only watch # 名字包含 watch 的
    python scripts/smoke-mcp.py -v           # 打印每个工具名

为什么需要它：单测都是直接 `import server` 拿函数来调，证明的是「函数算得对」，
没有证明「MCP 协议层面挂得上」。这两者的差异不是理论问题 —— 上一次 CLI 的
`tools` 子命令就是单测全绿、从装好的 wheel 一跑就崩。

它查四件事：
  1. 进程能起来并完成 initialize 握手（import 期错误、缺依赖都会在这里暴露）
  2. `tools/list` 数量与预期一致（工具增删时忘了同步文档会被抓）
  3. 每个工具都有非空 description 与 inputSchema
     —— 模型只能靠这两个字段决定要不要调，缺了等于工具不存在
  4. 真调一个工具，返回值能解析成 `dbt.result/v1` 信封且 `ok` 为真
     （探针一律选「本来就该成功」的调用，不选依赖真实数据库的）

需要先装 MCP SDK：pip install "sqltk-core[mcp]" 或 pip install mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SMOKE_DIR = tempfile.mkdtemp(prefix="dbt-smoke-")

# (发行名, 源码目录, 模块名, 期望工具数, 探针工具, 探针参数)
# 期望值来自 CHANGELOG 的工具表：改动工具集时两边一起改，否则这里会红。
SERVERS = [
    ("mcp-sql-review", "mcp-sql-review", "sql_review", 14, "list_rules", {}),
    ("mcp-change-risk", "mcp-change-risk", "change_risk", 13, "assess_change",
     {"sql": "UPDATE orders SET status = 'x' WHERE id = 1", "dialect": "mysql"}),
    ("mcp-dbwatch", "mcp-dbwatch", "dbwatch", 15, "oncall_checklist", {}),
    ("mcp-migration-diff", "mcp-migration-diff", "migration_diff", 12,
     "check_type_mapping", {"source_type": "NUMBER", "target_dialect": "postgres"}),
    ("mcp-data-semantics", "mcp-data-semantics", "data_semantics", 13,
     "list_definitions", {}),
    ("mcp-profile-hub", "mcp-profile-hub", "profile_hub", 24,
     "upsert_instance", {"instance_id": "smoke", "url": "mysql://localhost/db"}),
]

PACKAGE_DIRS = ["sqltk-core", "sqltk-store", "sqltk-know"]


def pypath() -> str:
    """把各仓 src 拼成 PYTHONPATH —— 不要求先 pip install 才能跑冒烟。"""
    parts = [str(ROOT / d / "src") for d in PACKAGE_DIRS]
    parts += [str(ROOT / s[1] / "src") for s in SERVERS]
    old = os.environ.get("PYTHONPATH")
    if old:
        parts.append(old)
    return os.pathsep.join(parts)


def _field(obj, *names, default=None):
    """MCP SDK 1.x 用驼峰（serverInfo / inputSchema / isError），2.x 改成了下划线。
    两边都试一遍 —— sqltk-core 的运行时外壳本来就同时支持两代。"""
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def _content_text(result) -> str:
    out = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            out.append(text)
    return "\n".join(out)


def child_env(installed: bool) -> dict:
    """冒烟必须是封闭的：给每个 server 独立的临时档案库。

    否则一次冒烟就会往用户真实的 ~/.dbtoolchain/store.db 里写一条
    instance_id=smoke 的记录（探针 upsert_instance 是写操作）。

    installed=True 时不注入源码路径，用的是当前解释器里**装好的**包 ——
    发布前该用这个：验证 wheel 而不是工作区。
    """
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DBT_STORE": os.path.join(SMOKE_DIR, "store.db"),
        "DBT_LOG_LEVEL": "ERROR",
    }
    if not installed:
        env["PYTHONPATH"] = pypath()
    return env


async def probe(spec: tuple, env: dict) -> list[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    name, _dir, module, expected, tool, args = spec
    problems: list[str] = []

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_name = _field(_field(init, "server_info", "serverInfo", default=init),
                                "name", default="?")
            tools = (await session.list_tools()).tools

            if len(tools) != expected:
                problems.append(f"{name}: 工具数 {len(tools)}，预期 {expected}"
                                f"（改了工具集就同步改本脚本与 CHANGELOG）")

            for t in tools:
                if not (t.description or "").strip():
                    problems.append(f"{name}.{t.name}: 缺 description")
                schema = _field(t, "input_schema", "inputSchema", default={}) or {}
                if schema.get("type") != "object":
                    problems.append(f"{name}.{t.name}: inputSchema 不是 object")

            names = {t.name for t in tools}
            if tool not in names:
                problems.append(f"{name}: 探针工具 {tool} 不在工具表里")
                return problems

            res = await asyncio.wait_for(session.call_tool(tool, args), timeout=30)
            if _field(res, "is_error", "isError", default=False):
                problems.append(f"{name}.{tool}: 调用返回 isError\n"
                                f"        {_content_text(res)[:300]}")
                return problems

            text = _content_text(res)
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                problems.append(f"{name}.{tool}: 返回值不是 JSON：{text[:200]}")
                return problems
            if not isinstance(payload, dict):
                problems.append(f"{name}.{tool}: 返回值不是对象：{type(payload).__name__}")
                return problems
            if payload.get("schema") != "dbt.result/v1":
                problems.append(f"{name}.{tool}: schema={payload.get('schema')!r}，"
                                f"应为 'dbt.result/v1'")
            # 探针刻意选「本来就该成功」的调用（读字典 / 静态计算 / 写本地元数据）。
            # ok=False 说明这条路径真的断了 —— 旧库升级那个缺陷就是这样暴露的。
            if payload.get("ok") is not True:
                problems.append(f"{name}.{tool}: ok={payload.get('ok')!r} "
                                f"{(payload.get('error') or {}).get('message', '')}"
                                f"｜{payload.get('summary', '')[:120]}")
                return problems

            print(f"  ✓ {name:20s} {server_name:20s} {len(tools):3d} 工具  "
                  f"探针 {tool} → ok={payload.get('ok')}")
    return problems


def _flatten(exc: BaseException) -> list[str]:
    """asyncio 的 TaskGroup 会把真实原因裹进 ExceptionGroup，
    直接打印只会看到「unhandled errors in a TaskGroup」，等于没信息。"""
    subs = getattr(exc, "exceptions", None)
    if subs:
        out: list[str] = []
        for s in subs:
            out += _flatten(s)
        return out
    return [f"{type(exc).__name__}: {exc}"]


async def run(specs: list[tuple], installed: bool) -> int:
    all_problems: list[str] = []
    env = child_env(installed)
    for spec in specs:
        try:
            all_problems += await asyncio.wait_for(probe(spec, env), timeout=90)
        except asyncio.TimeoutError:
            all_problems.append(f"{spec[0]}: 90s 超时（进程没起来，或在等输入）")
        except Exception as exc:  # noqa: BLE001 —— 冒烟失败要看到原始异常
            all_problems.append(f"{spec[0]}: " + " / ".join(_flatten(exc)))

    print()
    if all_problems:
        print(f"✗ {len(all_problems)} 个问题：")
        for p in all_problems:
            print("  -", p)
        return 1
    print(f"全部通过（{len(specs)} 个 MCP server 挂载成功）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="用真实 MCP 客户端冒烟各 server")
    ap.add_argument("--only", default="", help="只跑名字包含该子串的 server")
    ap.add_argument("--installed", action="store_true",
                    help="用当前环境里已安装的包，而不是仓库源码（发布前验证 wheel 用）")
    args = ap.parse_args()

    specs = [s for s in SERVERS if args.only in s[0]] if args.only else SERVERS
    if not specs:
        print("没有匹配的 server", file=sys.stderr)
        return 2

    try:
        import mcp  # noqa: F401
    except ImportError:
        print('✗ 没装 MCP SDK。先 pip install "sqltk-core[mcp]"（或 pip install mcp）',
              file=sys.stderr)
        return 2

    src = "已安装的包" if args.installed else "仓库源码"
    print(f"冒烟 {len(specs)} 个 MCP server（Python {sys.version.split()[0]}，{src}）\n")
    return asyncio.run(run(specs, args.installed))


if __name__ == "__main__":
    raise SystemExit(main())
