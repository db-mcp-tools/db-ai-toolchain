"""工具注册表 —— 把分散在 6 个 MCP 里的 TOOLS 字典汇总成一张全局表。

这样编排引擎只需要一个名字（或 mcp+tool）就能定位到可调用函数，
而不必关心某个工具属于哪个 MCP、用哪个模块实现。

每个 server 模块在文件末尾都定义了 `TOOLS = {工具名: 函数}`，
本模块直接复用它，保证编排层与 MCP 暴露的工具完全一致（单一事实来源）。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

# (MCP 显示名, server 模块路径) —— 与 DESIGN.md §3 一一对应
SERVER_SPECS = [
    ("sql-review", "servers.sql_review.server"),
    ("change-risk", "servers.change_risk.server"),
    ("dbwatch", "servers.dbwatch.server"),
    ("migration-diff", "servers.migration_diff.server"),
    ("data-semantics", "servers.data_semantics.server"),
    ("profile-hub", "servers.profile_hub.server"),
]


@dataclass(frozen=True)
class ToolEntry:
    mcp: str
    tool: str
    func: Callable


class Registry:
    """(mcp, tool) 与 纯工具名 两种定位方式。"""

    def __init__(self, by_mcp_tool: dict, by_name: dict, ambiguous: set) -> None:
        self.by_mcp_tool = by_mcp_tool
        self.by_name = by_name
        self.ambiguous = ambiguous

    def get(self, mcp: str | None, tool: str) -> ToolEntry | None:
        if mcp:
            return self.by_mcp_tool.get((mcp, tool))
        entry = self.by_name.get(tool)
        if entry is None:
            return None
        mcp_name, func = entry
        return ToolEntry(mcp_name, tool, func)

    def resolve(self, mcp: str | None, tool: str) -> ToolEntry:
        entry = self.get(mcp, tool)
        if entry is None:
            hint = f"{mcp}.{tool}" if mcp else tool
            if tool in self.ambiguous:
                raise KeyError(
                    f"工具名 {tool} 在多个 MCP 中存在歧义，必须指定 mcp=<全称>"
                )
            raise KeyError(f"未知工具：{hint}（检查 mcp 名与 tool 名是否拼写正确）")
        return entry

    def names(self) -> list[str]:
        return sorted(self.by_name)


def build_registry() -> Registry:
    by_mcp_tool: dict = {}
    by_name: dict = {}
    ambiguous: set = set()

    for mcp_name, modpath in SERVER_SPECS:
        mod = importlib.import_module(modpath)
        mname = getattr(mod, "MCP_NAME", mcp_name)
        tools = getattr(mod, "TOOLS", None)
        if tools is None:  # 兜底：扫描公开可调用对象
            tools = {n: o for n, o in vars(mod).items()
                     if not n.startswith("_") and callable(o)}
        for tname, func in tools.items():
            by_mcp_tool[(mname, tname)] = ToolEntry(mname, tname, func)
            if tname in by_name:
                ambiguous.add(tname)
                by_name[tname] = None  # 标记为歧义
            else:
                by_name[tname] = (mname, func)
    return Registry(by_mcp_tool, by_name, ambiguous)


# 进程级单例：构建注册表要 import 6 个 server 模块，略重
_REGISTRY: Registry | None = None


def registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY
