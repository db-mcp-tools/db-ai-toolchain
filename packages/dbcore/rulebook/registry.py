"""规则注册表：汇聚所有分册的规则，支持按方言 / 分类 / 严重度 / 显式名单启停。

单一事实来源：`build()` 扫描 select/dml/type/tx/engine/subquery 六个分册的
`RULES`（rule_id → fn）与 `META`，构造出全局规则表。任何工具要跑规则都从
这里取，不再硬编码顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from dbcore.rulebook.core import SEVERITY_ORDER

# 子模块到"分册"的归属（用于 list_rules 展示册名，可选）
_SUBBOOKS = (
    "select",
    "dml",
    "type",
    "tx",
    "engine",
    "subquery",
)

# 无法解析的语句标记：不是函数规则，只是 review 流程的产物，但 list_rules 要能解释它
PARSE_META = {
    "PARSE000": {
        "name": "语句无法解析",
        "category": "parse",
        "severity": "info",
        "why": "方言写法不在 sqlglot 覆盖范围内，静态检查已跳过",
        "false_positive": "本来就是需要人工复核的场景",
        "engines": [],
    }
}


@dataclass
class Rule:
    rule_id: str
    name: str
    category: str
    severity: str
    fn: Callable[[object, dict], list]
    why: str
    false_positive: str
    engines: tuple[str, ...] = ()

    @property
    def is_engine_specific(self) -> bool:
        return bool(self.engines)


class RuleRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, Rule] = {}

    # -- 构建 -------------------------------------------------------
    def add(self, rule: Rule) -> None:
        if rule.rule_id in self._by_id:
            raise ValueError(f"规则 ID 冲突：{rule.rule_id}")
        self._by_id[rule.rule_id] = rule

    # -- 查询 -------------------------------------------------------
    def get(self, rule_id: str) -> Rule | None:
        return self._by_id.get(rule_id.upper())

    def all(self) -> list[Rule]:
        return sorted(self._by_id.values(),
                      key=lambda r: (SEVERITY_ORDER.get(r.severity, 9), r.rule_id))

    def categories(self) -> list[str]:
        return sorted({r.category for r in self._by_id.values()})

    def rule_count(self) -> int:
        return len(self._by_id)

    # -- 启停过滤 ---------------------------------------------------
    def filtered(self, *, dialect: str | None = None, categories: list[str] | None = None,
                 severities: list[str] | None = None, include: list[str] | None = None,
                 exclude: list[str] | None = None) -> list[Rule]:
        """归并各维度过滤，返回本次应执行的规则列表。

        优先级：engines 方言过滤 → categories → severities → exclude → include。
        include 永远最后生效（即便前面被过滤掉也强制保留），便于临时顶起某条规则。
        """
        inc = {s.upper() for s in (include or [])}
        exc = {s.upper() for s in (exclude or [])}
        cats = {c.lower() for c in (categories or [])}
        sevs = {s.lower() for s in (severities or [])}

        out: list[Rule] = []
        for r in self.all():
            # 1) 方言引擎过滤
            if dialect and r.engines and dialect.lower() not in [e.lower() for e in r.engines]:
                if r.rule_id not in inc:
                    continue
            # 2) 分类过滤
            if cats and r.category.lower() not in cats:
                if r.rule_id not in inc:
                    continue
            # 3) 严重度过滤
            if sevs and r.severity.lower() not in sevs:
                if r.rule_id not in inc:
                    continue
            # 4) 显式排除
            if r.rule_id in exc and r.rule_id not in inc:
                continue
            # 5) 显式包含（已在上面各步被 inc 豁免，这里统一收口）
            out.append(r)
        # 把 include 中但前面被完全跳过（例如 engines 不匹配且无豁免）的补回来
        for rid in inc:
            r = self._by_id.get(rid)
            if r and r not in out:
                out.append(r)
        return sorted(out, key=lambda r: (SEVERITY_ORDER.get(r.severity, 9), r.rule_id))

    # -- 兼容旧世界（dbcore.rules 薄壳再导出用） ---------------------
    def as_rules_list(self) -> list[tuple[str, Callable]]:
        """返回旧式 (函数名, fn) 列表，保持 dbcore.rules.RULES 兼容。"""
        return [(r.fn.__name__, r.fn) for r in self.all()]

    def meta_map(self) -> dict[str, dict]:
        m = {rid: {"rule_id": rid, **r._meta_dict()} for rid, r in self._by_id.items()}
        m.update({rid: {"rule_id": rid, **v} for rid, v in PARSE_META.items()})
        return m


def _build_from_subbooks() -> RuleRegistry:
    reg = RuleRegistry()
    for mod_name in _SUBBOOKS:
        mod = __import__(f"dbcore.rulebook.{mod_name}", fromlist=["RULES", "META"])
        meta: dict = getattr(mod, "META", {})
        rules: list = getattr(mod, "RULES", [])
        for rule_id, fn in rules:
            info = meta.get(rule_id)
            if not info:
                raise ValueError(f"分册 {mod_name} 的规则 {rule_id} 缺少 META")
            reg.add(Rule(
                rule_id=rule_id,
                name=info.get("name", rule_id),
                category=info["category"],
                severity=info["severity"],
                fn=fn,
                why=info.get("why", ""),
                false_positive=info.get("false_positive", ""),
                engines=tuple(info.get("engines", []) or []),
            ))
    return reg


# 模块级单例：第一次访问时构建
_REGISTRY: RuleRegistry | None = None


def build() -> RuleRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_from_subbooks()
    return _REGISTRY


# 给 Rule 补一个内部方法（避免 dataclass 字段膨胀）
def _rule_meta_dict(self: Rule) -> dict:
    return {
        "name": self.name,
        "category": self.category,
        "severity": self.severity,
        "why": self.why,
        "false_positive": self.false_positive,
        "engines": list(self.engines),
    }


Rule._meta_dict = _rule_meta_dict  # type: ignore[attr-defined]
