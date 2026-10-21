# 仓库约定（给 AI 协作者与未来的自己）

## 三条不可违反

1. **改代码前先改 `DESIGN.md`**。工具清单、IO 协议、枚举取值都在那里定义。
2. **除 `profile-hub` 外任何 MCP 不得自建存储**。所有观测写入 `dbcore.profile_store`。
3. **每个工具必须返回 `dbcore.protocol.Result`**。禁止直接返回裸 dict。

## Tier 契约

任何诊断类工具必须在 Tier0（零历史）下可用。做不到就在入参缺失时明确报错并给出
`tier_upgrade` 指引，**绝不能：**
- 用 0 / 空值冒充"健康"
- 用单个数据点假装能判断趋势
- 默默降级而不在返回里声明

## 加规则的检查清单

- [ ] 规则函数写进 `packages/dbcore/rules.py`，并注册到 `RULES`
- [ ] 在 `RULE_META` 写清：为什么禁、什么情况会误报
- [ ] 至少 1 个命中样例 + 1 个不误报样例，写进 `tests/test_all.py`
- [ ] 能自动改写的，在 `fix.py` 里加 FIXER；不能的明确标 `manual`

## 加工具的检查清单

- [ ] 更新 `DESIGN.md` 对应 MCP 的工具表
- [ ] 函数签名里带上 `instance_id` / `correlation_id`（跨 MCP 关联用）
- [ ] 至少一条 `add_evidence`（没有依据的结论不允许 `confidence: high`）
- [ ] 声明 `next_actions`（下一步该调谁）
- [ ] 在 `tests/test_all.py` 加一条信封一致性断言

## 已知的 sqlglot 30 坑（后面加规则会再遇到）

- `FROM` 的 arg key 是 `from_`，不是 `from`
- `LIMIT n OFFSET m` 的结构是 `Select.args["offset"] = Offset(expression=Literal)`
- `DATE()` 被规范化成 `TsOrDsToDate`
- `SUBSTRING` 的参数在 `this/start/length`，不在 `expressions`
- **`exp.And` 是 `exp.Func` 的子类** —— 判断"字段上套函数"必须先排除 Connector/Predicate/Binary
- `And/Or` 是 Binary（this/expression），没有 `expressions` 列表
- `COUNT(DISTINCT x)` 的 inner 是 `Distinct(Column)`，要先展开

## 命名

- 工具名：小写下划线
- 规则 ID：`<场景码><序号>`，场景码见 `DESIGN.md §5`
- 证据 source：`模块.路径`，例如 `baselines.mysql.repl_lag_warn_s`、`rule.JOIN002`
