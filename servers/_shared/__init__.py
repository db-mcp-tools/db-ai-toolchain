"""MCP 公共脚手架。

做三件事：
  1. 兼容 MCP SDK 1.x（FastMCP）与 2.x（改名后的入口）—— 两个版本的类名/API 不同
  2. 把 dbcore.protocol.Result 自动转成工具返回值（dict）
  3. 统一的异常处理：任何工具抛异常都变成 ok=false 的信封，而不是让 MCP 会话崩掉

每个 server 只需要：

    from servers._shared import toolwrap
    mcp = toolwrap.make_server("sql-review", __version__)
    @toolwrap.RESULT_TOOL
    def my_tool(...)-> Result: ...
"""

from __future__ import annotations

import functools
import inspect
import os
import time
from typing import Any, Callable

from dbcore import logging_setup
from dbcore.protocol import Result, ToolError, fail as make_fail, PROTOCOL_VERSION

logging_setup.setup()

try:                                   # MCP SDK 2.x：入口已更名
    from mcp.server.mcpserver import MCPServer as _ServerCls  # type: ignore
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as _ServerCls  # type: ignore
    except ImportError as exc:          # pragma: no cover
        raise ImportError(
            "未安装 mcp 依赖。执行：pip install \"mcp>=1.0\""
        ) from exc


def make_server(name: str, version: str = PROTOCOL_VERSION) -> Any:
    return _ServerCls(name, version=version)


def _has_result_annotation(fn: Callable) -> bool:
    ann = inspect.signature(fn).return_annotation
    return ann is Result or ann == "Result"


def wrap(mcp_name: str, tool_name: str | None = None) -> Callable:
    """把一个返回 Result 的函数包装成 MCP 工具。

    自动完成：
      * Result -> dict
      * 异常 -> ok=false 信封
      * 计时
    """

    def deco(fn: Callable) -> Callable:
        name = tool_name or fn.__name__

        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> dict:
            t0 = time.time()
            try:
                r = fn(*args, **kwargs)
                if isinstance(r, Result):
                    r.took_ms = int((time.time() - t0) * 1000)
                    return r.to_dict()
                return {"ok": True, "schema": "dbt.result/v1", "mcp": mcp_name,
                        "tool": name, "version": PROTOCOL_VERSION,
                        "ts": int(time.time()), "data": r,
                        "verdict": "pass", "confidence": "low", "tier": "tier0",
                        "summary": "", "evidence": [], "next_actions": [],
                        "tier_upgrade": None, "warnings": [], "error": None, "took_ms": 0}
            except ToolError as exc:
                r = make_fail(mcp_name, name, exc)
                r.took_ms = int((time.time() - t0) * 1000)
                return r.to_dict()
            except Exception as exc:                       # 绝不让工具异常拖垮 MCP 会话
                r = Result(
                    ok=False, mcp=mcp_name, tool=name,
                    error={"code": "E_QUERY", "message": str(exc),
                           "detail": {"type": type(exc).__name__}},
                    summary=f"工具执行异常：{type(exc).__name__}: {exc}",
                )
                r.took_ms = int((time.time() - t0) * 1000)
                return r.to_dict()

        inner.__name__ = name
        inner.__wrapped_result__ = True                    # 供 CLI 区分
        return inner

    return deco


def register(mcp: Any, mcp_name: str, func: Callable, tool_name: str | None = None) -> Callable:
    """把函数注册为 MCP 工具（带 Result 包装）。"""
    wrapped = wrap(mcp_name, tool_name)(func)
    try:
        mcp.tool(wrapped)
    except TypeError:                                       # 某些版本要求显式 name
        mcp.tool(name=tool_name or func.__name__)(wrapped)
    return wrapped


def register_all(mcp: Any, mcp_name: str, namespace: dict) -> list[str]:
    """把模块里所有公开的可调用对象注册为工具。"""
    names: list[str] = []
    for n, obj in list(namespace.items()):
        if n.startswith("_") or not callable(obj):
            continue
        if getattr(obj, "__wrapped_result__", False):
            continue
        register(mcp, mcp_name, obj, n)
        names.append(n)
    return names


def tool_names(mcp: Any) -> list[str]:
    import asyncio
    r = mcp.list_tools()
    tools = asyncio.run(r) if inspect.isawaitable(r) else r
    return [t.name for t in tools]


def instance_id_of(dsn: str | None = None) -> str:
    if not dsn:
        return ""
    from dbcore.ids import instance_id_from_dsn
    return instance_id_from_dsn(dsn)


def audit(mcp: str, tool: str, instance_id: str, summary: str, correlation_id: str = "") -> None:
    try:
        from dbcore.profile_store import record_audit
        record_audit(f"{mcp}.{tool}", instance_id, summary, correlation_id)
    except Exception:
        pass                                                # 审计失败不能影响主流程


def store_root() -> str:
    return os.environ.get("DBT_STORE", os.path.join(os.path.expanduser("~"), ".dbtoolchain"))
