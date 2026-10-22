"""实例健康评分 —— 维度树聚合 + sigmoid 违规度（docs/DATA_MODEL_AND_ALGORITHMS.md §2.1.3）。

结构（固定不变，Tier 只影响阈值来源）：

    Health(0-100) = Σ_d  W_d · dim_score_d
    dim_score_d   = 100 × Π_s (1 − violation_s)          # 违规度连乘：一项烂就拉满维度
    violation_s   = σ( (value_s − threshold_s) / spread_s )   # 越界越深越接近 1

阈值来源随 Tier 升级：
    Tier0/1：静态告警线（metrics.LOGICAL_METRICS.default_warn/critical）
    Tier2  ：实例自身分位数基线（profile_store.get_metric_baseline，p95 为界）

可解释性：输出包含每个维度的分与每个子指标的违规明细，DBA 能对着报告吵架。
"""

from __future__ import annotations

import math

from dbcore import metrics as M

# 维度树：维度 -> (权重, [(子指标, 方向)])。方向 higher=越高越糟 / bool_reversed=期望为真。
HEALTH_TREE: dict[str, tuple[float, list[tuple[str, str]]]] = {
    "可用性": (25, [("connections_used_pct", "higher"), ("threads_active", "higher")]),
    "性能": (25, [("slow_query_count", "higher"), ("tmp_disk_ratio", "higher")]),
    "锁与并发": (15, [("lock_wait_count", "higher"), ("lock_wait_max_seconds", "higher"),
                     ("long_trx_count", "higher")]),
    "复制": (15, [("repl_lag_seconds", "higher"), ("repl_io_running", "bool_reversed"),
                 ("repl_sql_running", "bool_reversed")]),
    "容量": (10, [("disk_used_pct", "higher"), ("memory_used_pct", "higher")]),
    "配置卫生": (10, [("dead_tuples_ratio", "higher"), ("tmp_disk_tables", "higher")]),
}

_SIGMOID_K = 4.0          # 越界斜率：超阈值 1 个 spread ≈ σ(4)≈0.98


def _sigmoid(x: float) -> float:
    """标准 logistic：x=0 → 0.5，x>0 越大越接近 1，x<0 越小越接近 0。"""
    z = max(-50.0, min(50.0, _SIGMOID_K * x))
    return 1.0 / (1.0 + math.exp(-z))


def _thresholds(name: str, dialect: str) -> tuple[float | None, float | None]:
    m = M.LOGICAL_METRICS.get(name)
    if not m:
        return None, None
    return m.default_warn, m.default_critical


def _excess(value: float, threshold: float, spread: float, higher_is_worse: bool) -> float:
    """越界量（spread 数）。未越界返回 0 —— 违规度不该惩罚健康侧。"""
    d = (value - threshold) / max(spread, 1e-9)
    return d if higher_is_worse else -d


def _violation(name: str, value: float, direction: str,
               baseline: dict | None, dialect: str = "mysql") -> tuple[float, str, float | None]:
    """单个子指标的违规度。返回 (violation, 阈值来源说明, 参考阈值)。"""
    if direction == "bool_reversed":
        # 期望为 1/Yes 的布尔指标
        truthy = value in (1, 1.0, True) or (isinstance(value, str) and value.upper() in ("YES", "ON", "TRUE"))
        return (0.0 if truthy else 1.0, "布尔指标（期望为真）", 1.0)

    higher = direction == "higher"

    if baseline:
        # Tier2：以自身 p95 为界，spread 为带宽
        p95 = baseline.get("p95")
        spread = baseline.get("spread") or 0.0
        if p95 is None:
            return 0.0, "基线数据不完整，跳过", None
        sp = max(spread, abs(p95) * 0.05, 1e-9)
        raw = _excess(value, p95, sp, higher)
        v = _sigmoid(raw) if raw > 0 else 0.0
        return v, f"动态基线 p95={p95}（spread={round(sp, 2)}）", p95

    # Tier0/1：静态告警线。低于 warn 完全健康（violation=0），越界深度决定违规度。
    warn, crit = _thresholds(name, dialect)
    if warn is None:
        return 0.0, "无静态告警线，跳过", None
    spread = (crit - warn) / 2.0 if crit is not None else max(abs(warn) * 0.25, 1.0)
    raw = _excess(value, warn, spread, higher)
    v = _sigmoid(raw) if raw > 0 else 0.0
    return v, f"静态阈值 warn={warn}" + (f" / crit={crit}" if crit else ""), warn


def health_score(metrics: dict, dialect: str = "mysql",
                 baselines: dict[str, dict] | None = None) -> dict:
    """计算健康分。

    metrics: 逻辑指标快照（None 值视为未采集，跳过——不当 0）
    baselines: {metric: {p50,p95,p99,spread,samples}}，来自 profile_store.get_metric_baseline
    """
    baselines = baselines or {}
    dims = []
    total = 0.0
    used_any_baseline = False

    for dim_name, (weight, subs) in HEALTH_TREE.items():
        dim_score = 100.0
        details = []
        for name, direction in subs:
            val = metrics.get(name)
            if val is None:
                details.append({"metric": name, "violation": None,
                                "note": "未采集，不参与评分（不当 0）"})
                continue
            val = float(val)
            base = baselines.get(name)
            v, src, ref = _violation(name, val, direction, base, dialect)
            if base:
                used_any_baseline = True
            dim_score -= v * (100.0 / max(len(subs), 1))
            item = {"metric": name, "value": val, "violation": round(v, 3),
                    "threshold_source": src, "threshold": ref}
            if v >= 0.5:
                item["level"] = "critical" if v >= 0.85 else "warn"
            details.append(item)
        dim_score = max(0.0, min(100.0, dim_score))
        dims.append({"dimension": dim_name, "weight": weight,
                     "score": round(dim_score, 1),
                     "contribution": round(weight * dim_score / 100.0, 1),
                     "subs": details})
        total += weight * dim_score / 100.0

    score = int(round(total))
    return {
        "score": score,
        "grade": "优" if score >= 90 else "良" if score >= 75 else "差" if score >= 60 else "危险",
        "verdict": "pass" if score >= 90 else ("warn" if score >= 75 else "fail"),
        "dimensions": dims,
        "tier": "tier2" if used_any_baseline else "tier1",
        "worst": sorted(dims, key=lambda d: d["score"])[:2],
    }
