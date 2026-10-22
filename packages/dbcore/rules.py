"""SQL 静态体检规则（兼容薄壳）。

实现已迁移到 `dbcore/rulebook/`（按场景分册 + 单一注册表 + 按方言/分类/严重度启停）。
本文件仅做再导出，保证 `fix.py` / `dbcore/__init__.py` / 既有测试零改动。
新代码请直接 `from dbcore.rulebook import ...`。
"""

from __future__ import annotations

# 直接复用规则分册，避免重复维护
from dbcore.rulebook import (  # noqa: F401
    Finding,
    RULE_META,
    RULES,
    ReviewResult,
    RuleFn,
    SEVERITY_ORDER,
    build,
    explain_rule,
    list_rules,
    review_sql,
    rule_categories,
)

__all__ = [
    "SEVERITY_ORDER", "Finding", "ReviewResult", "RuleFn",
    "review_sql", "list_rules", "explain_rule", "rule_categories",
    "RULE_META", "RULES", "build",
]
