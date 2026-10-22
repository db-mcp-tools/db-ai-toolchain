"""把 ExecutionTrace 渲染成可读的编排报告（markdown）。

先给最终结论，再给调用链，最后给遗留的、需要人决策的后续动作——
和 dbcore.report.render_result 的「先结论后证据」风格保持一致。
"""

from __future__ import annotations

from typing import Any

from .engine import ExecutionTrace, Step

_VERDICT_BADGE = {
    "pass": "✅ pass", "warn": "⚠️ warn", "fail": "❌ fail",
    "block": "⛔ block", "unknown": "❓ unknown",
}


def render_trace(trace: ExecutionTrace, title: str = "编排执行报告") -> str:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    fv = trace.final_verdict()
    lines.append(f"> 最终结论：**{_VERDICT_BADGE.get(fv, fv)}**　"
                 f"置信度：{trace.final_confidence()}　"
                 f"Tier：{trace.final_tier()}　"
                 f"链路步数：{len(trace.steps)}")
    lines.append("")
    lines.append(f"- 关联 ID：`{trace.correlation_id}`")
    if trace.truncated:
        lines.append("- ⚠️ 已达到最大步数上限，链路被截断（可能存在未终止的 next_actions 环）")
    lines.append("")

    # ---- 调用链 ----
    lines.append("## 调用链")
    lines.append("")
    for s in trace.steps:
        if s.skipped:
            lines.append(f"{s.index}. ⏭️ `{s.mcp}.{s.tool}` —— 重复执行已剪枝（去重）")
            continue
        badge = _VERDICT_BADGE.get(fv if not s.result else s.result.verdict,
                                   s.result.verdict if s.result else "?")
        edge = f"（← 第 {s.parent} 步触发）" if s.parent is not None else "（入口）"
        reason = f" ｜ {s.reason}" if s.reason else ""
        if not s.ok:
            lines.append(f"{s.index}. ❌ `{s.mcp}.{s.tool}` {edge}{reason}")
            lines.append(f"   - 错误：{s.error_code} — {s.error}")
        else:
            lines.append(f"{s.index}. {badge} `{s.mcp}.{s.tool}` {edge}{reason}")
            if s.result and s.result.summary:
                lines.append(f"   - {s.result.summary}")
            if s.result and s.result.tier and s.result.tier != "tier0":
                lines.append(f"   - Tier：{s.result.tier}")
        if s.took_ms:
            lines.append(f"   - 耗时：{s.took_ms} ms")

    # ---- 关键证据（汇总每步 evidence 前两条）----
    ev = _collect_evidence(trace)
    if ev:
        lines.append("")
        lines.append("## 关键证据")
        lines.append("")
        for e in ev:
            lines.append(f"- `{e['source']}`：{e['fact']}"
                         + (f" ｜ {e['supports']}" if e.get("supports") else ""))

    # ---- 告警 ----
    warns = trace.warnings()
    if warns:
        lines.append("")
        lines.append("## 告警")
        lines.append("")
        for w in warns:
            lines.append(f"- ⚠️ {w}")

    # ---- 遗留后续动作 ----
    defer = trace.defused_next_actions()
    if defer:
        lines.append("")
        lines.append("## 建议后续（本轮未自动执行）")
        lines.append("")
        for d in sorted(defer, key=lambda x: x["priority"]):
            opt = "可选" if d["optional"] else "建议"
            lines.append(f"- [{opt}] `{d['mcp']}.{d['tool']}`"
                         + (f" —— {d['reason']}" if d["reason"] else "")
                         + f"  （由第 {d['from_step']} 步建议）")

    lines.append("")
    return "\n".join(lines)


def _collect_evidence(trace: ExecutionTrace) -> list[dict]:
    out: list[dict] = []
    for s in trace.steps:
        if not s.result:
            continue
        for e in s.result.evidence[:2]:
            ev = e.to_dict() if hasattr(e, "to_dict") else e
            out.append(ev)
        if len(out) >= 12:
            break
    return out
