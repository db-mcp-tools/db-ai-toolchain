"""根因推理引擎 —— Tier0 可用。

这是回答「客户现场数据我拿不到」问题的落地点。

推理分三条路，按可用信息自动选择（并在结果里说明用的是哪条）：

  A. 文本路径（Tier0）：只给一句症状描述 → 指纹库检索 → 检查清单 + 成因 + 处置
  B. 快照路径（Tier1）：给一份 snapshot → 用真实数值交叉指纹库 → 收敛假设、排出优先级
  C. 历史路径（Tier2）：再叠加档案里的历史观测 → 趋势判断、回归定位

设计原则：
  * **不许假装有历史**。Tier0 时所有跟"趋势/环比"有关的字段必须是 None 并给出 tier_upgrade 指引。
  * **每条证据要说清来源**。evidence[].kind 区分 指纹库 / 静态基线 / 真实数值。
  * **给出假设而不是断言**。多条候选根因排序输出，附"如何证伪"的检查项。
"""

from __future__ import annotations

import time
from typing import Any

from dbcore import baselines as BL
from dbcore import metrics as M
from dbcore import signature as SG


def _now() -> dict:
    ts = int(time.time())
    return {"ts": ts, "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))}


# ------------------------------------------------------------------
# A. 纯文本路径
# ------------------------------------------------------------------


def diagnose_by_text(symptom: str, dialect: str | None = None, top_n: int = 3) -> dict:
    matches = SG.match_symptom(symptom, dialect, top_n=top_n)
    if not matches:
        return {
            "matched": False,
            "message": "指纹库中没有匹配项，请换个说法（例如：'主从延迟突增'、'锁等待超时'、'CPU 打满'）",
            "known_symptoms": sorted({s for sig in SG.all_signatures() for s in sig.symptoms})[:60],
            "tier": "tier0",
        }
    hypotheses = []
    for sig, score in matches:
        checks = [
            {"name": c.name, "kind": c.kind, "target": c.target, "why": c.why,
             "applies": (dialect is None or not c.dialects or dialect in c.dialects)}
            for c in sig.checks
        ]
        hypotheses.append({
            "id": sig.id,
            "title": sig.title,
            "category": sig.category,
            "score": score,
            "confidence": "medium" if score > 1.0 else "low",
            "checks": checks,
            "causes": list(sig.causes),
            "actions": list(sig.actions),
            "escalate": sig.escalate,
            "evidence_source": f"signature.{sig.id}",
        })
    top = hypotheses[0]
    return {
        "matched": True,
        "tier": "tier0",
        "input": symptom,
        "dialect": dialect,
        "primary": top,
        "hypotheses": hypotheses,
        "immediate_actions": [a for a in top["actions"] if a.startswith("【止损】")] or top["actions"][:2],
        "note": "当前为 Tier0：结论来自静态故障指纹库，尚未结合该实例真实指标。",
        "tier_upgrade": {
            "to": "tier1",
            "need": "一份现场快照",
            "how": "dbwatch.capture_snapshot（连库），或让客户粘贴 bootstrap 脚本输出后用 offline 模式导入",
            "gain": "可以用真实数值证实/证伪假设，并把候选项收敛到 1~2 条",
        },
        **_now(),
    }


# ------------------------------------------------------------------
# B. 快照路径
# ------------------------------------------------------------------

# 逻辑指标 -> 触发的指纹 ID
_METRIC_TRIGGERS: list[tuple[str, callable, str, str]] = [
    ("connections_used_pct", lambda v: v >= 80, "CONN001",
     "连接使用率 {v}% 超过基线 {t}%"),
    ("threads_active", lambda v: v >= 50, "CPU001",
     "活跃会话 {v} 超过基线 {t}，通常伴随慢查询堆积"),
    ("long_trx_count", lambda v: v >= 1, "TX001",
     "存在 {v} 个长事务，是锁等待与 DDL 阻塞的常见根因"),
    ("longest_trx_seconds", lambda v: v >= 30, "TX001",
     "最长事务已运行 {v}s"),
    ("idle_in_transaction", lambda v: v >= 1, "PG001",
     "存在 {v} 个 idle in transaction 会话，会阻塞 vacuum 回收死元组"),
    ("lock_wait_count", lambda v: v >= 1, "LOCK001",
     "当前有 {v} 个锁等待"),
    ("lock_wait_max_seconds", lambda v: v >= 3, "LOCK001",
     "最长锁等待已达 {v}s"),
    ("repl_lag_seconds", lambda v: v >= 10, "REPL001",
     "复制延迟 {v}s 超过告警阈值 {t}s"),
    ("dead_tuples_ratio", lambda v: v >= 20, "PG001",
     "死元组占比 {v}% 超过 {t}%，表膨胀风险"),
    ("repl_slot_lag_bytes", lambda v: v >= 16 * 1024 * 1024, "PG001",
     "复制槽积压 {v} 字节，会阻止 vacuum 回收"),
    ("slow_query_count", lambda v: v >= 100, "CPU001",
     "慢查询累计 {v} 次，需提取 TOP SQL"),
    ("tmp_disk_ratio", lambda v: v >= 25, "CPU001",
     "临时表落盘率 {v}% 超过 {t}%"),
]

def _long_query_time_bad(v: float, _b: dict) -> bool:
    """超过 5s 就基本失去 OLTP 诊断价值（默认 10s 更糟）。"""
    return v >= 5


_PARAM_RULES: dict[str, tuple[str, str, str, callable]] = {
    # 参数名: (指纹ID, 问题模板, 基线引用键, 判定函数)
    "long_query_time": (
        "CPU001", "long_query_time={v}s，显著高于 OLTP 建议值 {t}s，大量有价值的慢查询被漏掉",
        "long_query_time_recommended", _long_query_time_bad,
    ),
    "innodb_lock_wait_timeout": (
        "LOCK001", "innodb_lock_wait_timeout={v}s，高于建议区间 {t}s，锁问题会被放大成雪崩",
        "lock_wait_timeout_suggest", lambda v, _b: v > 30,
    ),
    "max_connections": (
        "CONN001", "max_connections={v}，高于经验上限 {t}，连接多不代表吞吐高",
        "max_connections_recommended", lambda v, _b: v > 5000,
    ),
    "sync_binlog": (
        "IO001", "sync_binlog={v} 配合双 1 时每次提交两次 fsync，写密集场景 IO 压力显著",
        "innodb_io_capacity_ssd", lambda v, _b: v == 1,
    ),
}


def diagnose_by_snapshot(snapshot: dict, symptom: str | None = None) -> dict:
    dialect = snapshot.get("dialect", "mysql")
    metrics = snapshot.get("metrics") or {}
    params = (snapshot.get("sections") or {}).get("params") or {}

    findings: list[dict] = []
    triggered: dict[str, dict] = {}

    # --- 1) 真实指标触发 ---
    for mname, pred, sid, tmpl in _METRIC_TRIGGERS:
        val = metrics.get(mname)
        if val is None:
            continue                                    # 关键：没采到就是不知道，不能当健康
        try:
            if not pred(float(val)):
                continue
        except (TypeError, ValueError):
            continue
        thr = M.LOGICAL_METRICS.get(mname)
        threshold = thr.default_warn if thr else None
        findings.append({
            "type": "metric",
            "metric": mname,
            "value": val,
            "threshold": threshold,
            "signature_id": sid,
            "message": tmpl.format(v=val, t=threshold),
            "evidence_kind": "runtime_metric",
            "source": f"snapshot.metrics.{mname}",
        })
        triggered.setdefault(sid, {"metric_hits": []})["metric_hits"].append(mname)

    # --- 2) 参数触发 ---
    for pname, (sid, tmpl, base_key, judge) in _PARAM_RULES.items():
        raw = params.get(pname)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        threshold = BL.value(dialect, base_key, 0.0)
        try:
            if not judge(val, {}):
                continue
        except Exception:
            continue
        findings.append({
            "type": "param",
            "param": pname,
            "value": val,
            "threshold": threshold or None,
            "signature_id": sid,
            "message": tmpl.format(v=int(val) if float(val).is_integer() else val,
                                   t=int(threshold) if float(threshold).is_integer() else threshold),
            "evidence_kind": "static_baseline",
            "source": f"snapshot.params.{pname} / {BL.cite(dialect, base_key)}",
        })
        triggered.setdefault(sid, {"param_hits": []})["param_hits"].append(pname)

    # --- 3) 结构性问题触发（来自采集到的对象） ---
    secs = snapshot.get("sections") or {}

    def rowcount(name: str) -> int:
        v = secs.get(name)
        return len(v.get("rows", [])) if isinstance(v, dict) else 0

    for name, sid, msg in (
        ("tables_no_pk", "REPL001", "存在 %d 张无主键的表，主从回放时会退化为全表扫描"),
        ("redundant_indexes", "CFG001", "存在 %d 个冗余索引，写入被无谓放大"),
        ("unused_indexes", "CFG001", "存在 %d 个从未被使用的索引，建议评估后下线"),
        ("lock_waits", "LOCK001", "采集到 %d 条正在发生的锁等待"),
        ("idle_in_transaction", "PG001", "存在 %d 个 idle in transaction 会话"),
        ("bloat_candidates", "PG001", "存在 %d 张表满足膨胀嫌疑条件"),
        ("long_queries", "CPU001", "采集到 %d 条超过阈值的活跃查询"),
    ):
        n = rowcount(name)
        if n <= 0:
            continue
        findings.append({
            "type": "object",
            "section": name,
            "count": n,
            "signature_id": sid,
            "message": msg % n,
            "evidence_kind": "metadata",
            "source": f"snapshot.sections.{name}",
        })
        triggered.setdefault(sid, {"object_hits": []})["object_hits"].append(name)

    # --- 4) 文本症状补充 ---
    text_top = None
    if symptom:
        r = diagnose_by_text(symptom, dialect, top_n=3)
        if r.get("matched"):
            text_top = r
            for h in r["hypotheses"]:
                triggered.setdefault(h["id"], {"text_hit": h["score"]})["text_hit"] = h["score"]

    # --- 5) 假设排序 ---
    hypotheses = []
    for sid, hits in triggered.items():
        sig = SG.get_signature(sid)
        if sig is None:
            continue
        score = 0.0
        score += len(hits.get("metric_hits", [])) * 1.0
        score += len(hits.get("param_hits", [])) * 0.6
        score += len(hits.get("object_hits", [])) * 0.5
        score += (hits.get("text_hit") or 0) * 1.2
        related = [f for f in findings if f.get("signature_id") == sid]
        hypotheses.append({
            "id": sid,
            "title": sig.title,
            "category": sig.category,
            "score": round(score, 2),
            "confidence": "high" if score >= 1.5 else ("medium" if score >= 0.8 else "low"),
            "evidence_count": len(related),
            "evidence": related,
            "checks": [{"name": c.name, "kind": c.kind, "target": c.target, "why": c.why}
                       for c in sig.checks],
            "causes": list(sig.causes),
            "actions": list(sig.actions),
            "escalate": sig.escalate,
        })
    hypotheses.sort(key=lambda h: -h["score"])

    if not hypotheses:
        health = M.evaluate({k: v for k, v in metrics.items() if v is not None})
        return {
            "tier": "tier1",
            "dialect": dialect,
            "instance_id": snapshot.get("instance_id"),
            "hypotheses": [],
            "findings": [],
            "alerts": health,
            "verdict": "no_signal",
            "summary": "本次快照未发现超过阈值的信号。" +
                       ("不过以下指标位于告警区间：" + "、".join(a["metric"] for a in health)
                        if health else ""),
            "note": "没有命中不等于没有问题：静态阈值只能覆盖已知模式，"
                    "持续采集后的 Tier2 回归检测才能发现偏离自身基线的异常。",
            **_now(),
        }

    primary = hypotheses[0]
    evidence = [
        {"kind": f.get("evidence_kind", "runtime_metric"),
         "source": f.get("source", ""),
         "fact": f["message"],
         "supports": f"指向 {primary['title']}"}
        for f in findings
    ]
    return {
        "tier": "tier1",
        "dialect": dialect,
        "instance_id": snapshot.get("instance_id"),
        "primary": primary,
        "hypotheses": hypotheses,
        "findings": findings,
        "finding_count": len(findings),
        "summary": f"共 {len(findings)} 项信号，最高置信度假设：{primary['title']}"
                   f"（{primary['confidence']} confidence，{primary['evidence_count']} 条证据）",
        "immediate_actions": [a for a in primary["actions"] if a.startswith("【止损】")]
                             or primary["actions"][:2],
        "evidence": evidence,
        "text_match": text_top,
        "tier_upgrade": {
            "to": "tier2",
            "need": "至少 2 次以上快照（>7 天更佳）形成自身基线",
            "how": "dbwatch.capture_snapshot 定期执行，写入共享档案",
            "gain": "可以发现偏离自身常态的异常（静态阈值发现不了），并支持容量预测",
        },
        **_now(),
    }


def diagnose(snapshot: dict | None = None, symptom: str | None = None,
             dialect: str | None = None) -> dict:
    """统一入口：有快照走 Tier1，只有文本走 Tier0。

    ★ 这个函数就是"第一天就能给客户交付"的能力。
    """
    if isinstance(snapshot, dict) and snapshot:
        return diagnose_by_snapshot(snapshot, symptom)
    if symptom:
        return diagnose_by_text(symptom, dialect)
    raise ValueError("diagnose 需要 snapshot 或 symptom 至少一个入参")


# ------------------------------------------------------------------
# 报告渲染
# ------------------------------------------------------------------


def render_diagnosis(result: dict) -> str:
    """渲染成人能直接读的诊断报告（可交付给客户）。"""
    lines: list[str] = []
    tier = result.get("tier", "tier0")
    lines.append(f"# 数据库诊断报告（{tier}）")
    lines.append("")
    lines.append(f"- 实例：`{result.get('instance_id', '-')}`")
    lines.append(f"- 方言：{result.get('dialect', '-')}")
    lines.append(f"- 时间：{result.get('iso', '-')}")
    if result.get("summary"):
        lines.append(f"- 结论：**{result['summary']}**")
    lines.append("")

    if result.get("verdict") == "no_signal":
        lines.append("## 未检出异常信号")
        lines.append("")
        for a in result.get("alerts", []):
            lines.append(f"- `{a['metric']}` = {a['value']}（{a['level']}，阈值 {a['threshold']}）")
        if result.get("note"):
            lines.append("")
            lines.append(f"> {result['note']}")
        return "\n".join(lines)

    lines.append("## 待验证假设（按置信度排序）")
    lines.append("")
    lines.append("| # | 假设 | 置信度 | 证据数 | 类别 |")
    lines.append("|---|------|-------|-------|------|")
    for i, h in enumerate(result.get("hypotheses", []), 1):
        cnt = h.get("evidence_count")
        cnt = len(h.get("evidence") or []) if cnt is None else cnt
        lines.append(f"| {i} | {h['title']} | {h['confidence']} | {cnt} | {h['category']} |")
    lines.append("")

    primary = result.get("primary") or {}
    if primary:
        lines.append(f"## 首选假设：{primary.get('title')}")
        lines.append("")
        if result.get("evidence"):
            lines.append("### 支撑证据")
            for e in result["evidence"][:12]:
                lines.append(f"- `{e['source']}` → {e['fact']}")
            lines.append("")
        lines.append("### 建议立即执行")
        for a in result.get("immediate_actions", []):
            lines.append(f"1. {a}")
        lines.append("")
        if primary.get("checks"):
            lines.append("### 验证清单（按顺序取证）")
            for i, c in enumerate(primary["checks"], 1):
                applies = "" if c.get("target") else "  *(人工检查)*"
                lines.append(f"{i}. **{c['name']}** — {c.get('why') or '确认该维度状态'}{applies}")
                if c.get("target"):
                    lines.append(f"   ```sql\n   {c['target']}\n   ```")
            lines.append("")
        if primary.get("causes"):
            lines.append("### 常见成因（按出现频率排序）")
            for c in primary["causes"]:
                lines.append(f"- {c}")
            lines.append("")
        if primary.get("actions"):
            lines.append("### 处置建议")
            for a in primary["actions"]:
                lines.append(f"- {a}")
            lines.append("")
        if primary.get("escalate"):
            lines.append(f"> ⚠ 升级标准：{primary['escalate']}")
            lines.append("")

    up = result.get("tier_upgrade")
    if up:
        lines.append("---")
        lines.append("")
        lines.append(f"### 如何提升为 {up['to']}")
        lines.append(f"- 需要：{up['need']}")
        lines.append(f"- 做法：{up['how']}")
        lines.append(f"- 收益：{up['gain']}")
    return "\n".join(lines)
