"""变更风险评估：把一句 DDL 翻译成「能不能在业务高峰执行」的明确结论。

输出不是建议，是可执行方案：风险等级 + 锁影响 + 推荐工具 + 前置条件 + 回滚语句 + 灰度步骤。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from dbcore.dialect import normalize_dialect, split_sql

# 风险等级
L0, L1, L2, L3 = "L0", "L1", "L2", "L3"
RISK_DESC = {
    L0: "元数据操作，秒级完成，可直接执行",
    L1: "在线操作，不阻塞写入，可在低峰执行",
    L2: "需要评估表体量与复制延迟，建议低峰 + 专用工具 + 灰度",
    L3: "重建表/阻塞写入，必须停机窗口或双写迁移方案",
}

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ADD_INDEX", re.compile(r"\b(add\s+(unique\s+)?(index|key)|create\s+(unique\s+)?index)\b", re.I)),
    ("DROP_INDEX", re.compile(r"\b(drop\s+index|drop\s+key|alter\s+table\s+.+?\s+drop\s+index)\b", re.I)),
    ("ADD_COLUMN", re.compile(
        r"\badd\s+(?:column\s+)?(?!index\b|key\b|unique\b|primary\b|constraint\b|foreign\b)`?([\w$]+)`?", re.I)),
    ("DROP_COLUMN", re.compile(r"\bdrop\s+(?:column\s+)?(?!index\b|key\b|table\b)`?([\w$]+)`?", re.I)),
    ("MODIFY_COLUMN", re.compile(r"\b(modify|change|alter\s+column)\b", re.I)),
    ("RENAME_TABLE", re.compile(r"\brename\s+to\b|\brename\s+table\b", re.I)),
    ("CHANGE_CHARSET", re.compile(r"\b(convert\s+to\s+charset|character\s+set|collate)\b", re.I)),
    ("ADD_CONSTRAINT", re.compile(r"\badd\s+(constraint|foreign\s+key|primary\s+key|unique)\b", re.I)),
    ("TRUNCATE", re.compile(r"^\s*truncate\b", re.I)),
    ("DROP_TABLE", re.compile(r"^\s*drop\s+table\b", re.I)),
    ("SET_DEFAULT", re.compile(r"\bset\s+default\b|\balter\s+column\s+.+?\s+set\b", re.I)),
    ("SET_NOT_NULL", re.compile(r"\bset\s+not\s+null\b", re.I)),
]

_TABLE_RE = re.compile(r"(?:alter\s+table\s+|create\s+(?:unique\s+)?index\s+\w+\s+on\s+|truncate\s+table\s+|drop\s+table\s+)`?([\w$.]+)`?", re.I)


@dataclass
class ChangeOp:
    op_type: str
    table: str | None
    sql: str
    risk: str = L1
    lock_impact: str = ""
    recommended_tool: str = ""
    preconditions: list[str] = field(default_factory=list)
    rollback: str = ""
    notes: list[str] = field(default_factory=list)
    # 因子模型输出（§2.1.2）：每个因子 {name, weight, value, contribution, basis}
    risk_score: int = 0
    factors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk_desc"] = RISK_DESC[self.risk]
        return d


@dataclass
class ChangePlan:
    dialect: str
    ops: list[ChangeOp] = field(default_factory=list)
    overall_risk: str = L0
    verdict: str = ""
    overall_score: int = 0

    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "overall_risk": self.overall_risk,
            "overall_risk_desc": RISK_DESC[self.overall_risk],
            "overall_score": self.overall_score,
            "verdict": self.verdict,
            "ops": [o.to_dict() for o in self.ops],
        }


# --------------------------------------------------------------- 方言风险矩阵

_MYSQL_MATRIX: dict[str, dict[str, Any]] = {
    "ADD_COLUMN": dict(risk=L1, lock="MySQL 8.0.12+ 满足条件时为 INSTANT DDL；否则 INPLACE，仅元数据锁短暂加锁",
                       tool="原生 DDL（先确认 INSTANT/INPLACE）",
                       pre=["确认非首列添加、无临时表残留", "确认 innodb_online_alter_log_max_size 足够"]),
    "ADD_INDEX": dict(risk=L2, lock="INPLACE：允许并发 DML，但准备阶段需短暂元数据锁，会被长事务阻塞",
                      tool="gh-ost / pt-online-schema-change（大表必选）",
                      pre=["检查是否存在持有锁的长事务", "评估磁盘余量 ≥ 表大小 ×1.2", "确认从库延迟 < 阈值"]),
    "DROP_INDEX": dict(risk=L1, lock="INPLACE：元数据操作，秒级", tool="原生 DDL",
                       pre=["确认该索引未被任何慢查询依赖（先查 performance_schema/审计日志）"]),
    "DROP_COLUMN": dict(risk=L2, lock="INPLACE 重建聚簇索引，期间阻塞并发 DML 的窗口很短但存在",
                        tool="gh-ost / pt-osc", pre=["全量侧需先下线对该字段的所有读写", "确认无视图/存储过程引用"]),
    "MODIFY_COLUMN": dict(risk=L3, lock="COPY TABLE：全程持有锁，读写阻塞，耗时与表大小成正比",
                          tool="gh-ost / pt-osc / 双写迁移",
                          pre=["停机窗口或双写", "类型收窄时必须先扫描确认无超长数据", "字符集/类型同时变更要拆成两步"]),
    "RENAME_TABLE": dict(risk=L1, lock="元数据重命名，秒级，但会打断正在执行的查询",
                         tool="原生 DDL", pre=["确认无长事务正在访问该表", "通知上下游（ETL、报表、订阅任务）"]),
    "CHANGE_CHARSET": dict(risk=L3, lock="COPY TABLE：全表重建", tool="pt-osc / 双写迁移",
                           pre=["停机窗口", "确认新字符集能容纳现有数据"]),
    "ADD_CONSTRAINT": dict(risk=L2, lock="需校验现有数据，可能长时间持锁", tool="先人工校验数据再 NOT VALID 方式加约束",
                           pre=["预扫描确认无违反约束的历史数据"]),
    "SET_DEFAULT": dict(risk=L1, lock="MySQL 8.0.12+ 元数据操作", tool="原生 DDL", pre=[]),
    "SET_NOT_NULL": dict(risk=L2, lock="需全表扫描校验", tool="原生 DDL（低峰）", pre=["确认无存量 NULL"]),
    "TRUNCATE": dict(risk=L3, lock="不可回滚，非事务性 DDL", tool="改 DELETE 分批 或 先 rename 后 drop",
                     pre=["必须二次确认 + 备份", "确认无外键引用"]),
    "DROP_TABLE": dict(risk=L3, lock="不可回滚", tool="先 rename 进回收站 schema，观察 N 天再 drop",
                       pre=["数据备份", "确认无外键引用", "业务侧二次确认"]),
}

_PG_MATRIX: dict[str, dict[str, Any]] = {
    "ADD_COLUMN": dict(risk=L1, lock="PG 11+ 常量默认值免重写；否则 ACCESS EXCLUSIVE 且重写全表",
                       tool="原生 DDL", pre=["带 DEFAULT 且 < PG11 → 按 L3 处理", "确认无 lock_timeout 冲突"]),
    "ADD_INDEX": dict(risk=L2, lock="普通 CREATE INDEX 阻塞该表写入，CONCURRENTLY 则不阻塞但需两次扫表",
                      tool="CREATE INDEX CONCURRENTLY",
                      pre=["CONCURRENTLY 不能在事务块内执行", "失败会留下 INVALID 索引，需巡检清理",
                           "确认无长事务（CONCURRENTLY 需等待所有事务结束）"]),
    "DROP_INDEX": dict(risk=L1, lock="短暂 ACCESS EXCLUSIVE", tool="DROP INDEX CONCURRENTLY（PG 9.2+）",
                       pre=["确认无查询正在使用该索引"]),
    "DROP_COLUMN": dict(risk=L1, lock="元数据标记，空间不立即回收", tool="原生 DDL",
                        pre=["后续需 VACUUM FULL / pg_repack 回收空间", "确认无视图依赖"]),
    "MODIFY_COLUMN": dict(risk=L3, lock="ALTER TYPE 全程 ACCESS EXCLUSIVE 且重写全表",
                          tool="新增列 + 双写回填 + 改名（三步法）",
                          pre=["停机窗口", "USING 子句需覆盖全部存量数据"]),
    "RENAME_TABLE": dict(risk=L1, lock="短暂 ACCESS EXCLUSIVE", tool="原生 DDL", pre=["确认无长事务"]),
    "CHANGE_CHARSET": dict(risk=L3, lock="重写全表", tool="新建表 + 双写迁移", pre=["停机窗口"]),
    "ADD_CONSTRAINT": dict(risk=L2, lock="校验期间持锁；FK 需要 SHARE ROW EXCLUSIVE 双向加锁",
                           tool="NOT VALID 先加，再 VALIDATE CONSTRAINT（弱锁补齐）",
                           pre=["预扫描无违反数据"]),
    "SET_DEFAULT": dict(risk=L1, lock="元数据", tool="原生 DDL", pre=[]),
    "SET_NOT_NULL": dict(risk=L2, lock="全表扫描校验，PG 12+ 若有 CHECK 约束可免扫描",
                         tool="先加 CHECK NOT VALID → VALIDATE → SET NOT NULL", pre=["确认无 NULL"]),
    "TRUNCATE": dict(risk=L3, lock="ACCESS EXCLUSIVE，虽在事务内可回滚，但生产禁用", tool="DELETE 分批",
                     pre=["二次确认", "确认无外键引用"]),
    "DROP_TABLE": dict(risk=L3, lock="不可回滚", tool="改名为 recycle schema，观察后 drop", pre=["备份", "二次确认"]),
}

MATRIX = {"mysql": _MYSQL_MATRIX, "postgres": _PG_MATRIX, "oracle": dict(_MYSQL_MATRIX), "sqlite": dict(_MYSQL_MATRIX), "clickhouse": dict(_MYSQL_MATRIX)}

_ROWS_TIERS = ((1_000_000, "小表（<100万行）"), (10_000_000, "中表（100万-1000万）"), (10 ** 18, "大表（>1000万行）"))

# --------------------------------------------------------------- 因子模型（§2.1.2）
# Risk(0-100) = Σ weight_i · value_i；每个因子贡献可解释，权重可按客户调。
# 矩阵里的 risk 等级是引擎知识（下限），因子分负责放大与解释，不允许把矩阵等级冲淡。
_FACTOR_WEIGHTS = {
    "操作破坏性": 30,
    "影响行数": 20,
    "锁持有语义": 15,
    "回滚可行性": 15,
    "执行时机": 10,
    "对象热度": 10,
}
_L_BASE = {L0: 0.1, L1: 0.3, L2: 0.6, L3: 1.0}          # 破坏性因子取值
_LOCK_HEAVY = ("copy table", "access exclusive", "全程", "阻塞写入", "阻塞并发 dml", "持锁")
_LOCK_MID = ("inplace", "concurrently", "校验", "重建", "扫描", "锁窗口")


def _lock_factor(lock_text: str) -> tuple[float, str]:
    low = (lock_text or "").lower()
    if any(k in low for k in _LOCK_HEAVY):
        return 1.0, "锁语义：全程持锁/重写全表"
    if any(k in low for k in _LOCK_MID):
        return 0.6, "锁语义：在线但存在锁窗口"
    return 0.2, "锁语义：元数据级"


def _rollback_factor(rollback_text: str) -> tuple[float, str]:
    t = rollback_text or ""
    if t.startswith("-- 不可逆"):
        return 1.0, "回滚：不可逆操作"
    if t.startswith("-- 未能"):
        return 0.7, "回滚：无法自动生成，依赖人工预案"
    return 0.1, "回滚：可自动生成反向语句"


def factor_analysis(op_type: str, spec: dict | None, rollback: str,
                    table_rows: int | None, during_peak: bool) -> tuple[int, str, list[dict]]:
    """计算单操作的因子分。返回 (0-100 分, 风险等级 L0-L3, 因子明细)。

    数据未知时按中性 0.5 计并在 basis 里注明 —— 不许猜也不许当 0（当 0 会假性降低风险）。
    """
    base_risk = spec["risk"] if spec else L2
    factors: list[dict] = []

    f1 = _L_BASE[base_risk]
    factors.append({"name": "操作破坏性", "weight": _FACTOR_WEIGHTS["操作破坏性"], "value": f1,
                    "contribution": round(f1 * _FACTOR_WEIGHTS["操作破坏性"], 1),
                    "basis": f"操作 {op_type} 的矩阵基线为 {base_risk}"})

    if table_rows is None:
        f2 = 0.5
        basis2 = "行数未知，按中性计（传入 table_rows 可提高准确度）"
    else:
        import math
        f2 = min(1.0, math.log10(max(table_rows, 1)) / 8.0)
        basis2 = f"行数 {table_rows}（log10 标度）"
    factors.append({"name": "影响行数", "weight": _FACTOR_WEIGHTS["影响行数"], "value": round(f2, 3),
                    "contribution": round(f2 * _FACTOR_WEIGHTS["影响行数"], 1), "basis": basis2})

    if spec:
        f3, basis3 = _lock_factor(spec.get("lock", ""))
    else:
        f3, basis3 = 0.8, "未知操作，按高锁风险计"
    factors.append({"name": "锁持有语义", "weight": _FACTOR_WEIGHTS["锁持有语义"], "value": f3,
                    "contribution": round(f3 * _FACTOR_WEIGHTS["锁持有语义"], 1), "basis": basis3})

    f4, basis4 = _rollback_factor(rollback)
    factors.append({"name": "回滚可行性", "weight": _FACTOR_WEIGHTS["回滚可行性"], "value": f4,
                    "contribution": round(f4 * _FACTOR_WEIGHTS["回滚可行性"], 1), "basis": basis4})

    f5 = 1.0 if during_peak else 0.2
    factors.append({"name": "执行时机", "weight": _FACTOR_WEIGHTS["执行时机"], "value": f5,
                    "contribution": round(f5 * _FACTOR_WEIGHTS["执行时机"], 1),
                    "basis": "业务高峰" if during_peak else "非高峰（默认按低峰计）"})

    f6 = 0.5
    factors.append({"name": "对象热度", "weight": _FACTOR_WEIGHTS["对象热度"], "value": f6,
                    "contribution": round(f6 * _FACTOR_WEIGHTS["对象热度"], 1),
                    "basis": "热度未知按中性计（Tier2 接入慢日志/审计后可用真实热度）"})

    total = sum(f["contribution"] for f in factors)
    score = int(round(min(100.0, max(0.0, total))))
    if score >= 75:
        level = L3
    elif score >= 50:
        level = L2
    elif score >= 25:
        level = L1
    else:
        level = L0
    # 引擎知识是下限：矩阵判 L3 的操作不允许被因子分冲淡到 L3 以下
    order = {L0: 0, L1: 1, L2: 2, L3: 3}
    if order[level] < order[base_risk]:
        level = base_risk
    return score, level, factors


def _detect_ops(sql: str) -> list[tuple[str, str | None]]:
    ops: list[tuple[str, str | None]] = []
    for op_type, pattern in _PATTERNS:
        if pattern.search(sql):
            m = _TABLE_RE.search(sql)
            ops.append((op_type, m.group(1) if m else None))
    if not ops:
        ops.append(("UNKNOWN_DDL", _TABLE_RE.search(sql).group(1) if _TABLE_RE.search(sql) else None))
    return ops


def assess_change(sql: str, dialect: str | None = None, table_rows: int | None = None, during_peak: bool = False) -> ChangePlan:
    """评估一条或多条 DDL 的变更风险（因子模型，每个因子贡献可解释）。

    overall = max(矩阵基线等级, 因子分映射等级)；factors[] 里能看到每一分来自哪里。
    """
    d = normalize_dialect(dialect)
    plan = ChangePlan(dialect=d)
    matrix = MATRIX.get(d, _MYSQL_MATRIX)

    for text in split_sql(sql, d):
        for op_type, table in _detect_ops(text):
            spec = matrix.get(op_type)
            rollback = generate_rollback(text, d)
            notes: list[str] = []
            rows_for_factor = table_rows

            if spec is None:
                score, level, factors = factor_analysis(op_type, None, rollback,
                                                        rows_for_factor, during_peak)
                op = ChangeOp(op_type=op_type, table=table, sql=text, risk=level,
                              lock_impact="未知操作，无法自动评估", recommended_tool="人工评审",
                              preconditions=["人工确认执行计划"], rollback=rollback,
                              notes=["方言未被风险矩阵覆盖"], risk_score=score, factors=factors)
                plan.ops.append(op)
                continue

            if table_rows:
                tier = next(desc for limit, desc in _ROWS_TIERS if table_rows <= limit)
                if table_rows > 10_000_000 and spec["risk"] in (L1, L2):
                    notes.append(f"{tier} + {d}：大表在线 DDL 时长不可控，建议一律走旁路工具")
                if table_rows < 1_000_000 and op_type in ("ADD_COLUMN", "DROP_INDEX", "SET_DEFAULT"):
                    notes.append(f"{tier}：影响可控")
            else:
                notes.append("未提供表行数：行数因子按中性计，建议连库采集后重新评估（estimate_impact）")
            if during_peak and spec["risk"] != L0:
                notes.append("业务高峰期执行：元数据锁等待会引发雪崩式会话堆积，务必错峰")

            score, level, factors = factor_analysis(op_type, spec, rollback,
                                                    rows_for_factor, during_peak)
            # 大表硬性下限（引擎知识，不因小分被冲淡）：>1000 万行的 L1/L2 操作至少按 L2 处理
            if table_rows and table_rows > 10_000_000 and spec["risk"] in (L1, L2):
                order_lv = {L0: 0, L1: 1, L2: 2, L3: 3}
                if order_lv[level] < order_lv[L2]:
                    level = L2
                    factors.append({"name": "大表下限", "weight": 0, "value": 1.0,
                                    "contribution": 0.0,
                                    "basis": "行数 >1000 万：矩阵知识要求至少按 L2 处理（旁路工具）"})
            plan.ops.append(
                ChangeOp(
                    op_type=op_type,
                    table=table,
                    sql=text,
                    risk=level,
                    lock_impact=spec["lock"],
                    recommended_tool=spec["tool"],
                    preconditions=list(spec["pre"]),
                    rollback=rollback,
                    notes=notes,
                    risk_score=score,
                    factors=factors,
                )
            )

    order = {L0: 0, L1: 1, L2: 2, L3: 3}
    plan.overall_risk = max((o.risk for o in plan.ops), key=lambda r: order[r], default=L0)
    plan.overall_score = max((o.risk_score for o in plan.ops), default=0)
    plan.verdict = {
        L0: "可直接执行（仍需留痕）",
        L1: "低峰窗口执行，执行前跑 precheck_change_window",
        L2: "必须用旁路工具 + 灰度 + 回滚预案，非低峰禁止",
        L3: "停机窗口 / 双写迁移，禁止在线直接执行",
    }[plan.overall_risk]
    return plan


def generate_rollback(sql: str, dialect: str | None = None) -> str:
    """尽最大努力生成反向 DDL；无法自动生成的会明确说明（不编造）。"""
    d = normalize_dialect(dialect)
    text = " ".join(sql.split()).rstrip(";")
    lower = text.lower()
    table_m = _TABLE_RE.search(text)
    table = table_m.group(1).strip("`") if table_m else "<table>"

    # 注意排除 CONCURRENTLY / IF NOT EXISTS 这类关键字被误当成索引名
    _idx_name = re.search(r"index\s+(?:concurrently\s+|if\s+not\s+exists\s+)?`?(\w+)`?", text, re.I)

    if re.search(r"^\s*create\s+(unique\s+)?index", lower):
        name = _idx_name
        idx = name.group(1) if name else "<index_name>"
        return f"DROP INDEX {idx} ON {table};" if d == "mysql" else f"DROP INDEX CONCURRENTLY {idx};"

    if re.search(r"\badd\s+(unique\s+)?(index|key)\b", lower):
        name = re.search(r"(?:index|key)\s+`?(\w+)`?\s*(?:\(|$)", text, re.I) or _idx_name
        idx = name.group(1) if name else "<index_name>"
        return f"ALTER TABLE {table} DROP INDEX `{idx}`;" if d == "mysql" else f"DROP INDEX CONCURRENTLY {idx};"

    if re.search(r"\badd\s+(column\s+)?", lower) and re.search(r"\badd\b", lower):
        col = re.search(r"\badd\s+(?:column\s+)?`?(\w+)`?", text, re.I)
        c = col.group(1) if col else "<column>"
        return f"ALTER TABLE {table} DROP COLUMN `{c}`;" if d == "mysql" else f"ALTER TABLE {table} DROP COLUMN {c};"

    # TRUNCATE / DROP 必须先于列级 drop 判断，否则会被下面的 \bdrop\s+ 抢先匹配
    if re.search(r"^\s*truncate\b", lower):
        return ("-- 不可逆：TRUNCATE 不记录明细行、不可回滚、会重置自增计数器。\n"
                f"-- 回滚前提：变更前的全量备份或影子表。\n"
                f"-- 建议事前执行：CREATE TABLE {table}_bak_latest AS SELECT * FROM {table};")

    if re.search(r"^\s*drop\s+(table|database|schema)\b", lower) or re.search(r"\bdrop\s+table\b", lower):
        return ("-- 不可逆：DROP TABLE 会连同数据、触发器与该表的对象权限一起删除。\n"
                f"-- 回滚前提：变更前备份 + 依赖该表的视图/存储过程清单。\n"
                f"-- 建议事前执行：CREATE TABLE {table}_bak_latest AS SELECT * FROM {table};\n"
                f"-- 更稳妥的做法：先 RENAME 到回收 schema 观察 N 天，确认无影响后再 DROP。")

    if re.search(r"\bdrop\s+(column\s+)?", lower):
        return ("-- 不可逆：DROP COLUMN 会连同数据一起删除。\n"
                f"-- 回滚前提：变更前的结构定义 + 该字段数据备份。\n"
                f"-- 建议事前执行：CREATE TABLE {table}_bak_latest AS SELECT * FROM {table};")

    if re.search(r"\bmodify\b|\bchange\b|\balter\s+column\b", lower):
        return ("-- 不可逆：需要变更前的字段定义。\n"
                f"-- 回滚方式：ALTER TABLE {table} MODIFY COLUMN <col> <原类型原属性>;  （用以前的定义改写回去）")

    if re.search(r"\brename\s+to\b|\brename\s+table\b", lower):
        m = re.search(r"\brename\s+to\s+`?(\w+)`?|^\s*rename\s+table\s+`?(\w+)`?\s+to\s+`?(\w+)`?", lower)
        if m and m.group(3):
            return f"RENAME TABLE `{m.group(3)}` TO `{m.group(2)}`;"
        new = m.group(1) if m else None
        old = table
        if new:
            return f"ALTER TABLE `{new}` RENAME TO `{old}`;" if d == "mysql" else f"ALTER TABLE {new} RENAME TO {old};"
        return "-- 未能解析重命名目标，无法自动生成回滚"

    return "-- 未能识别操作类型，请人工制定回滚方案。"
