"""根因推理引擎 —— 假设-证据-置信度模型（docs/DATA_MODEL_AND_ALGORITHMS.md §2.2）。

这是回答「客户现场数据我拿不到」问题的落地点。

与"规则命中即结论"的区别：每条结论都是一个**可证伪的假设**，带行业先验、
支撑证据、反对证据、缺失证据清单。DBA 报告的行业惯例是
「我怀疑 X，因为 A、B，已排除 C，还需要 D 才能确认」——本引擎把这个结构建模出来。

推理分三条路，按可用信息自动选择（并在结果里说明用的是哪条）：

  A. 文本路径（Tier0）：只给一句症状描述 → 指纹库检索 → 检查清单 + 成因 + 处置
  B. 快照路径（Tier1）：给一份 snapshot → 用真实数值检验每个假设 → 按证据强度排序
  C. 历史路径（Tier2）：调用方可再叠加档案基线偏离作为证据（score_evidence 可直接传入）

设计原则（三条硬约束）：
  * **可证伪**：每条假设必须带 falsified_by——"什么证据出现即推翻"。
    不给推翻条件的结论不是诊断，是玄学。
  * **证据不足 ≠ 结论**：需要的指标采不到，该检验返回 unknown 并写进 evidence_gaps，
    引导用户补证据（tier_upgrade / next_actions）。
  * **每条证据要说清来源**：evidence[].kind 区分 指纹库 / 静态基线 / 真实数值 / 元数据。
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
# 假设的先验与可证伪条件（知识域，随指纹库一起维护）
# ------------------------------------------------------------------

# 行业先验：该类故障在对应症状候选集中的常见程度（0~1）。
# 这是知识域里唯一允许被 diagnosis_event 数据长期校准的参数（调整需留审计）。
_HYPOTHESIS_PRIORS: dict[str, float] = {
    "REPL001": 0.8,   # 复制延迟/中断 —— 最常见的值守告警之一
    "REPL002": 0.5,
    "LOCK001": 0.75,  # 行锁等待 —— 高并发 OLTP 高发
    "LOCK002": 0.5,
    "CONN001": 0.7,   # 连接耗尽
    "CPU001": 0.65,   # 慢查询/CPU 打满
    "LAT002": 0.5,
    "IO001": 0.5,
    "DISK001": 0.55,
    "PG001": 0.5,
    "PG002": 0.5,
    "MEM001": 0.45,
    "DATA001": 0.4,
    "DATA002": 0.3,
    "DDL001": 0.4,
    "CFG001": 0.45,
    "TX001": 0.6,     # 长事务
    "BACKUP001": 0.35,
}

_DEFAULT_PRIOR = 0.5

# 可证伪条件：出现该证据即推翻本假设（假设-证据模型的硬要求）。
_FALSIFIED_BY: dict[str, str] = {
    "REPL001": "Seconds_Behind_Master 归零且 IO/SQL 线程均为 Yes、且延迟未再增长",
    "REPL002": "SHOW SLAVE STATUS 报 IO 线程 Yes 且无 Last_IO_Error，延迟持续为 0",
    "LOCK001": "performance_schema.data_lock_waits 为空、且锁等待指标持续为 0",
    "LOCK002": "元数据锁等待清单为空，且无长事务持有该表",
    "CONN001": "Threads_connected 远低于 max_connections 且无连接错误日志",
    "CPU001": "慢查询数为 0、threads_active 正常、且 host CPU < 60%",
    "LAT002": "执行计划显示命中索引、扫描行数与返回行数同量级",
    "IO001": "磁盘 util < 60% 且 iowait < 5%",
    "DISK001": "磁盘剩余空间回升且增速重新拟合为正但放缓",
    "PG001": "pg_stat_activity 无 idle in transaction、死元组占比 < 10%",
    "PG002": "pg_locks 无未授权锁且无 blocking_pids",
    "MEM001": "内存使用率回落到基线区间且无 OOM 日志",
    "DATA001": "抽样比对双侧结果一致",
    "DATA002": "两侧口径 signature 九类 diff 全部一致",
    "DDL001": "DDL 已完成且表结构符合预期",
    "CFG001": "参数与静态基线建议一致",
    "TX001": "innodb_trx 中无运行超过阈值的事务",
    "BACKUP001": "最新备份时间戳在窗口内且校验通过",
}


def _prior_of(sid: str) -> float:
    return _HYPOTHESIS_PRIORS.get(sid, _DEFAULT_PRIOR)


def _falsified_of(sig: Any) -> str:
    return _FALSIFIED_BY.get(sig.id, f"指纹 {sig.id} 的全部检查项通过且症状消失")


# ------------------------------------------------------------------
# 假设的检验函数（judge）：证据 → support / refute / unknown
# 每条 judge 返回 (verdict, strength, fact)。verdict ∈ support|refute|unknown
# ------------------------------------------------------------------

def _judge_metric(metrics: dict, name: str, pred, sid: str, tmpl: str):
    """指标检验：命中→support；采到了但没超阈值→弱 refute（症状不符）；
    没采到→unknown（不许当健康）。"""
    val = metrics.get(name)
    if val is None:
        return ("unknown", 0.0, f"指标 {name} 未采集（不能当作正常）", "runtime_metric",
                f"snapshot.metrics.{name}")
    try:
        v = float(val)
    except (TypeError, ValueError):
        return ("unknown", 0.0, f"指标 {name} 值非法：{val!r}", "runtime_metric",
                f"snapshot.metrics.{name}")
    thr = M.LOGICAL_METRICS.get(name)
    threshold = thr.default_warn if thr else None
    if pred(v):
        return ("support", 1.0, tmpl.format(v=v, t=threshold), "runtime_metric",
                f"snapshot.metrics.{name}")
    return ("refute", 0.3, f"{name}={v} 未超阈值{f' {threshold}' if threshold else ''}，"
            f"与该假设不符（但不排除其他路径）", "runtime_metric", f"snapshot.metrics.{name}")


def _judge_param(params: dict, dialect: str, pname: str, sid: str, tmpl: str, base_key: str, judge):
    raw = params.get(pname)
    if raw is None:
        return ("unknown", 0.0, f"参数 {pname} 未采集", "static_baseline",
                f"snapshot.params.{pname}")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return ("unknown", 0.0, f"参数 {pname} 值非法：{raw!r}", "static_baseline",
                f"snapshot.params.{pname}")
    threshold = BL.value(dialect, base_key, 0.0)
    try:
        hit = bool(judge(v, {}))
    except Exception:
        return ("unknown", 0.0, f"参数 {pname} 判定异常", "static_baseline",
                f"snapshot.params.{pname}")
    if hit:
        return ("support", 0.6, tmpl.format(
            v=int(v) if float(v).is_integer() else v,
            t=int(threshold) if float(threshold).is_integer() else threshold),
            "static_baseline", f"snapshot.params.{pname} / {BL.cite(dialect, base_key)}")
    return ("refute", 0.2, f"{pname}={v} 在建议区间内", "static_baseline",
            f"snapshot.params.{pname}")


def _judge_object(secs: dict, name: str, sid: str, msg: str):
    v = secs.get(name)
    n = len(v.get("rows", [])) if isinstance(v, dict) else 0
    if n > 0:
        return ("support", 0.5, msg % n, "metadata", f"snapshot.sections.{name}")
    if isinstance(v, dict):
        return ("refute", 0.2, f"{name} 为空", "metadata", f"snapshot.sections.{name}")
    return ("unknown", 0.0, f"结构段 {name} 未采集", "metadata", f"snapshot.sections.{name}")


# 每个假设的检验注册表：sid -> [(kind, args...)]
# kind: metric | param | object   （文字症状命中在运行时另行叠加）
def _judge_plan(sid: str) -> list[tuple]:
    plan: list[tuple] = []
    for mname, pred, s, tmpl in _METRIC_TRIGGERS:
        if s == sid:
            plan.append(("metric", mname, pred, s, tmpl))
    for pname, (s, tmpl, base_key, judge) in _PARAM_RULES.items():
        if s == sid:
            plan.append(("param", pname, s, tmpl, base_key, judge))
    for name, s, msg in _OBJECT_TRIGGERS:
        if s == sid:
            plan.append(("object", name, s, msg))
    return plan


# ------------------------------------------------------------------
# A. 纯文本路径（Tier0）
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
    raw_scores = []
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
            "prior": _prior_of(sig.id),
            "score": score,
            "confidence": "medium" if score > 1.0 else "low",
            "checks": checks,
            "causes": list(sig.causes),
            "actions": list(sig.actions),
            "escalate": sig.escalate,
            "falsified_by": _falsified_of(sig),
            "evidence_source": f"signature.{sig.id}",
        })
        raw_scores.append(score)
    total = sum(raw_scores) or 1.0
    for h in hypotheses:
        h["score_norm"] = round(h["score"] / total, 3)
    top = hypotheses[0]
    return {
        "matched": True,
        "tier": "tier0",
        "input": symptom,
        "dialect": dialect,
        "primary": top,
        "hypotheses": hypotheses,
        "evidence_sufficient": False,
        "evidence_gaps": ["尚未结合实例真实指标：以下结论全部来自静态指纹库，"
                          "证据不足以区分候选假设"],
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
# B. 快照路径（Tier1）
# ------------------------------------------------------------------

# 逻辑指标 -> 触发的指纹 ID
_METRIC_TRIGGERS: list[tuple[str, Any, str, str]] = [
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
    ("cpu_used_pct", lambda v: v >= 80, "CPU001",
     "主机 CPU 使用率 {v}% 超过 {t}%"),
    ("memory_used_pct", lambda v: v >= 85, "MEM001",
     "主机内存使用率 {v}% 超过 {t}%"),
    ("disk_used_pct", lambda v: v >= 85, "DISK001",
     "磁盘使用率 {v}% 超过 {t}%"),
    ("iowait_pct", lambda v: v >= 20, "IO001",
     "iowait {v}% 超过 {t}%，存储子系统压力大"),
]

_OBJECT_TRIGGERS: list[tuple[str, str, str]] = [
    ("tables_no_pk", "REPL001", "存在 %d 张无主键的表，主从回放时会退化为全表扫描"),
    ("redundant_indexes", "CFG001", "存在 %d 个冗余索引，写入被无谓放大"),
    ("unused_indexes", "CFG001", "存在 %d 个从未被使用的索引，建议评估后下线"),
    ("lock_waits", "LOCK001", "采集到 %d 条正在发生的锁等待"),
    ("idle_in_transaction", "PG001", "存在 %d 个 idle in transaction 会话"),
    ("bloat_candidates", "PG001", "存在 %d 张表满足膨胀嫌疑条件"),
    ("long_queries", "CPU001", "采集到 %d 条超过阈值的活跃查询"),
]


def _long_query_time_bad(v: float, _b: dict) -> bool:
    """超过 5s 就基本失去 OLTP 诊断价值（默认 10s 更糟）。"""
    return v >= 5


_PARAM_RULES: dict[str, tuple[str, str, str, Any]] = {
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

# 候选假设的来源类型说明（报告用）
_EVIDENCE_KIND_LABEL = {
    "runtime_metric": "真实指标",
    "static_baseline": "静态基线",
    "metadata": "实例元数据",
    "signature": "故障指纹库",
}


def score_evidence(results: list[tuple], prior: float) -> dict:
    """把一组 judge 结果折算成假设得分（可解释公式，§2.2）：

    score = prior × (1 + Σsupport·strength) / (1 + Σrefute·strength)
    """
    sup = sum(s for v, s, *_ in results if v == "support")
    ref = sum(s for v, s, *_ in results if v == "refute")
    unk = [r for r in results if r[0] == "unknown"]
    score = prior * (1.0 + sup) / (1.0 + ref)
    return {"score": round(score, 4), "support": round(sup, 2), "refute": round(ref, 2),
            "unknown_n": len(unk)}


def diagnose_by_snapshot(snapshot: dict, symptom: str | None = None) -> dict:
    dialect = snapshot.get("dialect", "mysql")
    metrics = snapshot.get("metrics") or {}
    params = (snapshot.get("sections") or {}).get("params") or {}
    secs = snapshot.get("sections") or {}

    candidate_sids: list[str] = []

    # --- 1) 确定候选假设集：指标/参数/结构信号 + 文本症状 ---
    for mname, pred, sid, _tmpl in _METRIC_TRIGGERS:
        val = metrics.get(mname)
        if val is None:
            continue
        try:
            if pred(float(val)):
                if sid not in candidate_sids:
                    candidate_sids.append(sid)
        except (TypeError, ValueError):
            continue
    for pname, (sid, _t, _b, judge) in _PARAM_RULES.items():
        raw = params.get(pname)
        if raw is None:
            continue
        try:
            if judge(float(raw), {}):
                if sid not in candidate_sids:
                    candidate_sids.append(sid)
        except (TypeError, ValueError, Exception):
            continue
    for name, sid, _msg in _OBJECT_TRIGGERS:
        v = secs.get(name)
        if isinstance(v, dict) and len(v.get("rows", [])) > 0 and sid not in candidate_sids:
            candidate_sids.append(sid)

    text_top = None
    if symptom:
        r = diagnose_by_text(symptom, dialect, top_n=3)
        if r.get("matched"):
            text_top = r
            for h in r["hypotheses"]:
                if h["id"] not in candidate_sids:
                    candidate_sids.append(h["id"])

    # --- 2) 对每个候选假设跑全部检验（support / refute / unknown 全都收集） ---
    all_findings: list[dict] = []
    hypotheses = []
    for sid in candidate_sids:
        sig = SG.get_signature(sid)
        if sig is None:
            continue
        results: list[tuple] = []
        for item in _judge_plan(sid):
            if item[0] == "metric":
                _, mname, pred, _s, tmpl = item
                v, s, fact, ek, src = _judge_metric(metrics, mname, pred, sid, tmpl)
            elif item[0] == "param":
                _, pname, _s, tmpl, base_key, judge = item
                v, s, fact, ek, src = _judge_param(params, dialect, pname, sid, tmpl, base_key, judge)
            else:
                _, name, _s, msg = item
                v, s, fact, ek, src = _judge_object(secs, name, sid, msg)
            results.append((v, s, fact, ek, src))
            if v != "unknown":
                all_findings.append({
                    "type": item[0], "signature_id": sid, "verdict": v,
                    "message": fact, "evidence_kind": ek, "source": src,
                })
        # 文本症状命中 → 作为一条支撑证据
        if text_top:
            for h in text_top["hypotheses"]:
                if h["id"] == sid:
                    results.append(("support", min(1.2, h["score"] * 0.5),
                                    f"症状描述命中指纹 {sid}（匹配分 {h['score']}）",
                                    "signature", f"signature.{sid}"))
                    all_findings.append({
                        "type": "text", "signature_id": sid, "verdict": "support",
                        "message": f"症状描述命中指纹 {sid}",
                        "evidence_kind": "signature", "source": f"signature.{sid}",
                    })

        sc = score_evidence(results, _prior_of(sid))
        supporting = [r for r in results if r[0] == "support"]
        refuting = [r for r in results if r[0] == "refute"]
        unknown = [r for r in results if r[0] == "unknown"]
        # 无任何信号（既无支撑也无反对也没检验）的候选剔除
        if not results or (not supporting and not refuting and len(unknown) == len(results)):
            continue
        hypotheses.append({
            "id": sid,
            "title": sig.title,
            "category": sig.category,
            "prior": _prior_of(sid),
            **sc,
            "supporting": [{"fact": r[2], "kind": r[3], "source": r[4]} for r in supporting],
            "refuting": [{"fact": r[2], "kind": r[3], "source": r[4]} for r in refuting],
            "missing": [r[2] for r in unknown],
            "falsified_by": _falsified_of(sig),
            "evidence_count": len(supporting),
            "checks": [{"name": c.name, "kind": c.kind, "target": c.target, "why": c.why}
                       for c in sig.checks],
            "causes": list(sig.causes),
            "actions": list(sig.actions),
            "escalate": sig.escalate,
        })

    # --- 3) 归一化排序 + 置信度声明 ---
    total = sum(h["score"] for h in hypotheses)
    for h in hypotheses:
        h["score_norm"] = round(h["score"] / total, 3) if total else 0.0
    hypotheses.sort(key=lambda h: -h["score_norm"])
    for h in hypotheses:
        if h["score_norm"] >= 0.6 and h["support"] >= 1.0:
            h["confidence"] = "high"
        elif h["score_norm"] >= 0.35 or h["support"] >= 0.6:
            h["confidence"] = "medium"
        else:
            h["confidence"] = "low"

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
         "supports": f"指向 {primary['title']}" if f.get("verdict") == "support"
                     else f"与 {f.get('signature_id', '')} 不符（反向证据）"}
        for f in all_findings
    ]
    gaps = sorted({m for h in hypotheses for m in h["missing"]})
    evidence_sufficient = bool(primary["supporting"]) and primary["score_norm"] >= 0.5
    conf = primary["confidence"]

    summary = (f"共 {len(all_findings)} 项信号，最高置信度假设：{primary['title']}"
               f"（{conf}，归一化得分 {primary['score_norm']}，"
               f"支撑 {len(primary['supporting'])} 条 / 反对 {len(primary['refuting'])} 条）")
    if not evidence_sufficient:
        summary += "。⚠ 证据尚不足以区分候选假设，请补充缺失证据（见 evidence_gaps）"

    return {
        "tier": "tier1",
        "dialect": dialect,
        "instance_id": snapshot.get("instance_id"),
        "primary": primary,
        "hypotheses": hypotheses,
        "findings": all_findings,
        "finding_count": len(all_findings),
        "evidence_sufficient": evidence_sufficient,
        "evidence_gaps": gaps,
        "summary": summary,
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

    lines.append("## 待验证假设（按归一化得分排序）")
    lines.append("")
    lines.append("| # | 假设 | 置信度 | 得分 | 支撑/反对 | 类别 |")
    lines.append("|---|------|-------|------|----------|------|")
    for i, h in enumerate(result.get("hypotheses", []), 1):
        cnt = h.get("evidence_count")
        cnt = len(h.get("supporting") or h.get("evidence") or []) if cnt is None else cnt
        score = h.get("score_norm", "-")
        ref = len(h.get("refuting") or [])
        lines.append(f"| {i} | {h['title']} | {h['confidence']} | {score} | {cnt}/{ref} | {h['category']} |")
    lines.append("")

    primary = result.get("primary") or {}
    if primary:
        lines.append(f"## 首选假设：{primary.get('title')}")
        lines.append("")
        sup = primary.get("supporting") or []
        ref = primary.get("refuting") or []
        if sup:
            lines.append("### 支撑证据")
            for e in sup[:12]:
                lines.append(f"- `{e['source']}` → {e['fact']}")
            lines.append("")
        if ref:
            lines.append("### 反向证据（为何置信度没有更高）")
            for e in ref[:6]:
                lines.append(f"- `{e['source']}` → {e['fact']}")
            lines.append("")
        miss = primary.get("missing") or result.get("evidence_gaps") or []
        if miss:
            lines.append("### 缺失证据（补齐后可提升置信度）")
            for m in miss[:8]:
                lines.append(f"- {m}")
            lines.append("")
        fb = primary.get("falsified_by")
        if fb:
            lines.append(f"> 可证伪条件：{fb}")
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
