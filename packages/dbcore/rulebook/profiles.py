"""规则 profile：一组预定义的规则启停配置。

profile 是纯数据 dict，被 RuleRegistry.filtered() 消费，字段含义完全一致：
    categories / severities / include / exclude。

`dialect` 不应写进 profile —— 方言过滤由调用方传 dialect 完成（同一 profile
在不同方言下自动得到不同生效集，这才是"零配置"的正确行为）。
"""

from __future__ import annotations

from typing import Any

# 全量：所有规则均按其 engines / dialect 自然生效，无任何门禁。
DEFAULT: dict[str, Any] = {}

# 宽松：只看 blocker + high，适合给业务团队做"快速体检"或初版基线。
LENIENT: dict[str, Any] = {"severities": ["blocker", "high"]}

# CI 门禁：与 lenient 同口径，但语义上明确用于流水线 fail-on。
CI_GATE: dict[str, Any] = {"severities": ["blocker", "high"]}

# 可演示的"行业模板"占位：银行核心系统通常禁用 REPLACE INTO（用 ON DUPLICATE）。
# 未来按客户接入更多模板（saas-oltp / data-warehouse ...）。
BANK_CORE: dict[str, Any] = {"exclude": ["DML004"]}

PROFILES: dict[str, dict[str, Any]] = {
    "default": DEFAULT,
    "lenient": LENIENT,
    "ci-gate": CI_GATE,
    "bank-core": BANK_CORE,
}

PROFILE_DESCRIPTIONS: dict[str, str] = {
    "default": "全量规则，仅受方言(engines)过滤",
    "lenient": "只看 blocker + high，适合快速体检/初版基线",
    "ci-gate": "blocker + high，CI 门禁用，fail-on 这些级别",
    "bank-core": "全量，但显式禁用 REPLACE INTO（DML004），改用 ON DUPLICATE",
}


def resolve(name: str) -> dict[str, Any]:
    if name not in PROFILES:
        raise KeyError(f"未知 profile：{name}（可选：{', '.join(PROFILES)}）")
    return PROFILES[name]
