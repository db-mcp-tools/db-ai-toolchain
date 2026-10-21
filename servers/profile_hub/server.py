"""profile-hub MCP —— 档案中枢。

**这是所有 MCP 的唯一数据落点。**

为什么必须单一落点：
    每个 MCP 各存一份日志 = 一堆能被单独替换的原子能力，没有复利。
    所有 MCP 往同一个库写观测 = 别人第一天拿到一条 SQL，
    你拿到的是这张表三个月的历史。**这个文件才是公司资产，MCP 只是它的入口。**

它同时提供冷启动的两条解法：
    1. tier_of() —— 如实告诉调用方当前能做什么（见 DESIGN.md §0.2）
    2. export/import_profile —— 让"脱敏的行业常识包"成为可交付物，
       新客户零历史时先导入同行业匿名基线作为 Tier1 起点

工具清单见 DESIGN.md §3.6。

运行： python -m servers.profile_hub
"""

from __future__ import annotations

import os
import time

from servers._shared import register, make_server
from dbcore import ids as IDS
from dbcore import metrics as M
from dbcore.profile_store import (
    upsert_instance, get_instance, list_instances, get_profile, instance_summary,
    record_observation, get_history, tier_of, require_tier,
    set_baseline, get_baseline, compute_baselines,
    record_audit, recent_audit,
    link_cross_instance, related_instances,
    put_dictionary, get_dictionary, list_dictionary,
    export_profile, import_profile, store_stats, store_path,
)
from dbcore.protocol import Result, ToolError

MCP_NAME = "profile-hub"
VERSION = "0.1.0"
mcp = make_server(MCP_NAME, VERSION)


def upsert_instance_tool(instance_id: str = "", url: str = "", dialect: str = "",
                         version: str = "", label: str = "", meta: dict | None = None,
                         correlation_id: str = "") -> Result:
    """注册/更新实例元数据。url 会参与脱敏处理，密码绝不落盘。"""
    iid = instance_id or (IDS.instance_id_from_dsn(url) if url else "")
    if not iid:
        raise ToolError("E_ARGS", "instance_id 与 url 至少提供一个")
    host = port = dbname = ""
    if url:
        redacted = IDS.redact_dsn(url)
        try:
            from urllib.parse import urlparse
            p = urlparse(url if "://" in url else "mysql://" + url)
            host, port, dbname = p.hostname or "", str(p.port or ""), (p.path or "").lstrip("/")
        except Exception:
            pass
    d = upsert_instance(iid, dialect=dialect, version=version, host=host,
                        port=port, database_name=dbname, label=label, meta=meta)
    return Result(
        ok=True, mcp=MCP_NAME, tool="upsert_instance", data=d,
        verdict="pass", confidence="high", tier=d.get("tier", "tier1"),
        summary=f"实例 {iid} 已注册，当前档位 {d.get('tier')}",
        instance_id=iid, correlation_id=correlation_id,
    )


def record_observation_tool(instance_id: str, kind: str, payload: dict,
                            correlation_id: str = "") -> Result:
    """写入一次观测。**这是 Tier0 → Tier2 升级的唯一写入点**"""
    if not instance_id:
        raise ToolError("E_ARGS", "instance_id 不能为空")
    if not isinstance(payload, dict):
        raise ToolError("E_ARGS", "payload 必须是 dict")
    before = tier_of(instance_id)
    oid = record_observation(instance_id, kind, payload)
    after = tier_of(instance_id)
    res = Result(
        ok=True, mcp=MCP_NAME, tool="record_observation",
        data={"observation_id": oid, "tier_before": before, "tier_after": after,
              "summary": instance_summary(instance_id)},
        verdict="pass", confidence="high", tier=after,
        summary=f"已记录观测 #{oid}（kind={kind}）"
                + (f"，档位 {before} → {after}" if before != after else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )
    res.add_evidence("history", f"observation.{kind}", f"id={oid}", "本次写入的观测")
    if after != "tier2":
        res.tier_upgrade = {"to": "tier2", "need": "至少 2 次观测且跨 >= 7 天",
                            "how": "持续调用本工具或 dbwatch.capture_snapshot",
                            "gain": "可计算自身基线，做趋势与异常检测"}
    return res


def get_instance_profile(instance_id: str, correlation_id: str = "") -> Result:
    """实例全貌 —— 给 LLM 和客户看的完整画像。"""
    prof = get_profile(instance_id)
    if not prof.get("known"):
        res = Result(
            ok=True, mcp=MCP_NAME, tool="get_instance_profile", data=prof,
            verdict="unknown", confidence="low", tier="tier0",
            summary=f"档案库中没有实例 {instance_id} 的记录",
            instance_id=instance_id, correlation_id=correlation_id,
        )
        res.tier_upgrade = {"to": "tier1", "need": "至少一次快照",
                            "how": "dbwatch.capture_snapshot（连库）或 record_observation 写入",
                            "gain": "之后该实例的所有诊断都带真实元数据"}
        return res
    return Result(
        ok=True, mcp=MCP_NAME, tool="get_instance_profile", data=prof,
        verdict="pass", confidence="high", tier=prof.get("tier", "tier1"),
        summary=f"{prof['summary']['total_observations']} 次观测，档位 {prof.get('tier')}",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def query_observations(instance_id: str, kind: str | None = None, limit: int = 50,
                       days: int | None = None, correlation_id: str = "") -> Result:
    since = time.time() - days * 86400 if days else None
    rows = get_history(instance_id, kind=kind, limit=limit, since=since)
    return Result(
        ok=True, mcp=MCP_NAME, tool="query_observations",
        data={"items": rows, "count": len(rows), "tier": tier_of(instance_id, kind)},
        verdict="pass", confidence="high", tier=tier_of(instance_id, kind),
        summary=f"查询到 {len(rows)} 条观测" + (f"（近 {days} 天）" if days else ""),
        instance_id=instance_id, correlation_id=correlation_id,
    )


def get_history_tool(instance_id: str, kind: str, limit: int = 30,
                     correlation_id: str = "") -> Result:
    rows = get_history(instance_id, kind=kind, limit=limit)
    return Result(
        ok=True, mcp=MCP_NAME, tool="get_history",
        data={"items": rows, "count": len(rows)},
        verdict="pass", confidence="high", tier=tier_of(instance_id, kind),
        summary=f"{kind} 类观测 {len(rows)} 条",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def set_baseline_tool(instance_id: str, metric: str, value: float,
                      correlation_id: str = "") -> Result:
    set_baseline(instance_id, metric, value)
    return Result(
        ok=True, mcp=MCP_NAME, tool="set_baseline",
        data={"instance_id": instance_id, "metric": metric, "value": value},
        verdict="pass", confidence="high", tier="tier2",
        summary=f"基线 {metric}={value} 已设置",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def get_baseline_tool(instance_id: str, metric: str | None = None,
                      correlation_id: str = "") -> Result:
    if metric:
        b = get_baseline(instance_id, metric)
        if b is None:
            res = Result(
                ok=True, mcp=MCP_NAME, tool="get_baseline", data={"baselines": {}},
                verdict="unknown", confidence="low", tier="tier0",
                summary=f"实例 {instance_id} 尚无 {metric} 的基线",
                instance_id=instance_id, correlation_id=correlation_id,
            )
            res.tier_upgrade = {"to": "tier2", "need": "同一指标至少 2 个样本",
                                "how": "调用 compute_baselines 或持续采集",
                                "gain": "得到该实例自身的常态值，而非通用阈值"}
            return res
        return Result(
            ok=True, mcp=MCP_NAME, tool="get_baseline", data={"baselines": {metric: b}},
            verdict="pass", confidence="high", tier="tier2",
            summary=f"{metric} 基线 {b['value']}",
            instance_id=instance_id, correlation_id=correlation_id,
        )
    from dbcore.profile_store import get_profile
    prof = get_profile(instance_id)
    return Result(
        ok=True, mcp=MCP_NAME, tool="get_baseline",
        data={"baselines": prof.get("baselines", {})},
        verdict="pass", confidence="high", tier=tier_of(instance_id),
        summary=f"共 {len(prof.get('baselines', {}))} 项基线",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def compute_baselines_tool(instance_id: str, window_days: int = 30,
                           correlation_id: str = "") -> Result:
    """从已有观测计算基线。**单点不算基线**，样本不足会跳过而不是编造。"""
    out = compute_baselines(instance_id, window_days)
    return Result(
        ok=True, mcp=MCP_NAME, tool="compute_baselines",
        data={"baselines": out, "count": len(out), "window_days": window_days},
        verdict="pass" if out else "warn", confidence="high" if out else "low",
        tier="tier2" if out else "tier1",
        summary=f"计算出 {len(out)} 项基线"
                if out else "样本不足（每项需要 >= 2 个观测），未能计算任何基线",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def record_audit_tool(tool: str, summary: str, instance_id: str = "",
                      correlation_id: str = "") -> Result:
    record_audit(tool, instance_id, summary, correlation_id)
    return Result(
        ok=True, mcp=MCP_NAME, tool="record_audit",
        data={"tool": tool, "summary": summary[:200]},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"审计留痕已写入：{tool}",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def query_audit(instance_id: str | None = None, limit: int = 20,
                correlation_id: str = "") -> Result:
    rows = recent_audit(limit=limit, instance_id=instance_id)
    return Result(
        ok=True, mcp=MCP_NAME, tool="query_audit",
        data={"items": rows, "count": len(rows)},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"最近 {len(rows)} 条审计记录",
        instance_id=instance_id or "", correlation_id=correlation_id,
    )


def link_cross_instance_tool(source_instance: str, target_instance: str,
                             relation: str = "same_business",
                             meta: dict | None = None,
                             correlation_id: str = "") -> Result:
    """异构实例关联（同一业务跨多个引擎）—— 这是通用工具做不到的事。"""
    link_cross_instance(source_instance, target_instance, relation, meta)
    return Result(
        ok=True, mcp=MCP_NAME, tool="link_cross_instance",
        data={"source": source_instance, "target": target_instance, "relation": relation},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"已关联 {source_instance} ↔ {target_instance}（{relation}）",
        instance_id=source_instance, correlation_id=correlation_id,
    )


def get_related_instances(instance_id: str, correlation_id: str = "") -> Result:
    rows = related_instances(instance_id)
    return Result(
        ok=True, mcp=MCP_NAME, tool="get_related_instances",
        data={"items": rows, "count": len(rows)},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"{len(rows)} 个关联实例",
        instance_id=instance_id, correlation_id=correlation_id,
    )


def export_profile_tool(instance_id: str | None = None,
                        include_observations: bool = True,
                        correlation_id: str = "") -> Result:
    """★ 导出脱敏知识包。

    这是让"经验"变成可交付实体的通道：
    同行业几十个实例的中位数，打包给新客户做 Tier1 起点。
    """
    data = export_profile(instance_id, include_observations=include_observations)
    size = len(str(data))
    return Result(
        ok=True, mcp=MCP_NAME, tool="export_profile", data=data,
        verdict="pass", confidence="high", tier="tier2",
        summary=f"已导出知识包：{len(data.get('instances', []))} 实例 / "
                f"{len(data.get('baselines', []))} 基线 / "
                f"{len(data.get('observations', []))} 观测（约 {size // 1024}KB）",
        instance_id=instance_id or "", correlation_id=correlation_id,
    )


def import_profile_tool(profile: dict, remap_to: str | None = None,
                        correlation_id: str = "") -> Result:
    """★ 导入知识包 —— 冷启动的第二条解法。

    remap_to: 把包里的匿名基线挂到当前实例名下，立即获得 Tier1/Tier2 能力。
    """
    try:
        stats = import_profile(profile, remap_to=remap_to)
    except ValueError as exc:
        raise ToolError("E_ARGS", str(exc))
    return Result(
        ok=True, mcp=MCP_NAME, tool="import_profile", data={"stats": stats, "remap_to": remap_to},
        verdict="pass", confidence="high", tier=tier_of(remap_to) if remap_to else "tier1",
        summary=f"导入完成：{stats['baselines']} 基线 / {stats['observations']} 观测 / "
                f"{stats['dictionary']} 字典项"
                + (f"，已挂载到 {remap_to}" if remap_to else "（未挂载到具体实例）"),
        instance_id=remap_to or "", correlation_id=correlation_id,
    )


def store_stats_tool(correlation_id: str = "") -> Result:
    st = store_stats()
    return Result(
        ok=True, mcp=MCP_NAME, tool="store_stats", data=st,
        verdict="pass", confidence="high", tier="tier1",
        summary=f"档案库 {st['size_mb']}MB，"
                f"观测 {st['counts']['observations']} 条，实例 {st['counts']['instances']} 个",
        correlation_id=correlation_id,
    )


def list_instances_tool(limit: int = 100, correlation_id: str = "") -> Result:
    rows = list_instances(limit=limit)
    return Result(
        ok=True, mcp=MCP_NAME, tool="list_instances",
        data={"items": rows, "count": len(rows)},
        verdict="pass", confidence="high", tier="tier1",
        summary=f"档案库中共 {len(rows)} 个实例",
        correlation_id=correlation_id,
    )


def list_metrics_tool(dialect: str = "mysql", correlation_id: str = "") -> Result:
    """列出某引擎支持的逻辑指标，方便确认能采到什么。"""
    return Result(
        ok=True, mcp=MCP_NAME, tool="list_metrics",
        data={"supported": M.supported_metrics(dialect), "all": M.list_metrics()},
        verdict="pass", confidence="high", tier="tier0",
        summary=f"{dialect} 支持 {len(M.supported_metrics(dialect))} 项逻辑指标",
        correlation_id=correlation_id,
    )


def tier_of_tool(instance_id: str, kind: str | None = None,
                 correlation_id: str = "") -> Result:
    """查询实例当前的能力档位。**要求 Tier2 的能力会用它来明确报错，而不是悄悄降级**"""
    t = tier_of(instance_id, kind)
    summary = instance_summary(instance_id)
    guide = {
        "tier0": "无档案：只能用静态知识做 Tier0 诊断（仍可交付）",
        "tier1": "有元数据/至少一次快照：诊断可结合真实数值",
        "tier2": "有跨时间积累：可做趋势、漂移、容量预测",
    }[t]
    return Result(
        ok=True, mcp=MCP_NAME, tool="tier_of",
        data={"tier": t, "guide": guide, "summary": summary},
        verdict="pass", confidence="high", tier=t,
        summary=f"{t}：{guide}",
        instance_id=instance_id, correlation_id=correlation_id,
    )


TOOLS = {
    "upsert_instance": upsert_instance_tool,
    "record_observation": record_observation_tool,
    "get_instance_profile": get_instance_profile,
    "query_observations": query_observations,
    "get_history": get_history_tool,
    "set_baseline": set_baseline_tool,
    "get_baseline": get_baseline_tool,
    "compute_baselines": compute_baselines_tool,
    "record_audit": record_audit_tool,
    "query_audit": query_audit,
    "link_cross_instance": link_cross_instance_tool,
    "get_related_instances": get_related_instances,
    "export_profile": export_profile_tool,
    "import_profile": import_profile_tool,
    "list_instances": list_instances_tool,
    "list_metrics": list_metrics_tool,
    "tier_of": tier_of_tool,
    "store_stats": store_stats_tool,
}

for _n, _f in TOOLS.items():
    register(mcp, MCP_NAME, _f, _n)


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
