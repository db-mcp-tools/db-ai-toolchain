"""统一输入输出协议 dbt.result/v1 —— 跨 MCP 交互的唯一契约。

设计要点（详见 DESIGN.md §2）：
  * 任何 MCP 的任何工具都返回同一信封，编排层可无差别解析
  * 结论必须带 evidence（可溯源），杜绝"给结论不给依据"
  * tier 显式声明 tier0/tier1/tier2，不允许默默降级
  * next_actions 是留给多 Agent 编排的合同
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

SCHEMA = "dbt.result/v1"
PROTOCOL_VERSION = "0.1.0"

# ---------------------------------------------------------------- 枚举

VERDICTS = ("pass", "warn", "fail", "block", "unknown")
CONFIDENCES = ("high", "medium", "low")
TIERS = ("tier0", "tier1", "tier2")
SEVERITIES = ("critical", "high", "medium", "low", "info")
SQL_KINDS = ("executable", "template", "needs_metadata", "manual")
RISK_LEVELS = ("L0", "L1", "L2", "L3")
CHANGE_STATES = (
    "draft", "prechecked", "dryrun", "shadow", "canary",
    "applied", "rolled_back", "aborted",
)
EVIDENCE_KINDS = (
    "ast",            # 语法树静态分析得到
    "static_baseline",  # 静态知识基线（baselines.py）
    "signature",      # 故障指纹库（signature.py）
    "metadata",       # 实例元数据（版本/参数/表结构）
    "plan",           # 执行计划
    "runtime_metric",  # 运行时指标
    "history",        # 本实例历史观测
    "external",       # 外部知识（文档/案例）
    "sql",            # 可执行 SQL（作为证据或验证手段）
)

_SEVERITY_ORDER = {s: i for i, s in enumerate(SEVERITIES)}
_VERDICT_ORDER = {v: i for i, v in enumerate(("pass", "unknown", "warn", "fail", "block"))}

# ---------------------------------------------------------------- 错误码


class ToolError(Exception):
    """工具执行失败。code 取自 ERROR_CODES。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


ERROR_CODES = {
    "E_ARGS": "入参缺失或非法",
    "E_SQL_PARSE": "SQL 无法解析（方言不匹配或语法错误）",
    "E_UNSUPPORTED_DIALECT": "不支持的数据库方言",
    "E_READONLY_VIOLATION": "违反只读护栏，语句被拒绝",
    "E_CONNECT": "数据库连接失败",
    "E_QUERY": "查询执行失败",
    "E_NOT_TIER2": "该能力需要历史数据（Tier2），当前档案不足",
    "E_NOT_IMPLEMENTED": "能力尚未实现",
    "E_STORE": "档案库读写失败",
    "E_STATE": "状态机不允许该迁移",
    "E_PERMISSION": "操作未被授权或超出允许的变更范围",
    "E_TIMEOUT": "执行超时",
}


# ---------------------------------------------------------------- 信封


@dataclass
class Evidence:
    kind: str
    source: str
    fact: str
    supports: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ToolError("E_ARGS", f"未知 evidence.kind: {self.kind}", got=self.kind)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v != "" or k in ("kind", "source", "fact")}


@dataclass
class NextAction:
    mcp: str
    tool: str
    args: dict = field(default_factory=dict)
    reason: str = ""
    priority: int = 5
    optional: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    ok: bool
    mcp: str
    tool: str
    data: dict = field(default_factory=dict)
    verdict: str = "unknown"
    confidence: str = "low"
    summary: str = ""
    tier: str = "tier0"
    instance_id: str = ""
    correlation_id: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    next_actions: list[NextAction] = field(default_factory=list)
    tier_upgrade: dict | None = None
    warnings: list[str] = field(default_factory=list)
    error: dict | None = None
    took_ms: int = 0
    version: str = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ToolError("E_ARGS", f"未知 verdict: {self.verdict}")
        if self.confidence not in CONFIDENCES:
            raise ToolError("E_ARGS", f"未知 confidence: {self.confidence}")
        if self.tier not in TIERS:
            raise ToolError("E_ARGS", f"未知 tier: {self.tier}")

    # --流式 API -------------------------------------------------
    def add_evidence(self, kind: str, source: str, fact: str, supports: str = "", url: str = "") -> "Result":
        self.evidence.append(Evidence(kind, source, fact, supports, url))
        return self

    def next(self, mcp: str, tool: str, **kw: Any) -> "Result":
        """登记下一步建议调用的工具（跨 MCP 也允许）。"""
        reason = kw.pop("reason", "")
        priority = kw.pop("priority", 5)
        optional = kw.pop("optional", True)
        self.next_actions.append(NextAction(mcp, tool, kw, reason, priority, optional))
        return self

    def warn(self, msg: str) -> "Result":
        self.warnings.append(msg)
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schema"] = SCHEMA
        d["ts"] = int(time.time())
        d["evidence"] = [e.to_dict() if hasattr(e, "to_dict") else e for e in self.evidence]
        d["next_actions"] = [a.to_dict() if hasattr(a, "to_dict") else a for a in self.next_actions]
        d.pop("took_ms", None) or None
        d["took_ms"] = self.took_ms
        ordered = [
            "ok", "schema", "mcp", "tool", "version", "ts", "took_ms",
            "correlation_id", "instance_id", "tier",
            "verdict", "confidence", "summary",
            "data", "evidence", "next_actions", "tier_upgrade",
            "warnings", "error",
        ]
        return {k: d[k] for k in ordered if k in d}

    def json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


def ok(mcp: str, tool: str, **kw: Any) -> Result:
    return Result(ok=True, mcp=mcp, tool=tool, **kw)


def fail(mcp: str, tool: str, exc: Exception | ToolError, **kw: Any) -> Result:
    e = exc if isinstance(exc, ToolError) else ToolError("E_ARGS", str(exc))
    return Result(
        ok=False, mcp=mcp, tool=tool, verdict="unknown", confidence="low",
        error=e.to_dict(), summary=e.message, **kw,
    )


def new_correlation_id() -> str:
    """ULID 风格的时间排序 ID，便于按时间检索调用链。"""
    ts = int(time.time() * 1000)
    return f"{ts:013d}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- 判定合成工具


def worst_verdict(verdicts: Iterable[str]) -> str:
    vals = [v for v in verdicts if v in _VERDICT_ORDER]
    return max(vals, key=lambda v: _VERDICT_ORDER[v], default="unknown")


def worst_severity(severities: Iterable[str]) -> str:
    vals = [s for s in severities if s in _SEVERITY_ORDER]
    return min(vals, key=lambda s: _SEVERITY_ORDER[s], default="info")


def severity_to_verdict(severity: str) -> str:
    return {
        "critical": "fail",
        "high": "fail",
        "medium": "warn",
        "low": "warn",
        "info": "pass",
    }.get(severity, "unknown")


def lower_confidence(a: str, b: str) -> str:
    order = {c: i for i, c in enumerate(CONFIDENCES)}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def confirm_data(data: Any) -> dict:
    """保证 data 字段一定是 dict（协议要求）。"""
    if isinstance(data, dict):
        return data
    if data is None:
        return {}
    if hasattr(data, "to_dict"):
        d = data.to_dict()
        return d if isinstance(d, dict) else {"value": d}
    if isinstance(data, (list, tuple)):
        return {"items": list(data)}
    return {"value": data}
