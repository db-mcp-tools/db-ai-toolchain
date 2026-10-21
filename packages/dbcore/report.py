"""报告渲染 —— 把结构化结果变成能交付给客户的文档。

输出的 markdown 遵守三条：
  * 先说结论和影响，再给证据（客户/领导只看前五行）
  * 每条结论下面必须能看到"为什么这么说"
  * 可执行的部分用代码块或清单，让人能直接照着做
"""

from __future__ import annotations

import json
import time
from typing import Any

from dbcore.protocol import TIERS, VERDICTS

_VERDICT_CN = {
    "pass": "通过", "warn": "需关注", "fail": "不合格",
    "block": "禁止", "unknown": "无法判定",
}
_TIER_CN = {
    "tier0": "Tier0（静态知识，无历史依赖）",
    "tier1": "Tier1（含实例真实元数据）",
    "tier2": "Tier2（含自身历史基线）",
}
_CONF_CN = {"high": "高", "medium": "中", "low": "低"}


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    if v is None:
        return "—"
    return str(v)


def render_result(result: dict | Any, max_evidence: int = 10, max_items: int = 20) -> str:
    """把任意 dbt.result/v1 信封渲染成 markdown。"""
    if not isinstance(result, dict):
        result = result.to_dict() if hasattr(result, "to_dict") else {"data": result}
    lines: list[str] = []
    tool = result.get("tool", "unknown")
    lines.append(f"## `{result.get('mcp')}.{tool}`")
    lines.append("")
    lines.append(
        f"> 结论：**{_VERDICT_CN.get(result.get('verdict'), result.get('verdict'))}**"
        f"　置信度：{_CONF_CN.get(result.get('confidence'), result.get('confidence'))}"
        f"　{_TIER_CN.get(result.get('tier'), result.get('tier'))}"
    )
    if result.get("summary"):
        lines.append(f"> {result['summary']}")
    lines.append("")

    for w in (result.get("warnings") or [])[:5]:
        lines.append(f"- ⚠ {w}")
    if result.get("warnings"):
        lines.append("")

    ev = (result.get("evidence") or [])[:max_evidence]
    if ev:
        lines.append("### 依据")
        lines.append("")
        lines.append("| 来源 | 事实 | 支撑 |")
        lines.append("|------|------|------|")
        for e in ev:
            src = str(e.get("source", "")).replace("|", "\\|")
            fact = str(e.get("fact", "")).replace("|", "\\|")[:160]
            sup = str(e.get("supports", "")).replace("|", "\\|")[:120]
            lines.append(f"| `{src}` | {fact} | {sup} |")
        lines.append("")

    data = result.get("data") or {}
    rendered_data = _render_data(data, max_items)
    if rendered_data:
        lines.append("### 详情")
        lines.append("")
        lines.append(rendered_data)

    na = result.get("next_actions") or []
    if na:
        lines.append("### 建议的下一步")
        lines.append("")
        for a in na[:6]:
            opt = "（可选）" if a.get("optional") else "（建议）"
            args = ", ".join(f"{k}={str(v)[:30]}" for k, v in (a.get("args") or {}).items())[:120]
            lines.append(f"- `{a['mcp']}.{a['tool']}` {opt} {a.get('reason', '')}")
            if args:
                lines.append(f"  - 参数：{args}")

    up = result.get("tier_upgrade")
    if up:
        lines.append("")
        lines.append("### 如何提升结论质量")
        lines.append("")
        lines.append(f"- 目标：{_TIER_CN.get(up.get('to'), up.get('to'))}")
        lines.append(f"- 需要：{up.get('need')}")
        lines.append(f"- 做法：{up.get('how')}")
        if up.get("gain"):
            lines.append(f"- 收益：{up['gain']}")
    return "\n".join(lines)


def _render_data(data: dict, max_items: int) -> str:
    """按常见字段类型做针对性渲染，其它字段走 通用表格。"""
    lines: list[str] = []
    skip = {"evidence", "next_actions"}

    if isinstance(data.get("findings"), list) and data["findings"]:
        lines.append("**问题清单**")
        lines.append("")
        lines.append("| 规则 | 严重度 | 说明 | 建议 |")
        lines.append("|------|-------|------|------|")
        for f in data["findings"][:max_items]:
            lines.append(
                f"| {f.get('rule_id')} | {f.get('severity')} | "
                f"{str(f.get('message', '')).replace('|', '/')[:100]} | "
                f"{str(f.get('suggestion', '')).replace('|', '/')[:100]} |")
        lines.append("")

    if isinstance(data.get("items"), list) and data["items"]:
        lines.append("**修正建议**")
        lines.append("")
        for i, it in enumerate(data["items"][:max_items], 1):
            if not isinstance(it, dict):
                continue
            lines.append(f"{i}. [{it.get('rule_id')}] {it.get('action', '')}")
            if it.get("fixed_sql"):
                lines.append("   ```sql")
                lines.append(f"   {it['fixed_sql']}")
                lines.append("   ```")
            if it.get("side_ddl"):
                lines.append(f"   - 配套 DDL：`{it['side_ddl']}`")
            if it.get("follow_up_sql"):
                lines.append(f"   - 验证：{it['follow_up_sql'][0][:120]}")
            if it.get("recheck"):
                rc = it["recheck"]
                if isinstance(rc, dict):
                    lines.append(f"   - 复检：{rc.get('verdict')}（{rc.get('summary', '')[:80]}）")
            lines.append("")
    if data.get("composed_sql"):
        lines.append("**合并后的可执行 SQL**")
        lines.append("")
        lines.append("```sql")
        lines.append(data["composed_sql"])
        lines.append("```")
        lines.append("")

    if isinstance(data.get("checks"), list) and data["checks"]:
        lines.append("| 检查项 | 值 | 状态 | 建议 |")
        lines.append("|-------|----|------|------|")
        for c in data["checks"][:max_items]:
            lines.append(f"| {c.get('name')} | {_fmt(c.get('value'))} | {c.get('status')} | "
                         f"{str(c.get('advice', ''))[:80]} |")
        lines.append("")

    if isinstance(data.get("steps"), list) and data["steps"]:
        lines.append("**执行计划**")
        lines.append("")
        for s in data["steps"][:max_items]:
            lines.append(f"{s.get('ordinal')}. [{s.get('kind')}] {s.get('name')}"
                         + (f" — 期望：{s.get('expect')}" if s.get("expect") else ""))
            if s.get("target"):
                lines.append(f"   ```")
                lines.append(f"   {str(s['target'])[:200]}")
                lines.append(f"   ```")
        lines.append("")

    if isinstance(data.get("hypotheses"), list) and data["hypotheses"]:
        lines.append("| 假设 | 置信度 | 证据数 |")
        lines.append("|------|-------|-------|")
        for h in data["hypotheses"][:max_items]:
            lines.append(f"| {h.get('title')} | {h.get('confidence')} | {h.get('evidence_count')} |")
        lines.append("")

    rendered_keys = {"findings", "items", "checks", "steps", "hypotheses", "composed_sql"}
    others = {k: v for k, v in data.items()
              if k not in rendered_keys and k not in skip
              and isinstance(v, (str, int, float, bool, list))}
    if others:
        lines.append("| 字段 | 值 |")
        lines.append("|------|------|")
        for k, v in list(others.items())[:max_items]:
            if isinstance(v, list):
                v = f"{len(v)} 项"
            lines.append(f"| {k} | {_fmt(v)} |")
        lines.append("")
    return "\n".join(lines)


def render_index_report(instance_id: str, profile_summary: dict, findings: dict) -> str:
    """实例体检总报告（给客户汇报用）。"""
    lines = [f"# 数据库体检报告", "",
             f"- 实例：`{instance_id}`",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 档案档位：{_TIER_CN.get(profile_summary.get('tier', 'tier0'), '')}",
             ""]
    if profile_summary:
        lines.append("## 实例概况")
        lines.append("")
        lines.append(f"- 累计观测：{profile_summary.get('total_observations', 0)} 次")
        for k in (profile_summary.get("by_kind") or [])[:8]:
            lines.append(f"  - {k['kind']}：{k['count']} 次（最近 {k['last_seen']}）")
        lines.append("")
    for name, content in (findings or {}).items():
        lines.append(f"## {name}")
        lines.append("")
        if isinstance(content, str):
            lines.append(content)
        else:
            lines.append("```json")
            lines.append(json.dumps(content, ensure_ascii=False, indent=2)[:4000])
            lines.append("```")
        lines.append("")
    return "\n".join(lines)


def to_json(obj: Any) -> str:
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
