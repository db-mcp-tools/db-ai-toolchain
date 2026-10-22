"""可配置的 SQL 静态体检入口。

在保持旧行为（profile=default 时与拆分前逐条一致）的前提下，新增按
profile / disable / enable / only_categories / only_severities 的启停能力。
"""

from __future__ import annotations

from typing import Any

from dbcore.rulebook.core import (_dedup_findings, normalize_dialect, parse_statements)
from dbcore.rulebook.profiles import PROFILES, resolve
from dbcore.rulebook.registry import Rule, RuleRegistry, build


def review_sql(sql: str, dialect: str | None = None, *,
               profile: str | dict[str, Any] = "default",
               disable: list[str] | None = None,
               enable: list[str] | None = None,
               only_categories: list[str] | None = None,
               only_severities: list[str] | None = None,
               **opts) -> Any:
    """对一段 SQL（可多句）做静态体检。

    参数
    ----
    profile: 命名配置名（"default"/"lenient"/"ci-gate"/...）或自定义 dict。
    disable: 本次额外禁用的规则 ID 列表（在 profile 之上叠加）。
    enable:  本次强制启用的规则 ID 列表（优先级最高）。
    only_categories / only_severities: 临时把生效集收窄到指定分类/严重度。
    **opts: 透传给规则上下文，如 partition_keys / deep_offset_threshold / table_rows。
    """
    from dbcore.rulebook.core import Finding, ReviewResult  # 延迟避免环

    d = normalize_dialect(dialect)
    result = ReviewResult(dialect=d)
    reg: RuleRegistry = build()

    prof = resolve(profile) if isinstance(profile, str) else (profile or {})
    # profile 里若已含 categories/severities，与显式参数合并
    cats = list(only_categories or prof.get("categories") or [])
    sevs = list(only_severities or prof.get("severities") or [])
    inc = list(enable or prof.get("include") or [])
    exc = list(disable or prof.get("exclude") or [])

    active: list[Rule] = reg.filtered(dialect=d, categories=cats or None,
                                      severities=sevs or None,
                                      include=inc or None, exclude=exc or None)

    stmts = parse_statements(sql, d)
    result.statement_count = len(stmts)
    ctx = dict(opts)
    ctx["_dialect"] = d

    for stmt in stmts:
        ctx["_sql_text"] = stmt.sql
        if not stmt.parsable:
            result.unparsable.append(stmt.sql[:200])
            result.findings.append(
                Finding(
                    rule_id="PARSE000",
                    severity="info",
                    message="该语句无法被解析器识别，跳过静态检查",
                    suggestion="人工复核；若确为合法方言写法，请提 issue 补充方言支持",
                )
            )
            continue
        for rule in active:
            result.findings.extend(rule.fn(stmt.ast, ctx))

    result.findings = _dedup_findings(result.findings)
    result.findings.sort(key=lambda f: _severity_key(f.severity))
    return result


def _severity_key(sev: str) -> int:
    from dbcore.rulebook.core import SEVERITY_ORDER
    return SEVERITY_ORDER.get(sev, 9)


# -- 兼容旧 dbcore.rules 的 list_rules / explain_rule / rule_categories -----


def list_rules(category: str | None = None, severity: str | None = None,
               dialect: str | None = None) -> list[dict]:
    reg = build()
    meta = reg.meta_map()
    out = []
    for rid, m in meta.items():
        if category and m.get("category") != category:
            continue
        if severity and m.get("severity") != severity:
            continue
        if dialect:
            engines = m.get("engines") or []
            cat = m.get("category")
            if cat in ("postgres", "gaussdb", "kingbase") and dialect.lower() not in \
                    [e.lower() for e in engines] and dialect.lower() not in ("postgres", "gaussdb", "kingbase"):
                continue
            if cat == "mysql" and dialect.lower() not in [e.lower() for e in engines] and \
                    dialect.lower() not in ("mysql", "mariadb", "oceanbase", "tidb"):
                continue
        out.append(m)
    return sorted(out, key=lambda r: (_severity_key(r["severity"]), r["rule_id"]))


def explain_rule(rule_id: str) -> dict | None:
    reg = build()
    r = reg.get(rule_id)
    if r is not None:
        return {"rule_id": r.rule_id, "name": r.name, "category": r.category,
                "severity": r.severity, "why": r.why, "false_positive": r.false_positive,
                "engines": list(r.engines)}
    m = reg.meta_map().get(rule_id.upper())
    return m


def rule_categories() -> list[str]:
    reg = build()
    cats = reg.categories()
    if "parse" not in cats:
        cats.append("parse")
    return sorted(cats)
