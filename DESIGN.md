# DESIGN.md — db-ai-toolchain 顶层设计

> 本文件是**唯一契约来源**。所有 MCP 的工具清单、输入输出格式、闭环链路、跨 MCP 交互口都在此定义。
> 改动任何一个 MCP 前，先改这里。

版本：v0.1.0 · 最后更新：2026-10-22

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

## 1. 目录结构

```
db-ai-toolchain/
├── DESIGN.md                  ← 本文件（契约）
├── README.md                  ← 使用入口
├── CHANGELOG.md
├── CLAUDE.md / AGENTS.md      ← 给 AI 协作者的仓库约定
├── pyproject.toml
├── Makefile
├── .gitignore
│
├── packages/dbcore/           ← 共享内核（任何 MCP 都不得重复实现下列能力）
│   ├── protocol.py            统一返回信封 + 错误码 + 置信度
│   ├── ids.py                 instance_id / fingerprint / correlation_id
│   ├── dialect.py             多方言解析、归一、AST 工具
│   ├── extract.py             AST → 结构化对象（表/列/谓词/函数/排序/分页）
│   ├── guard.py               只读护栏、注入防护、危险操作拦截
│   ├── rulebook/              规则分册（按场景拆，避免 rules.py 膨胀）
│   │   ├── registry.py        规则注册与编排
│   │   ├── select.py          SELECT 类
│   │   ├── dml.py             INSERT/UPDATE/DELETE 类
│   │   ├── ddl.py             DDL / 表结构类
│   │   ├── tx.py              事务 / 锁 / 连接类
│   │   └── datax.py           数据类型与隐式转换类
│   ├── rules.py               规则引擎入口（调度 rulebook）
│   ├── baselines.py           ★ Tier0 静态基线（各版本阈值、硬件经验值）
│   ├── signature.py           ★ Tier0 故障指纹库（已知故障模式 → 检查项）
│   ├── plan.py                执行计划归一化（MySQL/PG/Oracle → 统一结构）
│   ├── fix.py                 修正引擎（发现问题 → 给出可执行改法）
│   ├── verify.py              等价性 / 执行计划对比 / 收益度量
│   ├── advisor.py             索引与统计信息建议
│   ├── risk.py                变更风险矩阵（按引擎分套）
│   ├── execution.py           变更执行护栏状态机（dry-run→灰度→回滚）
│   ├── semantics.py           口径解析 / 血缘 / 漂移检测骨架
│   ├── migration.py           异构迁移：方言差异检测 + 一致性校验计划
│   ├── rca.py                 根因推理（Tier0 可用）
│   ├── metrics.py             指标归一化（不同引擎同一指标一个名字）
│   ├── bootstrap.py           冷启动一次性采集器
│   ├── profile_store.py       ★ 共享档案库（SQLite，唯一真相源）
│   ├── connectors.py          安全连接层
│   ├── report.py              报告渲染（markdown / json）
│   └── logging_setup.py
│
├── servers/                   ← 一个 MCP 一个文件夹，可独立发版 / 独立 git 子树
│   ├── _shared/               MCP 公共脚手架（FastMCP 兼容层、工具装饰器）
│   ├── sql-review/
│   ├── change-risk/
│   ├── dbwatch/
│   ├── migration-diff/
│   ├── data-semantics/
│   └── profile-hub/
│
├── cli/dbt.py                 ← 单一 CLI，可驱动全部能力（CI 用）
├── orchestrator/              ← 多 Agent 编排预留（A2A / 编排脚本）
├── tests/                     ← 分层测试
└── fixtures/                  ← 脱敏样本数据
```

---

## 2. 统一输入输出协议（★ 跨 MCP 交互口）

### 2.1 返回信封 `dbt.result/v1`

**所有 MCP、所有工具，一律返回如下结构**。编排层因此可以无差别地解析任何工具的输出。

```jsonc
{
  "ok": true,
  "schema": "dbt.result/v1",
  "mcp": "sql-review",
  "tool": "review_sql",
  "version": "0.1.0",
  "ts": 1761...,
  "correlation_id": "01J...",        // 贯穿一次跨 MCP 调用链
  "instance_id": "sha256:ab12...",   // 由 DSN/主机指纹派生，不含密码
  "tier": "tier0",                   // tier0 | tier1 | tier2
  "took_ms": 12,

  "data": { },                       // 工具专属结构（见各 MCP 契约）

  "verdict": "warn",                 // pass | warn | fail | block | unknown
  "confidence": "medium",            // high | medium | low
  "summary": "一句话结论，给人和给 LLM 看",

  "evidence": [                      // 每条结论必须可溯源
    {
      "kind": "static_baseline" | "metadata" | "runtime_metric" | "plan" | "history" | "ast",
      "source": "baselines.mysql.innodb_lock_wait_timeout",
      "fact": "innodb_lock_wait_timeout=50s",
      "supports": "锁等待超时是当前现象的直接原因"
    }
  ],

  "next_actions": [                  // ★ 编排层用：下一步调谁
    {
      "mcp": "change-risk",
      "tool": "assess_change",
      "args": { "sql": "ALTER TABLE ..." },
      "reason": "该 SQL 需要在线 DDL 评估",
      "priority": 1,
      "optional": false
    }
  ],

  "tier_upgrade": {                  // 冷启动引导：怎样能升级到更高 Tier
    "to": "tier2",
    "need": "至少 2 次观测",
    "how": "调用 dbwatch.capture_snapshot，或接入 profile-hub.record_observation"
  },

  "warnings": ["未连接实例，表行数未知，风险等级可能偏低"],
  "error": null                       // {"code": "E_SQL_PARSE", "message": "...", "detail": ...}
}
```

失败时 `ok=false`，`error.code` 取自统一错误码表（见 `protocol.ERROR_CODES`）。

### 2.2 命名与类型约定

| 约定 | 说明 |
|---|---|
| 工具名 | 全部小写下划线：`review_sql`、`assess_change` |
| 实例标识 | `instance_id = "sha256:" + sha256(host|port|engine|db)[:16]`，**永不写入密码/表名以外的业务数据** |
| SQL 指纹 | `sql_fingerprint`：去掉字面量后的规范化哈希，用于跨 MCP 关联同一条 SQL |
| 时间 | 内部一律 epoch 秒；对外展示 ISO8601 |
| 金额/行数 | 一律 int 或 float，**禁止字符串拼接的表达**（如 "1,000,000 行"） |
| 方言字符串 | 归一到 `mysql|postgres|oracle|clickhouse|dameng|oceanbase|gaussdb|kingbase|sqlserver|sqlite` |
| 枚举 | 只允许本文档 §2.3 定义的取值，新增需改文档 |

### 2.3 核心枚举

```
verdict    : pass | warn | fail | block | unknown
confidence : high | medium | low
tier       : tier0 | tier1 | tier2
severity   : critical | high | medium | low | info
sql_kind   : executable | template | needs_metadata | manual
risk_level : L0 | L1 | L2 | L3
change_state: draft | prechecked | dryrun | shadow | canary | applied | rolled_back | aborted
```

---

## 3. MCP 清单与工具契约

图例：`◎` = 不需要连库（冷启动可用） · `●` = 需要连库 · `◆` = 需要先 bootstrap

### 3.1 `sql-review` — SQL 体检与修正（15 工具）

> 闭环：**发现问题 → 给出修法 → 复验证明改对了**

| 工具 | 连库 | 输入要点 | 输出要点 | 闭环位置 |
|---|---|---|---|---|
| `review_sql` | ◎ | sql, dialect, 可选 table_rows | verdict + findings[rule_id,severity,message,line] | ①发现 |
| `review_batch` | ◎ | sqls[] | 逐条 verdict + 汇总 gate=pass/fail | ① CI 门禁 |
| `review_diff` | ◎ | repo_root, base, head | 只体检 diff 中新增的 SQL | ① CI |
| `review_file` | ◎ | path（.sql/.xml/.java mapper） | 从 XML/注解/字符串里抽 SQL 后体检 | ① 存量扫描 |
| `suggest_fixes` | ◎ | sql, dialect, columns?, pk_column? | 每条 finding 的修法 + composed_sql | ②修正 |
| `verify_fix` | ◎ | original_sql, fixed_sql, dialect | 等价性判定 + 语义差异清单 | ③验证 |
| `compare_plans` | ● | dsn?, before/after plan 或 sql | 扫描行数/fsort/临时表 差异表 | ③验证 |
| `advise_indexes` | ◆ | sql 或 workload[], existing_indexes[] | DDL + 去重/前缀劝退 + 选择性排序 | ②修正 |
| `explain_sql` | ● | dsn, sql | 归一执行计划 + 可疑点 | ③验证 |
| `normalize_sql` | ◎ | sql, target_dialect | 方言转换（迁移场景复用） | 辅助 |
| `extract_objects` | ◎ | sql | 表/列/谓词/函数结构化（供其它 MCP 消费） | 交互口 |
| `fingerprint_sql` | ◎ | sql | sql_fingerprint + 归一化 SQL | 交互口 |
| `list_rules` | ◎ | category?, dialect? | 规则清单与开关状态 | 元工具 |
| `explain_rule` | ◎ | rule_id | 规则原理、误报场景、修法 | 元工具 |
| `list_profiles` | ◎ | — | 可用 profile + 全部规则 ID（配合 disable/enable） | 元工具 |

### 3.2 `change-risk` — 变更执行护栏（13 工具）

> 闭环：**风险评估 → 窗口检查 → dry-run → 灰度 → 回滚 → 留痕**。这是"从给建议到敢执行"的分水岭。

| 工具 | 连库 | 说明 |
|---|---|---|
| `assess_change` | ◎ | DDL 风险 L0-L3 + 锁影响 + 推荐工具 + 前置条件 |
| `generate_rollback` | ◎ | 反向 DDL；不可逆操作明确标记而非编造 |
| `precheck_change_window` | ● | 长事务/复制延迟/磁盘/参数 → go or no-go |
| `dry_run_change` | ● | 影子表 / 事务回滚方式验证可行性，不改数据 |
| `estimate_impact` | ◆ | 影响行数 + 预计耗时 + 锁等待面 |
| `plan_change_execution` | ◎ | 生成可审批的执行计划（步骤 + 检查点 + 回滚点） |
| `execute_canary` | ● | 受限灰度（按分片/比例/白名单），带超时与自动回滚 |
| `apply_change` | ● | 按计划执行，全程审计 |
| `rollback_change` | ● | 按 change_id 回滚到快照点 |
| `change_status` | ◎ | 状态机查询（change_state 枚举） |
| `list_changes` | ◎ | 按 instance/时间/状态检索，含责任人 |
| `abort_change` | ● | 中止并回滚到最近检查点 |
| `change_report` | ◎ | 单个 change_id 的完整时间线证据链 |

`execution.py` 里的状态机：`draft → prechecked → dryrun → shadow → canary → applied`，任一环节可 `aborted/rolled_back`。
**每次状态迁移写 profile-hub 审计表**，这是合规和责任划分的依据。

### 3.3 `dbwatch` — DBA 值守与根因报告（15 工具）

> **Tier0 必须可用**（见 §0.2）。即使客户今天第一次接入，也要能出一份报告。

| 工具 | 连库 | Tier | 说明 |
|---|---|---|---|
| `capture_snapshot` | ● | →Tier1 | 一次性采集：version/params/表/索引/会话/锁/慢查询 top |
| `classify_load` | ● | T0 | 当前是 CPU/IO/锁/连接/复制 哪类压力（只需一次快照） |
| `diagnose` | ●/◎ | **T0** | 根因推理：输入快照或现象描述，输出 Top-N 假设 + 证据 |
| `explain_symptom` | ◎ | **T0** | 纯文本症状（"主从延迟突然变大"）→ 检查清单 + 可能成因 |
| `analyze_slow_log` | ◎ | **T0** | 贴一段慢日志文本 → 聚合 TOP SQL + 模式归类（不需连库） |
| `analyze_lock` | ● | T1 | 锁等待链：谁阻塞谁、kill 谁安全 |
| `detect_blocking_chain` | ● | T1 | 递归阻塞环形检测 |
| `check_replication` | ● | T1 | 延迟/线程状态/错误，给修复顺序 |
| `capacity_forecast` | ◆ | T2 | 基于历史趋势的容量预测（无历史时明确降级为空，不编造） |
| `health_score` | ● | T1 | 实例健康评分 + 扣分明细（有基线对比） |
| `anomaly_report` | ◆ | T2 | 与自身历史基线比对出异常；无历史则退化为静态阈值告警 |
| `incident_report` | ◎ | T0 | 把上面任意输出渲染成可交付的事故报告（markdown） |
| `oncall_checklist` | ◎ | T0 | 故障时该按顺序看什么的清单（静态知识） |
| `record_observation` | ◎ | — | 把本次观测写入共享档案（供 profile-hub 消费） |

**Tier0 的知识来源**：`baselines.py`（各引擎/版本的阈值）+ `signature.py`（~40 种已知故障指纹，每种附「现象 → 3~8 个必查项 → 常见成因 → 处置动作」）。这部分是**可增量维护的知识资产**，也是最先能体现专业深度的地方。

### 3.4 `migration-diff` — 国产化迁移正确性验证（12 工具）

Oracle/MySQL → PG / 达梦 / OceanBase / GaussDB / Kingbase。

| 工具 | 说明 |
|---|---|
| `detect_dialect_gap` | 静态扫描源 SQL，列出目标库不支持的语法/函数/类型 |
| `translate_sql` | 方言改写（含 ROWNUM→LIMIT、NVL→COALESCE、DECODE→CASE 等映射表） |
| `check_object_parity` | 表/列/索引/约束/视图/序列/存储过程 数量与定义比对 |
| `compare_data` | 分批 checksum 比对计划生成 + 差异定位（不传全量数据） |
| `verify_consistency` | 生成抽样+全量两层校验 SQL，供客户在自己环境执行 |
| `check_charset_collation` | 字符集/排序规则差异导致的长度与排序问题 |
| `plan_migration` | 迁移批次编排（依赖顺序 + 可并行度 + 回滚批次） |
| `check_null_semantics` | Oracle 空串=NULL 等语义陷阱 |
| `check_type_mapping` | 类型映射表 + 精度溢出预警（NUMBER→numeric 等） |
| `estimate_rewrite_effort` | 按存储过程/函数复杂度估算改造工作量 |
| `generate_migration_report` | 迁移就绪度报告 |
| `record_migration_result` | 写入共享档案，积累跨项目迁移知识 |

### 3.5 `data-semantics` — 数据口径治理（11 工具）

解决"两个部门算出来的 DAU 不一样"。

| 工具 | 说明 |
|---|---|
| `parse_metric_definition` | 自然语言/SQL → 结构化口径（度量、维度、过滤、时间粒度、去重键） |
| `compare_definitions` | 两个口径的结构化差异 diff（列出不等价点） |
| `build_lineage` | 从 SQL 依赖推导血缘（表级/列级） |
| `detect_drift` | 同一指标不同时间的结果漂移检测 |
| `reconcile` | 两个结果不一致时，按差异树逐层下钻定位到具体 JOIN/过滤/去重 |
| `suggest_canonical` | 推荐标准口径 + 影响面评估 |
| `check_grain` | 粒度冲突检测（明细 vs 聚合混用导致的翻倍） |
| `detect_fanout` | JOIN 扇出放大检测（行数翻倍根因，最常见） |
| `check_timezone_window` | 时区/日期窗口边界差异 |
| `generate_metric_contract` | 生成指标契约文档（YAML） |
| `record_definition` | 写入指标字典（共享档案） |

### 3.6 `profile-hub` — 档案中枢（10 工具）

**所有 MCP 的唯一数据落点**，避免"各写各的日志"。

| 工具 | 说明 |
|---|---|
| `upsert_instance` | 注册实例元数据 |
| `record_observation` | 写一次观测（结构化，带 ts） |
| `get_instance_profile` | 读实例全貌 |
| `query_observations` | 按 kind/时间窗查询 |
| `get_history` | 某个 SQL/对象的历史表现 |
| `set_baseline` / `get_baseline` | 基线读写（Tier2 的必需数据来源） |
| `record_audit` / `query_audit` | 审计留痕（谁在什么时候做了什么） |
| `link_cross_instance` | 异构实例关联（同一业务的多个引擎） |
| `export_profile` | 导出为可迁移的知识包（换机/交付给客户） |
| `import_profile` | 导入知识包（**这就是"把公开案例库带给新客户"的通道**） |
| `store_stats` | 档案库健康度与体积 |

> ★ **冷启动的第二条解法**：`export_profile/import_profile` 让"脱敏的行业常识包"成为可交付物。
> 新客户即使零历史，也能先 `import_profile` 一份同行业的匿名基线（几十个同类实例的中位数）作为 Tier1 起点。

---

## 4. 跨 MCP 交互的三条硬约定

1. **单一落点**：除 `profile-hub` 外，任何 MCP **不得自建本地存储**。
   （原 `sql_review` 里的 `record_observation` 一律改为调用 `profile-hub` 的客户端。）
2. **关联三件套**：`correlation_id`（一次调用链）、`instance_id`（一个实例）、`sql_fingerprint`（一条 SQL）。
   任何跨 MCP 调用必须透传这三者，否则编排层无法串起来。
3. **`next_actions` 即编排合同**：工具输出里声明"下一步建议调谁"，编排层可以直接照着跑，
   不需要为每个 MCP 单独写胶水代码。这是留给多 Agent 编排的唯一接口。

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

## 6. 质量门槛

- 每个工具必须有至少一个测试，输入输出的字段结构与 DESIGN 一致（测试里断言 envelope）。
- 无真实数据库时，**所有 Tier0/Tier1 的纯逻辑工具必须可测**（这是 CI 的前提）。
- 规则新增必须同步：规则本体 + 误报说明 + 至少 2 个正反样例 + 修法（或显式标注 `manual`）。
- 禁止在没有 `evidence` 的情况下给出 `confidence: high` 的结论。

---

## 7. 版本与发布

- `packages/dbcore` 版本号与仓库版本一致，语义化版本。
- 每个 `servers/<name>/` 独立 `README.md` + `__main__.py`，可 `python -m servers.<name>` 启动。

---

## 8. 编排层 orchestrator（§4.3 的运行端）

`next_actions` 是留给多 Agent 编排的合同，但合同需要运行端。`orchestrator/` 就是这份合同的执行引擎，
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
- **注册表单一事实来源**：`orchestrator/registry.py` 直接复用各 server 模块的 `TOOLS` 字典，
  保证编排层能调到的工具与 MCP 暴露的工具完全一致。

### 8.3 命令行

```bash
dbt run --seed sql-review.review_sql --sql "SELECT * FROM t ORDER BY RAND() LIMIT 1000" --dialect mysql
dbt run --seed sql-review.review_sql --sql "..." --follow-optional      # 连可选后续也追
dbt run --recipe orchestrator/examples/safe_change.json                # 确定性流水线
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

## 9. 规则分册 rulebook（§5 的实现端）

§5 只定义了规则 ID 命名与分册设想，本章把它落到代码：`packages/dbcore/rulebook/`。
目标——**规则可治理**：拆成场景分册 + 单一注册表 + 按方言/分类/严重度/显式名单启停。

### 9.1 目录与职责

```
packages/dbcore/rulebook/
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

`packages/dbcore/rules.py` 退化为薄壳：`from dbcore.rulebook import *` 并补齐
`RULES` / `RULE_META` 两个历史导出名，保证 `fix.py`、`__init__.py`、既有测试零改动。

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
