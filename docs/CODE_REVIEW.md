# 代码审查报告

审查日期：2026-10-22
审查对象：`db-ai-toolchain` 11 仓（dbt-core / dbt-store / dbt-know / dbt-mcp-kit / 6 个 MCP / dbt-orchestrator）
代码规模：16,787 行 Python，92 个工具，12 个测试套件

---

## 一、审查方法与基线

| 项目 | 结果 |
|---|---|
| `scripts/check-boundaries.py` | 边界干净，0 违规（10 个仓） |
| `scripts/test-all.py` | 12 套件全过，225 项，39.7s |
| ruff `F,E9`（真实缺陷类） | 2 处（均为未使用导入） |
| ruff `B,C4,SIM,RET,ARG,PIE,PL` | 3 处（2 处 `subprocess.run` 缺 `check`，1 处循环变量未用） |
| 人工阅读 | 核心链路全读：协议、护栏、连接器、存储、状态机、规则引擎、编排 |
| 行为验证 | 写了 6 组探针脚本，对规则引擎 / 状态机 / Tier 契约 / 脱敏做实证 |

**静态质量高于同类项目平均水平。** 没有未定义变量、没有裸 `except:`、没有 TODO/FIXME 遗留、没有可变默认参数。问题不在"写得脏"，而在**几处关键路径的语义漏洞**——这些漏洞静态检查抓不到，因为代码本身是"合法且看起来对"的。

结论先行：**测试全绿不代表可用。** 本次找到 5 个 P0，其中 3 个是已复现的确定性缺陷（不是隐患），全都不在现有测试覆盖范围内。

---

## 二、P0：必须修，会导致数据事故或虚假安全承诺

### P0-1 `rollback_change` 的回滚 SQL 必然语法错误

`mcp-change-risk/src/change_risk/server.py:417`

```python
rb_sql = " ".join(s.target for s in plan.rollback_plan if s.target)
```

`rollback_plan` 有两个 Step：第 1 步 `kind="sql"`（target 是反向 DDL），第 2 步 `kind="check"`（target 是字面量 `"compare_with_baseline"`）。这里只按 `target` 非空过滤，把 check 步骤的占位符也拼进了 SQL。

复现：

```
rollback_change 实际会执行的 SQL:
'ALTER TABLE t DROP COLUMN c compare_with_baseline'
```

**影响**：`rollback_change` 100% 执行失败。这是"敢执行"卖点的最后一道保险，现在是断的。变更出事时按按钮回滚会拿到 `E_QUERY`，现场只能人工救。

**修法**：

```python
rb_sql = " ".join(s.target for s in plan.rollback_plan if s.kind == "sql" and s.target)
```

**为什么测试没发现**：`test_change_risk.py` 只测状态机迁移和 `build_plan` 的结构，`rollback_change` 需要真实数据库连接，端到端路径无覆盖。

---

### P0-2 MySQL 上的 DDL dry-run 会真的改库，却报告"已回滚"

`mcp-change-risk/src/change_risk/server.py:191`

```python
is_ddl = any(s.strip().lower().startswith(("alter ", "create ", "drop ", "truncate ", "rename ")) for s in stmts)
```

字符串前缀判断。用 sqlglot 实测：

| SQL | sqlglot 判定 | 前缀判定 |
|---|---|---|
| `/* 运维注记 */ ALTER TABLE t ADD COLUMN c INT` | **ddl** | **False** ← 绕过 |
| `# comment\nALTER TABLE t ADD COLUMN c INT` | **ddl** | **False** ← 绕过 |
| `OPTIMIZE TABLE t` | other | False |
| `GRANT SELECT ON db.* TO u` | other | False |

一旦绕过，走向 `run_in_transaction(commit=False)`。MySQL 的 DDL 会**隐式提交**，事务里的 `rollback()` 回滚不了已经完成的 ALTER。函数却返回：

```
"verdict": "事务已 ROLLBACK，数据未发生任何变更（dry-run 完成）"
```

**影响**：声称"验证过了、没留痕迹"，实际表结构已改。这是最坏的一类 bug——它让用户对 dry-run 建立错误的信任。

**根因不只在调用方**。`dbt-core/src/dbcore/connectors.py:242` 的 `run_in_transaction` 是共享库函数，任何 MCP 都能 import，**它自己完全不做语句类型校验**，只信调用方传进来的 `commit` 标志。

**修法（两层，缺一不可）**：

1. 库层纵深防御（`run_in_transaction` 内）：`commit=False` 时用 sqlglot 判 `stmt.kind`，遇到 DDL 直接 `raise ToolError("E_PERMISSION", ...)`。这保证"dry-run 永不改库"是库的保证，不是调用方的自觉。
2. 调用方改用 AST 判定，与 `dbcore.guard` 同源：
   ```python
   from dbcore.guard import is_ddl_statement  # 建议新增，复用 parse_statements
   is_ddl = any(is_ddl_statement(s, d) for s in stmts)
   ```

---

### P0-3 `JOIN002` 把 CTE 当成表，对正常 SQL 报 blocker

`mcp-sql-review/src/sql_review/rulebook/select.py:181`

```python
WITH t AS (SELECT id FROM x WHERE y=1) SELECT * FROM t
```

判定结果：

```
JOIN002 blocker: 查询涉及 2 张表（t、x）但没有任何跨表关联条件，结果是笛卡尔积
```

`t` 是 CTE 名不是表。根因：`ast.find_all(exp.Table)` 收集表时，只排除了父节点是 `exp.Subquery` 的（派生表），**没有排除 CTE 定义的名字**——CTE 的父节点是 `exp.CTE`，不是 `Subquery`，于是 `x` 和 `t` 都被算成平级表。

**影响**：`WITH` + CTE 是现代 SQL 的主流写法（`ci-gate` profile 下 blocker = 直接拦构建）。这个误报会让人在接入第一天就关掉规则引擎。

**修法**：

```python
cte_names = {c.alias for c in (ast.args.get("with") or exp.With()).expressions
             if isinstance(c, exp.CTE)}
...
if not in_sub and t.name not in cte_names:
    top_tables.append(t.name)
```

**测试盲区**：`fp_cases` 只有 5 条，覆盖了逗号连接、主键等值、分区键，**没有 CTE**。

---

### P0-4 PostgreSQL 上的"灰度 5%"实际是全量执行

`mcp-change-risk/src/change_risk/server.py:377-380`

```python
if dialect in ("postgres", "gaussdb", "kingbase"):
    # PG 的 UPDATE/DELETE 不支持 LIMIT，必须用 ctid 子查询限定
    return s          # ← 注释说要限定，代码直接原样返回
```

`execute_canary` 拿到原 SQL，`run_in_transaction(commit=True)` 全量提交，然后返回：

```
summary=f"灰度完成（{int(ratio * 100)}%），已 COMMIT，影响 {affected:,} 行"
```

**影响**：在 PG/GaussDB/Kingbase 上按了"灰度"，实际全量改了生产数据。这不是"能力弱"，是**对用户说谎**。同类问题：`_limit_sql` 里 `if " limit " in s.lower(): return s` —— 原 SQL 自带 `LIMIT 1000000` 时，灰度同样失效但仍报"灰度完成"。

**修法**（三选一，必须选一个，不能保留现状）：

- 实现 ctid 限定：`UPDATE t SET ... WHERE ctid IN (SELECT ctid FROM t WHERE <原条件> LIMIT n)`
- 或：PG 不支持时**明确拒绝**，`raise ToolError("E_NOT_IMPLEMENTED", "PG 暂不支持行数受限灰度，请用影子表方案")`
- 或：`ratio` 参数在 PG 下忽略，summary 如实写"全量执行（PG 无行数受限灰度）"

首要原则：**返回值不能说谎**。宁可报"不支持"，不能报"已完成 5%"。

---

### P0-5 明文密码随 `next_actions` 进入模型上下文

`mcp-change-risk/src/change_risk/server.py:173, 301`（同类用法还有多处）

```python
res.next(MCP_NAME, "dry_run_change", url=url, dialect=d, ...)
```

复现：

```json
"next_actions": [{
  "args": {"url": "mysql://root:S3cretPwd@10.0.0.1:3306/prod", "dialect": "mysql"}
}]
```

`Result.next()` 把参数原样塞进 `NextAction.args`，`to_dict()` 原样输出。MCP 工具返回值会**直接进入 LLM 上下文**——生产库密码因此出现在模型上下文、会话日志、以及 orchestrator 的 trace 里。

值得注意的是：**库里已经有脱敏函数 `dbcore.ids.redact_dsn`**，`profile_hub` 和 `dbwatch.bootstrap` 都正确调用了，唯独协议层（每个工具都必经的地方）没有。

**修法**：在 `protocol.py` 的 `Result.next()` 里做入口脱敏，而不是靠每个调用方自觉：

```python
_SENSITIVE_KEYS = ("url", "dsn", "connection", "dsn_str")
def next(self, mcp, tool, **kw):
    for k in _SENSITIVE_KEYS:
        if isinstance(kw.get(k), str):
            from dbcore.ids import redact_dsn
            kw[k] = redact_dsn(kw[k])
    ...
```

注意：脱敏后的 url 无法用于真实连接。应在 `next_actions` 里只放 `instance_id`，让下一步工具接收 `instance_id` 而非完整 URL——**这也是更好的接口设计**（`_iid()` 已经支持这个路径）。

---

## 三、P1：可靠性、性能与契约一致性

### P1-1 `_conn()` 每次调用跑 19 条 DDL，且连接从不关闭

`dbt-store/src/dbstore/store.py:200`

```python
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    for ddl in SCHEMA:      # 19 条 CREATE TABLE IF NOT EXISTS
        c.execute(ddl)
    _migrate(c)             # 2 次 PRAGMA table_info + 可能的 ALTER
    return c
```

实测：**单次约 9.06 ms**（50 次 453 ms）。全文 43 处 `with _conn() as c:`。

`with sqlite3.connect(...) as c:` **只提交事务，不关闭连接**（已实测：`with` 退出后连接仍可执行 SQL）。43 处全是这个模式，连接靠 GC 回收。

**影响**：
- `capture_snapshot` 写 30 个指标 = 30 次连接 = 约 270 ms 纯开销
- 一个 `instance_summary` 内部串 5 次 `_conn()` = 45 ms
- 多 MCP 是**独立进程**，同时写同一个 WAL 库会有 `database is locked`；`sqlite3.connect` 默认 `timeout=5`，没有 retry

**修法**：
1. 模块级 `_initialized` 标记，DDL 只跑一次（进程内）
2. 换成 `contextlib.closing(conn)` 或 try/finally `close()`
3. 显式 `sqlite3.connect(path, timeout=30)` + `PRAGMA busy_timeout=30000`

### P1-2 `anomaly_report` 在 0 个样本时返回 `pass`

实测空档案：

```
samples = 0 | alerts = [] | verdict = pass | tier = tier1
summary = "仅 0 个样本，退化为静态阈值告警（0 项）"
```

**summary 自己承认"仅 0 个样本"，却给 `pass`。** 这直接违反架构文档写的第 1 条硬约束——"不允许默默降级"。正确行为：`verdict="unknown"` + `tier_upgrade`，或直接 `E_NOT_TIER2`。

这也让 `health_score` 的错误码显得不一致：无快照时抛 `E_NOT_TIER2`，但"无快照"只需一次采集（tier1），应该是一个新的 `E_NO_SNAPSHOT` 或 `E_ARGS` 语义。

### P1-3 `check_grain` 的 GROUP BY 分支是空循环

`mcp-data-semantics/src/data_semantics/semantics.py:716-718`

```python
if aggs and group:
    for c in st.get("columns") or []:
        pass        # ← 什么都没做
```

工具对外宣称"粒度冲突检测"，实际只实现了"有聚合无 GROUP BY"这一半。**最常见、最严重的粒度错误——`SELECT dept, name, COUNT(*) FROM emp GROUP BY dept`（name 既不在 GROUP BY 也不在聚合里）——完全检不出来。**

这一条危害不只是功能缺失：函数返回 `verdict="pass"`，用户会以为检查过了。

### P1-4 状态机允许跳过 dry-run 与 canary

`mcp-change-risk/src/change_risk/execution.py:38`

```python
"prechecked": ("dryrun", "canary", "applied", "aborted"),
```

`prechecked → applied` 是合法迁移。只要 `rollback_possible=True` 且 `approver` 非空（任意字符串），就能从 prechecked 直达 applied，**完全跳过 dry-run 和 canary**。这与 `ARCHITECTURE.md` 的流程图（prechecked → dryrun → shadow → canary → applied）不一致。

另外 `REQUIRES_APPROVAL = {"canary", "applied", "rolled_back"}` 只校验 approver 非空，**没有校验 approver ≠ actor**。自己批自己，责任链形同虚设。

**修法**：按风险等级收紧 ALLOWED（L2/L3 不允许跳过 dryrun）；补 `approver == actor` 拒绝。

### P1-5 迁移证据链落盘即丢

`execution.py:260` 的 `persist()` 手写了一个 dict 交给 `save_change`：

```python
save_change(plan.instance_id, {
    "change_id": ..., "state": ..., "risk_level": ..., "sql": ...,
    "rollback_plan": ..., "notes": plan.notes,
})            # ← 没有 evidence
```

而 `load()` 也不恢复 `evidence`。`transition()` 里精心收集的 `evidence`（含 actor/reason/from/to）**只活在内存里**，进程退出即消失。

`execution.py` 的模块 docstring 写着"每次状态迁移必须写审计（谁、何时、理由、证据）"。目前只有 notes 落了盘，evidence 没有。

**修法**：`persist()` 加 `"evidence": plan.evidence`，`load()` 对应恢复。

### P1-6 规则误报率偏高，`ci-gate` 场景不可用

用 15 条**完全正常**的生产常见 SQL 测：

```
10/15 条被判有问题
```

具体误报：

| SQL | 误报 | 问题 |
|---|---|---|
| `DELETE FROM logs WHERE id = 42` | `DML002 medium` | 主键等值删单行，被判"影响行数不可控" |
| `UPDATE users SET name='x' WHERE id=1` | `DML002 medium` | 同上 |
| `WHERE created_at >= '2026-01-01'` | `TYPE001 low` | 日期列与字符串比较是标准写法 |
| `WHERE status='active'` | `TYPE001 low` | 没有列类型信息就报，全是噪音 |
| `id IN (SELECT id FROM users WHERE vip=1)` | `SUBQ002 medium` | **非相关**子查询不可能变成 DEPENDENT SUBQUERY |
| `SELECT id, (SELECT COUNT(*) FROM y) FROM x` | `SUBQ001 high` | 同上，非相关子查询只执行一次 |

`DML002` 和 `SUBQ001/002` 是 medium/high 级，直接决定 verdict 是"建议修改"还是"强烈建议修改"。这两类误报会让用户对 high 级告警脱敏——**误报比漏报更糟，这是项目自己写在 CLAUDE.md 里的原则**。

**修法优先级**：`DML002` 先判 WHERE 是否为主键/唯一键等值（可从 `ctx` 传主键信息，或退一步：有等值条件在 `id`/`*_id` 上就降为 info）；`SUBQ001/002` 先判子查询是否真的引用了外层列。

---

## 四、P2：代码卫生与一致性

| # | 位置 | 问题 |
|---|---|---|
| P2-1 | 7 个 `*/cli.py` | 除描述字符串外**完全相同**（83 行 × 7 = 581 行）。应下沉到 `dbt-mcp-kit`，各仓两行调用 |
| P2-2 | `docs/ARCHITECTURE.md:55` | 写 "~80 个工具"，实际 **92**（各仓 README 的工具表 100% 准确，只有总纲漂移） |
| P2-3 | `mcp-sql-review/src/sql_review/rules.py:3` | docstring 说"实现已迁移到 `dbcore/rulebook/`"，实际是 `sql_review/rulebook/` |
| P2-4 | `dbt-core/src/dbcore/protocol.py:163` | 死代码：`d.pop("took_ms", None) or None` 后立刻 `d["took_ms"] = self.took_ms` |
| P2-5 | `dbt-core/src/dbcore/guard.py:52` | 死分支：`isinstance(node, exp.Select)` 永不为真（`Select` 不在上面的 forbidden 元组里） |
| P2-6 | `dbt-mcp-kit/src/dbmcp/__init__.py:74` | 非 Result 分支硬编码 `"took_ms": 0`，但 `t0` 已经算了；同一处 `except Exception` 用 `E_QUERY`，而 `fail()` 把一切非 ToolError 归为 `E_ARGS` —— 同类异常两种码 |
| P2-7 | `dbt-core/src/dbcore/connectors.py:19` | `_URL_RE` 不 decode percent-encoding；密码含 `@`/`/` 直接解析失败。`redact_dsn` 的正则同样在密码含 `@` 时残留 `mysql://root:***@ss@host` |
| P2-8 | `mcp-change-risk/src/change_risk/server.py:254` | `_tables_of` 正则 `\bTABLE\s+(\w+)` 对 `CREATE TABLE IF NOT EXISTS t` 会捕获到 `IF` |
| P2-9 | `execution.py:184` | `rb_possible` 靠中文关键词（"不可逆"/"无法"…）判可回滚性。应改为 `rollback` dict 里的显式布尔字段 |
| P2-10 | `scripts/dev-install.py:38`、`scripts/test-all.py:42` | `subprocess.run` 未显式传 `check=`（ruff PLW1510） |

---

## 五、做得好的地方（应当保留的设计）

审下来并非全是问题。以下几点在同类项目中少见，值得守住：

1. **边界护栏是真的在跑**。`check-boundaries.py` 用 AST 扫 import，MCP→MCP 与共享库→MCP 全部为 0。11 仓拆分后每个 MCP 能独立 import 调用，已逐个验证过。"按需安装"不是口号。
2. **README 与代码 100% 对齐**。6 个 MCP 的 README 工具表逐行核对，14/13/15/12/13/25 全部准确。工具表从 `TOOLS` 导出而非手写副本，这个决定是对的。
3. **Tier 契约是硬的**。`capacity_forecast` 在 0 样本时确实抛 `E_NOT_TIER2` 并附带"先持续采集一段时间"，不是软提示。这比大多数"AI 分析工具"诚实。
4. **信封协议严格**。`Result.__post_init__` 校验 verdict/confidence/tier 枚举，`Evidence.__post_init__` 校验 kind 白名单——非法值当场炸，不会污染下游。
5. **规则 META 带 `why` + `false_positive`**。每条规则必须写清"为什么禁"和"什么场景不该报"，这个要求本身就逼出了质量。问题在于执行层面没按自己定的标准验收。
6. **orchestrator 有 URL 脱敏、`max_steps` 防环、失败隔离**。编排层没有为了"跑通 demo"而牺牲护栏。

---

## 六、建议动作顺序

按"修复成本 ÷ 风险削减"排序：

| 优先级 | 动作 | 预计改动 |
|---|---|---|
| 1 | 修 P0-1 回滚拼接（一行） | 1 行 |
| 2 | 修 P0-2 DDL 判定 + 库层防御（两层） | ~20 行 |
| 3 | 修 P0-4 PG 灰度说谎（改为明确拒绝） | ~15 行 |
| 4 | 修 P0-3 CTE 误报 + 补 CTE 回归用例 | ~10 行 |
| 5 | 修 P0-5 协议层脱敏 | ~15 行 |
| 6 | 修 P1-2 空样本 verdict + P1-3 空循环 | ~20 行 |
| 7 | 修 P1-5 evidence 持久化 | ~5 行 |
| 8 | 修 P1-1 连接复用（性能） | ~25 行 |
| 9 | 收紧 P1-4 状态机（按风险分级） | ~15 行 |
| 10 | 系统性治理 P1-6 误报（需先定"什么算可接受误报"） | 较大 |
| 11 | 清 P2 卫生项 + 补一条 CI 跑 `ruff -select F,E9` | ~1 小时 |

**最关键的一条流程建议**：现有 225 项测试是"命中测试"为主，误报回归只有 5 条。而规则引擎的价值恰恰在"不误报"。建议为每条 blocker/high 规则**至少配一条"不该报"的用例**，并把它作为加规则的前置条件（CLAUDE.md 里已经写了这个要求，只是没落实到测试门禁上）。

---

## 七、需要你决策的三个问题

1. **P0-4 的选择**：PG 的灰度是实现 ctid 限定，还是诚实拒绝？前者有工作量且 ctid 方案在大表上性能不稳，后者损失一个卖点但保住可信度。我倾向后者。
2. **P1-6 的治理标准**：`DML002` 在 "UPDATE 无 WHERE" 时该报，在 "WHERE 主键等值" 时不该报，中间地带（WHERE 非主键等值）报不报？这决定了规则的可用性边界。
3. **P1-6 的排期**：误报治理需要你先定标准，建议单独排期，不要和 P0 混在一起改。

---

## 八、修复记录（2026-10-22 同日完成）

**5 个 P0 全部修复，测试 225 → 263 项，12 套件全过，边界检查 0 违规。**

| 编号 | 修法 | 验证方式 |
|---|---|---|
| P0-1 | 抽出 `_rollback_statements()`，只取 `kind == "sql"` 的步骤；回滚改为按语句列表逐条执行而非 join | 5 条新用例：只取 sql 步骤、不含 check 占位符、空计划返回空表 |
| P0-2 | 双层防御：`guard.unsafe_in_transaction()` 用 AST 白名单判定；`run_in_transaction` 在 `commit=False` 时自己拒绝非 DML；`dry_run_change` 改用 AST | 15 条新用例：注释 DDL、行内注释 DDL、TRUNCATE/OPTIMIZE/GRANT/CALL/SET 全部拒绝；纯 DML 放行 |
| P0-3 | 收集表时排除 `with_.expressions` 的 CTE 别名；兼容 sqlglot 30 的 `with_` key | 5 条误报用例 + 1 条"真笛卡尔仍要抓"的反向用例 |
| P0-4 | PG 家族改为 `E_NOT_IMPLEMENTED` 明确拒绝并给替代方案；原 SQL 自带 LIMIT 时取 `min` 而非原样返回；summary 只陈述"实际影响行数 + 本轮上限" | 4 条用例：PG 拒绝、MySQL 加 LIMIT、原 LIMIT 收窄、非 DML 拒绝 |
| P0-5 | `protocol.Result.next()` 入口按 `SENSITIVE_ARG_KEYS` 脱敏；`_scrub_arg` 对"密码含未转义 @"兜底（`://` 到最后一个 `@`） | 3 条用例：常规 DSN、含 @ 的密码、非敏感键不被改写 |

**顺带修掉的既有 bug**（在修复过程中暴露，不在原报告里）：

- `_shadow_plan()` 引用未定义的 `t` —— **DDL dry-run 一走影子表分支就 NameError**。原报告的 P0-2 描述的是"注释绕过"，实际上即便走对分支也会崩，两条路都不通。已改为按 `_tables_of()` 结果遍历生成步骤，并补"识别不出表名时给人工提示"。
- `_tables_of()` 正则把 `CREATE TABLE IF NOT EXISTS orders` 的表名抓成 `IF`，且漏掉 `RENAME TABLE a TO b` 的 `b`。已改为 AST 提取。
- `dry_run_change` 的 `except Exception` 会把护栏抛的 `E_PERMISSION` 吞成 `E_CONNECT`，导致错误码误导。已加 `except ToolError: raise` 透传。
- `protocol.to_dict()` 里 `d.pop("took_ms", None) or None` 死代码。
- `guard.assert_readonly()` 里 `isinstance(node, exp.Select)` 永不为真的死分支。

**顺手修的文档漂移**：

- `docs/ARCHITECTURE.md` §0.3 的 "~80 个工具" → 92。

### 验证方式（不只看"测试通过"）

对 CTE 修复做了一次**变异测试**：把 `t.name not in cte_names` 临时改回旧逻辑，测试立刻报 2 项失败；恢复后通过。这说明新用例真的能拦住回归，而不是恒真断言。

```
--- 已临时还原为旧逻辑，跑测试 ---
通过 68 项，失败 2 项
  ✗ 不误报 JOIN002  << WITH t AS (SELECT id FROM x WHERE y=1) S -> {'JOIN002', 'SEL001'}
  ✗ 不误报 JOIN002  << WITH a AS (SELECT 1 AS i), b AS (SELECT  -> {'JOIN002', 'SEL001'}
```

### 未修（等你定标准）

- **P1-6 规则误报**（`DML002` / `SUBQ001` / `SUBQ002` / `TYPE001`）：改动涉及产品判断，需要先定"什么算可接受误报"，见第七节问题 2。
- **P1-1 连接复用**（`_conn()` 单次 9ms）：纯性能，不影响正确性，建议与 CI 一起做。
- **P1-2 / P1-3 / P1-4 / P1-5**：契约与状态机问题，改动小但会动行为，建议单独一个 commit 便于回滚。
- **P2 卫生项**：除已顺手修掉的死代码外，`cli.py` 去重（581 行）与 `ARCHITECTURE.md` 数字已/待处理。

