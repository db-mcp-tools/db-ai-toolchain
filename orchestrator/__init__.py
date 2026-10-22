"""orchestrator —— 跨 MCP 编排引擎。

DESIGN.md §4 把 `next_actions` 定义为「多 Agent 编排合同」：
工具自己声明下一步调谁（可跨 MCP），本包是这份合同的运行端——
按调用链自动串起多个 MCP，不为每一对 MCP 写胶水代码。

提供两种用法：
  * 自动追链（follow）：给一个入口工具，引擎照着 next_actions 自动跑下去
  * 配方（recipe）：一份显式的、确定性的跨 MCP 流水线（适合 CI / 受控变更）

详见 orchestrator/engine.py、orchestrator/report.py。
"""
