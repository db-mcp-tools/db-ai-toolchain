"""规则分册包：按场景拆分的 SQL 静态体检规则集合。

对外提供与旧 dbcore.rules 一致的接口（review_sql / list_rules /
explain_rule / rule_categories / RULE_META / RULES / Finding / ReviewResult /
SEVERITY_ORDER），并新增 profile / 启停能力。
"""

from __future__ import annotations

from dbcore.rulebook.core import (  # noqa: F401
    Finding,
    ReviewResult,
    RuleFn,
    SEVERITY_ORDER,
)
from dbcore.rulebook.registry import Rule, RuleRegistry, build  # noqa: F401
from dbcore.rulebook.review import (  # noqa: F401
    explain_rule,
    list_rules,
    review_sql,
    rule_categories,
)
from dbcore.rulebook.registry import build as _build  # noqa: F401

# 供旧 dbcore.rules 薄壳再导出
RULE_META = {rid: m for rid, m in _build().meta_map().items()}
RULES = _build().as_rules_list()
