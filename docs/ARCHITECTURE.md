# ARCHITECTURE.md — db-ai-toolchain 总体架构

> 本文件管**总体架构与仓库边界**：为什么这样拆、每个仓的职责、Tier 冷启动契约、
> 规则分册编码、质量门槛、版本策略。
>
> 三份文档的分工：
> - **本文件** → 架构与边界
> - [`PROTOCOL.md`](PROTOCOL.md) → 跨仓唯一契约 `dbt.result/v1`（改返回格式先改它）
> - [`DATA_MODEL_AND_ALGORITHMS.md`](DATA_MODEL_AND_ALGORITHMS.md) → 算法与数据模型（从需求倒推）

版本：v0.2.0 · 最后更新：2026-10-22

---

## 0. 三个必须回答的问题

### 0.1 为什么是 MCP 先行

MCP 是「能力原子」。单独一个 MCP 不值钱，**能被编排层组合 + 共享同一份档案**才值钱。
本仓的目标产出不是一个工具，是一条链：

```
体检(sql-review) → 修正(sql-review) → 验证(sql-review)
      → 变更护栏(change-risk) → 值守(dbwatch) → 根因(dbwatch)
      → 迁移验证(migration-diff) → 口径治理(data-semantics)
                    ↑              ↓
                   所有环节 ← profile-hub（唯一真相源）
```

### 0.2 冷启动问题（关键设计约束）

> **质疑**："你说 dbwatch 的根因报告依赖 1/2/3 的积累，但那是客户现场的数据，我拿不到。
> 除非客户先买了前面的产品、积累几个月档案，才能用上值守 —— 那我永远卖不出第一单。"

这个质疑成立，所以本仓引入 **Tier 分级契约**：**任何诊断类工具必须在 Tier0（零历史）下可用。**

| Tier | 输入条件 | 能产出什么 | 置信度声明 |
|------|---------|-----------|-----------|
| **Tier0 · 静态** | 一段 SQL / 一份快照 / 一次采集，无任何历史 | 结论 + 依据 + 修法 | `confidence: medium`，`evidence[].kind = "static_baseline"` |
| **Tier1 · 单次快照** | 现场做过一次 `bootstrap` 采集 | 在 Tier0 基础上叠加实例真实元数据（版本/参数/表大小/索引） | `high`（涉及元数据的部分） |
| **Tier2 · 有时序** | ProfileStore 内有 ≥2 次观测（≥7 天更佳） | 趋势、漂移、同比、回归检测 | `high`，并附样本数与时间跨度 |

硬性约束写进代码：

1. 每个返回必须携带 `"tier"` 字段，**不允许默默降级**。
2. Tier0 下不伪造"环比/趋势"结论；缺失历史时返回 `tier_upgrade` 提示，告诉用户"再做一次 X 就能得到 Y"。
3. Tier0 的知识来自 **`baselines.py`（静态基线）+ `signature.py`（故障指纹库）**，这是可维护的知识资产，不是硬编码默认值。

> **商业含义**：销售路径不再是"先买体检养几个月"。Tier0 就能交付第一次报告，
> 每次交付自动把这次的观测写进 ProfileStore，从第 2 次起自然升到 Tier2 —— **积累是使用的副产品，不是前置条件。**

### 0.3 规模与深度

单体脚本量级不够。`rsi-boot` 一个记忆类 MCP 是十几工具、上万行。
本仓目标：**6 个 MCP / ~80 个工具 / 内核分模块**。覆盖面以"DBA 真实工作流"为准，而不是以"能演示"为准。

---

---

## 1. 仓库布局（一个 MCP 一个仓）

```
db-ai-toolchain/                 ← 本仓：文档 / 脚本 / 集成测试的容器（不含业务代码）
├── dbt-core/                    共享库：协议 / 方言 / AST / 连库 / 计划归一
├── dbt-store/                   共享库：统一档案存储（SQLite）
├── dbt-know/                    共享库：引擎阈值 / 指标字典 / 故障指纹
├── dbt-mcp-kit/                 共享库：MCP 运行时外壳
├── mcp-sql-review/              MCP：SQL 体检与修法
├── mcp-change-risk/             MCP：变更风险与执行状态机
├── mcp-dbwatch/                 MCP：DBA 值守与根因
├── mcp-migration-diff/          MCP：国产化迁移差异
├── mcp-data-semantics/          MCP：数据口径治理
├── mcp-profile-hub/             MCP：档案中枢
├── dbt-orchestrator/            可选：跨 MCP 编排（需要装齐 6 个 MCP）
├── docs/                        跨仓文档（本文件 / PROTOCOL / 数据模型）
├── scripts/                     批量测试 / 开发安装 / 边界检查
└── tests/                       集成测试（全量信封一致性）
```

每个仓的固定形态：

```
<repo>/
├── pyproject.toml      独立包名 / 版本 / 依赖 / console_scripts
├── README.md           定位、安装、MCP 配置、工具表、边界声明
├── src/<pkg>/          源码（唯一的 import 根）
└── tests/test_<pkg>.py 独立可跑的回归测试（不依赖 pytest）
```

### 1.1 拆分原则

**一条能力一个 MCP，用户按需安装。** 装 `mcp-sql-review` 不该被迫装 `mcp-dbwatch`。

判断某段代码该放哪，问三个问题：

1. **只有一个 MCP 用** → 放它自己的 `src/<pkg>/` 里（如 `rules/`、`risk.py`）
2. **多个 MCP 用，且是纯技术能力（不含业务判断）** → 放共享库（如协议、方言、存储）
3. **多个 MCP 用，且是行业知识** → 放 `dbt-know`（阈值、指标、指纹）

### 1.2 四条边界（`scripts/check-boundaries.py` 强制检查）

| 方向 | 允许 | 说明 |
|---|---|---|
| MCP → 共享库 | ✅ | 这是正常的依赖方向 |
| 共享库 → 共享库 | ✅ | `dbt-mcp-kit` 依赖 `dbt-core`/`dbt-store` |
| **MCP → MCP** | ❌ | 跨能力协作只能走 `next_actions`，不许 import 代码 |
| **共享库 → MCP** | ❌ | 共享库必须对业务无知 |

跨 MCP 需要对方能力时，写进结果的 `next_actions`（见 PROTOCOL.md §4）。
这样单装一个 MCP 时它是「建议」，装了编排器时它是「执行指令」——同一份数据，两种用法。

### 1.3 为什么存储是库而不是 MCP

`dbt-store` 做成库，任何 MCP 直接写同一个 SQLite。
如果改成「通过 MCP 协议调用 profile-hub 写档案」，就等于**没装 hub 就丢数据**，
与「按需安装」直接冲突。`mcp-profile-hub` 只是这份存储的查询/导出外壳。


---

## 3. MCP 清单（工具表以各仓库 README 为准）

| MCP | 工具数 | 闭环 | 详表 |
|---|---|---|---|
| `sql-review` | 14 | 体检 → 修法 → 验证 → 计划对比 → 索引建议 | [README](../mcp-sql-review/README.md) |
| `change-risk` | 13 | 评估 → 回滚方案 → 预检 → 干跑 → 灰度 → 应用/回滚 | [README](../mcp-change-risk/README.md) |
| `dbwatch` | 15 | 快照 → 定级 → 根因 → 健康分 → 事故报告 → 归档 | [README](../mcp-dbwatch/README.md) |
| `migration-diff` | 12 | 类型映射 → 语法 Gap → 翻译 → 校验方案 → 对象对齐 → 工作量 | [README](../mcp-migration-diff/README.md) |
| `data-semantics` | 13 | 口径解析 → diff → 扇出 → 下钻 → 契约固化 | [README](../mcp-data-semantics/README.md) |
| `profile-hub` | 25 | 档案 / 基线 / 技术债账本 / 事故 / 知识包 | [README](../mcp-profile-hub/README.md) |

> **为什么不在总纲里复制工具表**：文档一旦与代码不同步就会说谎。
> 各仓 README 里的工具表由脚本从 `TOOLS` 字典导出的真实签名生成，
> 这里是索引，不是副本。

能力链路（每段都可独立使用，不必串起来）：

```
体检(sql-review) → 修法(sql-review) → 验证(sql-review)
      → 变更护栏(change-risk) → 值守(dbwatch) → 根因(dbwatch)
      → 迁移验证(migration-diff) → 口径治理(data-semantics)
                    ↑
              所有环节写档案 → dbt-store（唯一落点）→ profile-hub（查询/导出外壳）
```


---

## 5. 规则分册（rulebook）

规则 ID 命名：`<场景><序号>`，场景码如下。每条规则必须提供：**说明、触发条件、误报场景、修法映射、严重度来源**。

| 前缀 | 场景 | 示例 |
|---|---|---|
| `SEL` | SELECT 查询 | SEL001 SELECT *、SEL002 前导%LIKE、SEL003 字段套函数、SEL004 RAND 排序、SEL005 深分页 |
| `JOIN` | 关联 | JOIN001 隐式连接、JOIN002 扇出放大风险、JOIN003 关联字段类型不一致 |
| `IDX` | 索引 | IDX001 缺失复合索引、IDX002 冗余索引、IDX003 低选择性索引、IDX004 索引列顺序 |
| `DML` | 写操作 | DML001 无 WHERE 的更新、DML002 大批量单事务、DML003 INSERT 无列清单 |
| `DDL` | 结构变更 | DDL001 大表加索引、DDL002 列类型变更、DDL003 删列/删表不可逆 |
| `TYPE` | 数据类型 | TYPE001 隐式转换、TYPE002 字符集不一致关联、TYPE003 时间比较用字符串 |
| `TX` | 事务/锁 | TX001 长事务、TX002 事务内含远程调用风险、TX003 隔离级别不匹配 |
| `PART` | 分区 | PART001 分区键未命中、PART002 全分区扫描 |
| `AGG` | 聚合 | AGG001 COUNT(*) vs COUNT(col)、AGG002 大结果集排序、AGG003 DISTINCT 与 GROUP BY 混用 |
| `MY` / `PG` / `ORA` | 引擎专属 | PG001 缺 CONCURRENTLY、MY001 缺 ALGORITHM=INPLACE |

每条规则在 `rulebook/registry.py` 注册，支持按 dialect / 分类 / 严重度启停。

---

---

## 6. 质量门槛

- 每个工具必须有至少一个测试，输入输出的字段结构与 DESIGN 一致（测试里断言 envelope）。
- 无真实数据库时，**所有 Tier0/Tier1 的纯逻辑工具必须可测**（这是 CI 的前提）。
- 规则新增必须同步：规则本体 + 误报说明 + 至少 2 个正反样例 + 修法（或显式标注 `manual`）。
- 禁止在没有 `evidence` 的情况下给出 `confidence: high` 的结论。

---

---

## 7. 版本与发布

- **11 个仓各自语义化版本，独立发版**。共享库与 MCP 之间用 `>=0.x.0` 下限约束。
- 共享库只做向后兼容的新增；破坏性改动升主版本，并按「dbt-core → 6 个 MCP」顺序同步升依赖下限。
- 每个 MCP 有 `python -m <pkg>` 与 `dbt-<name>` 两个入口，互不依赖。

---

---

## 8. 编排层 dbt-orchestrator（§4.3 的运行端，可选组件）

`next_actions` 是留给多 Agent 编排的合同，但合同需要运行端。`dbt-orchestrator/` 就是这份合同的执行引擎，
**不为每一对 MCP 写胶水代码**。

### 8.1 两种驱动

- **自动追链 `run(mcp, tool, args)`**：给一个入口工具，引擎照着返回的 `next_actions` 自动跑下去。
  `optional=True` 的后续默认不追（`--follow-optional` 才追）。幂等收敛：同一 `(mcp, tool, args)`
  不重复执行；全局步数有上限，杜绝 `next_actions` 环导致的死循环。
- **配方 `run_recipe([...])`**：一份显式的、确定性的跨 MCP 流水线（JSON/YAML 列表），
  按给定顺序执行、**不**自动追 `next_actions`。适合 CI / 受控变更这类要可复现、可审计的场景。

### 8.2 引擎职责（与协议对齐）

- **关联透传**：一次 `run` 生成唯一 `correlation_id`，沿整条链路注入到每一步的调用参数；
  `instance_id`（实例）与 `sql_fingerprint`（SQL）同理透传，保证档案/记忆能串回同一调用链。
- **失败隔离**：某一步抛异常或返回 `ok=false` 只标记该步，不拖垮整条链，且不追它的 `next_actions`。
- **护栏不下放**：受控变更类工具（dry_run / apply）是否真执行，由调用方显式决定（recipe 里写明 / `--follow-optional`）；
  引擎本身不替业务拍板「要不要真改库」。缺必填参数（如 `precheck_change_window` 的 `url`）给出干净的
  `E_ARGS` 失败，而不是 TypeError 堆栈。
- **注册表单一事实来源**：`dbt-orchestrator/src/dbt_orchestrator/registry.py` 直接复用各 server 模块的 `TOOLS` 字典，
  保证编排层能调到的工具与 MCP 暴露的工具完全一致。

### 8.3 命令行

```bash
dbt run --seed sql-review.review_sql --sql "SELECT * FROM t ORDER BY RAND() LIMIT 1000" --dialect mysql
dbt run --seed sql-review.review_sql --sql "..." --follow-optional      # 连可选后续也追
dbt run --recipe dbt-orchestrator/examples/safe_change.json                # 确定性流水线
dbt run --seed ... --json                                              # 仅输出 trace JSON
python -m orchestrator --seed ...                                      # 也可直接跑引擎
```

### 8.4 已知约定约束

- 工具名全局唯一；若出现跨 MCP 重名（如 `list_metrics` / `record_observation`），调用时必须显式带 `mcp=<全称>`，
  注册表会将其标记为歧义，强制消歧。
- `next_actions` 的参数必须是自包含的（工具自己把 `sql`/`dialect` 等字面量带进 `args`），
  编排层不做跨步参数引用替换——保持"工具对自己产出的后续负责"的简单契约。
- 单 MCP 独立发版时，用 git subtree split，保持 dilivery 粒度。

---

---

## 9. 规则分册 rulebook（§5 的实现端）

§5 只定义了规则 ID 命名与分册设想，本章把它落到代码：`mcp-sql-review/src/sql_review/rulebook/`。
目标——**规则可治理**：拆成场景分册 + 单一注册表 + 按方言/分类/严重度/显式名单启停。

### 9.1 目录与职责

```
mcp-sql-review/src/sql_review/rulebook/
  core.py        共享基础设施：SEVERITY_ORDER / Finding / ReviewResult / RuleFn /
                _mk / _dedup_findings / _eq_pairs / _literal_of / _NUMERIC_COL_RE
  select.py     SEL* JOIN* AGG* ORDER* PART*（查询结构与结果集风险）
  dml.py        DML* INS* DDL* LOCK*（写操作与结构变更风险）
  type.py       TYPE* NULL*（类型与 NULL 语义）
  tx.py         TX*（事务/锁边界）
  engine.py     MY* PG* ORA*（引擎专属）
  subquery.py   SUBQ*（子查询风险）
  registry.py   Rule 数据类 + RuleRegistry：add / all / get / filtered
  profiles.py   命名配置：default / lenient / ci-gate（确定性、可审计）
  review.py     review_sql(sql, dialect, profile, disable, enable, only_*) → ReviewResult
  __init__.py   再导出，兼容旧 import 路径
```

`src/sql_review/rules.py` 是薄壳：`from sql_review.rulebook import *` 并补齐
`RULES` / `RULE_META` 两个历史导出名，保证 `fix.py` 与既有测试零改动。
规则分册归 `mcp-sql-review` 独有，不进共享库 —— 它是体检能力的一部分，其他 MCP 不该被迫装 31 条规则。

### 9.2 注册表与启停语义

`Rule(rule_id, name, category, severity, fn, why, false_positive, engines)`。`engines` 为空表示该规则方言无关；
非空（如 `["postgres","gaussdb","kingbase"]`）表示**仅对这些方言生效**。

`RuleRegistry.filtered(dialect=, categories=, severities=, include=, exclude=)` 的归并规则：

1. `engines` 过滤：规则 `engines` 非空且 `dialect` 不在其中 → 直接跳过（如 `PG001` 在 mysql 下不触发）。
2. `categories`：非空则只保留该分类的规则。
3. `severities`：非空则只保留该严重度的规则（用于 `lenient` / `ci-gate` 只看 blocker+high）。
4. `exclude`：显式名单最后剔除（团队已确认接受的写法，如 `DML004` REPLACE INTO）。
5. `include`：显式名单最后强制保留（即便被 1~4 过滤掉也留着）。

`include`/`exclude` 优先级最高，可在工具层按需覆盖 profile 默认值。

### 9.3 profile 与工具接口

`profiles.py` 提供：`default`（全量，仅受 engines 过滤）、`lenient`（只看 blocker+high）、
`ci-gate`（blocker+high，CI 门禁用）。未来可按客户加 `bank-core` / `saas-oltp` 等行业 profile。

`sql_review` 的 `review_sql` 工具新增三个可选参数（默认值保持旧行为完全不变）：

- `profile: str = "default"` —— 套用命名配置。
- `disable: list[str] = []` —— 本次禁用规则（叠加在 profile 之上）。
- `enable: list[str] = []` —— 本次强制启用规则。

新增工具 `list_profiles` 枚举可用 profile 及其门禁说明。

### 9.4 质量约束（沿用 §6）

- 规则搬移不得改变任何既有规则的行为与严重度（157 项回归测试为铁证）。
- 每条规则在注册时必须带 `why` 与 `false_positive`，缺失即 CI 失败。
- `engines` 字段缺失默认视为"全方言"，但引擎专属规则（MY/PG/ORA）必须显式声明。
