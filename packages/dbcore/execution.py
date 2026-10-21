"""变更执行状态机 —— 从「给建议」跨到「敢执行」的那一层。

通用工具之所以不敢执行变更，不是技术做不到，是没人能回答这三个问题：
    1. 这个变更会影响什么？（影响面）
    2. 失败了怎么回滚？（回滚点）
    3. 谁批准的？（责任链）

所以执行层的产品形态不是"自动执行"，而是**带检查点的受控执行**：

    draft ──precheck──> prechecked ──dry_run──> dryrun ──shadow──> shadow
                                                                     │
                                            ┌────────────────────────┘
                                            ▼
                                         canary ──apply──> applied
                                            │                 │
                                            └──rollback────────┴──> rolled_back
    任一环节 ──abort──> aborted

硬性约束：
  * 没有 rollback_plan 的变更不允许进入 dryrun 之后的状态
  * 每次状态迁移必须写审计（谁、何时、理由、证据）
  * L3 风险的变更必须经过 explicit approval（记录 approver）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from dbcore import baselines as BL
from dbcore.profile_store import save_change, load_change, list_changes  # noqa: F401

STATES = ("draft", "prechecked", "dryrun", "shadow", "canary", "applied", "rolled_back", "aborted")

ALLOWED: dict[str, tuple[str, ...]] = {
    "draft": ("prechecked", "aborted"),
    "prechecked": ("dryrun", "canary", "applied", "aborted"),
    "dryrun": ("shadow", "canary", "applied", "aborted"),
    "shadow": ("canary", "applied", "aborted"),
    "canary": ("applied", "rolled_back", "aborted"),
    "applied": ("rolled_back",),
    "rolled_back": (),
    "aborted": (),
}

# 哪些状态跃迁需要审批人
REQUIRES_APPROVAL = {"canary", "applied", "rolled_back"}

# 哪些状态必须有回滚方案
REQUIRES_ROLLBACK = {"dryrun", "shadow", "canary", "applied"}


class StateError(Exception):
    pass


@dataclass
class Step:
    ordinal: int
    name: str
    kind: str                      # sql | check | gate | manual
    target: str = ""
    expect: str = ""
    reversible: bool = True
    timeout_s: int = 0
    depends_on: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ordinal": self.ordinal, "name": self.name, "kind": self.kind,
            "target": self.target, "expect": self.expect, "reversible": self.reversible,
            "timeout_s": self.timeout_s, "depends_on": self.depends_on,
        }


@dataclass
class ChangePlan:
    change_id: str
    instance_id: str
    dialect: str
    sql: str
    risk_level: str
    steps: list[Step]
    rollback_plan: list[Step] = field(default_factory=list)
    rollback_possible: bool = True
    state: str = "draft"
    created_at: int = 0
    updated_at: int = 0
    actor: str = ""
    approver: str = ""
    evidence: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        ts = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
        return {
            "change_id": self.change_id,
            "instance_id": self.instance_id,
            "dialect": self.dialect,
            "sql": self.sql,
            "risk_level": self.risk_level,
            "state": self.state,
            "steps": [s.to_dict() for s in self.steps],
            "rollback_plan": [s.to_dict() for s in self.rollback_plan],
            "rollback_possible": self.rollback_possible,
            "created_at": self.created_at,
            "created_at_iso": ts(self.created_at),
            "updated_at": self.updated_at,
            "updated_at_iso": ts(self.updated_at),
            "actor": self.actor,
            "approver": self.approver,
            "evidence": self.evidence,
            "notes": self.notes,
            "allowed_next": list(ALLOWED.get(self.state, ())),
        }


# ------------------------------------------------------------------
# 计划生成
# ------------------------------------------------------------------


def build_plan(
    change_id: str,
    instance_id: str,
    sql: str,
    dialect: str,
    risk: dict,
    rollback: dict | None = None,
    table_rows: int | None = None,
    actor: str = "",
) -> ChangePlan:
    """根据风险评估结果生成可执行计划。步骤粒度取决于风险等级。"""
    level = risk.get("risk_level", "L1")
    steps: list[Step] = []
    n = 1

    long_tx_sql = (
        "SELECT COUNT(*) FROM information_schema.innodb_trx "
        "WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 60"
        if dialect in ("mysql", "mariadb", "oceanbase") else
        "SELECT COUNT(*) FROM pg_stat_activity WHERE backend_xid IS NOT NULL "
        "AND xact_start < now() - interval '60 seconds'"
    )

    def add(name: str, kind: str, target: str = "", expect: str = "",
            reversible: bool = True, timeout_s: int = 0, deps: list[int] | None = None) -> None:
        nonlocal n
        steps.append(Step(n, name, kind, target, expect, reversible, timeout_s, deps or []))
        n += 1

    add("采集变更前基线快照", "check", "capture_snapshot", "快照成功且 failed_sections 为空")
    add("确认无长事务阻塞", "gate", long_tx_sql, "count == 0", timeout_s=10)
    add("确认磁盘余量", "check", "disk_free_pct", "free >= 可用空间的安全余量（见表大小 × 2）")

    if dialect in ("mysql", "mariadb", "oceanbase"):
        add("确认复制状态", "check", "SHOW REPLICA STATUS", "Seconds_Behind_Master 在可接受范围")
    if dialect in ("postgres", "gaussdb", "kingbase"):
        add("确认无阻塞 vacuum 的会话", "gate",
            "SELECT COUNT(*) FROM pg_stat_activity WHERE state='idle in transaction'", "count == 0")

    if level in ("L2", "L3"):
        add("dry-run 验证（事务内执行后立即回滚）", "check", sql, "执行成功且影响行数符合预期")

    if level == "L3" or (table_rows or 0) >= BL.value(dialect, "table_rows_large", 10_000_000):
        add("影子表验证", "check", "shadow_table:<table>", "影子表结构与数据一致性校验通过")

    if risk.get("recommended_tool") and risk["recommended_tool"] not in ("直接执行",):
        add(f"使用 {risk['recommended_tool']} 执行在线变更", "sql", sql,
            f"工具自身进度达到 100%，且触发的 cut-over 在窗口内完成", timeout_s=3600)
    else:
        add("执行变更语句", "sql", sql, "执行成功", timeout_s=1800)

    add("验证变更结果", "check", "SHOW CREATE TABLE / \\d <table>", "目标对象结构符合预期")
    add("确认从库已同步", "check", "replica_status", "延迟回落到阈值内")
    if risk.get("prerequisites"):
        for p in risk["prerequisites"]:
            if isinstance(p, str) and p.strip():
                add(f"前置：{p[:60]}", "manual", "", "人工确认完成")

    rb_text = (rollback or {}).get("rollback_sql") or ""
    # 生成出来的"不可/无法回滚"说明不是回滚方案 —— 不能因为字符串非空就当作有回滚
    rb_possible = bool(rb_text) and not any(x in rb_text for x in ("不可逆", "不可回滚", "无法", "需人工"))
    rb_plan: list[Step] = []
    if rb_possible:
        rb_plan.append(Step(1, "执行反向语句", "sql",
                            rollback.get("rollback_sql", ""), "执行成功", True, 1800))
        rb_plan.append(Step(2, "验证数据/结构回到变更前状态", "check",
                            "compare_with_baseline", "与步骤1采集的基线一致", False))
    else:
        rb_plan.append(Step(1, "无自动回滚方案", "manual", "",
                            "需通过备份/影子表人工恢复", False))

    now = int(time.time())
    plan = ChangePlan(
        change_id=change_id, instance_id=instance_id, dialect=dialect, sql=sql,
        risk_level=level, steps=steps, rollback_plan=rb_plan,
        rollback_possible=rb_possible, state="draft",
        created_at=now, updated_at=now, actor=actor,
    )
    return plan


# ------------------------------------------------------------------
# 状态迁移
# ------------------------------------------------------------------


def validate_transition(plan: ChangePlan, to: str, approver: str = "") -> None:
    if to not in STATES:
        raise StateError(f"未知状态：{to}")
    allowed = ALLOWED.get(plan.state, ())
    if to not in allowed:
        raise StateError(
            f"不允许从 {plan.state} 迁移到 {to}（允许的目标：{'、'.join(allowed) or '无'}）"
        )
    if to in REQUIRES_ROLLBACK and not plan.rollback_possible:
        raise StateError(
            f"该变更没有自动回滚方案，禁止进入 {to}。"
            f"L2/L3 变更必须先确定回滚路径（有可能是不可逆操作，那就需要备份+影子表兜底）"
        )
    if to in REQUIRES_APPROVAL and not approver:
        raise StateError(f"进入 {to} 需要显式审批人（approver 不能为空）——责任链必须留痕")


def transition(plan: ChangePlan, to: str, actor: str = "", approver: str = "",
               reason: str = "", evidence: dict | None = None, note: str = "") -> ChangePlan:
    validate_transition(plan, to, approver)
    prev = plan.state
    now = int(time.time())
    plan.state = to
    plan.updated_at = now
    if actor:
        plan.actor = actor
    if approver:
        plan.approver = approver
    if evidence:
        evidence = dict(evidence)
        evidence.setdefault("from", prev)
        evidence.setdefault("to", to)
        evidence.setdefault("actor", actor)
        evidence.setdefault("reason", reason)
        evidence.setdefault("ts_iso", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)))
        plan.evidence.append(evidence)
    if note:
        plan.notes.append({
            "state": to, "actor": actor, "text": note,
            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        })
    return plan


def persist(plan: ChangePlan) -> None:
    save_change(plan.instance_id, {
        "change_id": plan.change_id,
        "state": plan.state,
        "risk_level": plan.risk_level,
        "sql": plan.sql,
        "dialect": plan.dialect,
        "actor": plan.actor,
        "approver": plan.approver,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "rollback_possible": plan.rollback_possible,
        "steps": [s.to_dict() for s in plan.steps],
        "rollback_plan": [s.to_dict() for s in plan.rollback_plan],
        "notes": plan.notes,
    })


def load(instance_id: str, change_id: str) -> ChangePlan | None:
    raw = load_change(instance_id, change_id)
    if not raw:
        return None
    plan = ChangePlan(
        change_id=raw["change_id"], instance_id=instance_id,
        dialect=raw.get("dialect", "mysql"), sql=raw.get("sql", ""),
        risk_level=raw.get("risk_level", "L1"),
        steps=[Step(**{k: v for k, v in s.items() if k in Step.__dataclass_fields__})
               for s in raw.get("steps", [])],
        rollback_plan=[Step(**{k: v for k, v in s.items() if k in Step.__dataclass_fields__})
                       for s in raw.get("rollback_plan", [])],
        rollback_possible=raw.get("rollback_possible", True),
        state=raw.get("state", "draft"),
        created_at=raw.get("created_at", 0), updated_at=raw.get("updated_at", 0),
        actor=raw.get("actor", ""), approver=raw.get("approver", ""),
        notes=raw.get("notes", []),
    )
    return plan


def timeline(instance_id: str, change_id: str) -> list[dict]:
    """变更的完整时间线（证据链）。"""
    from dbcore.profile_store import get_history
    hist = get_history(instance_id, kind="change", limit=200)
    return [h for h in hist if h.get("payload", {}).get("change_id") == change_id]
