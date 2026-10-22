# CHANGELOG

10 个包共享版本号，一次发布覆盖全部。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.2.0] — 未发布

首个按"按需安装"形态组织的版本：10 个独立包，装一个 MCP 不会拖进其他五个。

### 包

| 包 | 类型 | 内容 |
|---|---|---|
| `sqltk-core` | 库 | `dbt.result/v1` 协议信封、方言归一、SQL 指纹、只读连库、AST 结构化、执行计划归一、MCP 运行时外壳 |
| `sqltk-store` | 库 | SQLite 档案存储（schema v3），零第三方依赖，所有 MCP 的唯一数据落点 |
| `sqltk-know` | 库 | 引擎阈值与版本能力位、逻辑指标字典、18 种故障指纹 |
| `mcp-sql-review` | MCP | 14 工具：规则分册体检 → 修法 → 验证 → 计划对比 → 索引建议 |
| `mcp-change-risk` | MCP | 13 工具：风险因子模型 → 回滚方案 → 预检 → dry-run → 执行状态机 |
| `mcp-dbwatch` | MCP | 15 工具：快照 → 定级 → 根因 → 健康分 → 事故报告 |
| `mcp-migration-diff` | MCP | 12 工具：类型映射 → 语法 Gap → 翻译 → 校验方案 → 工作量 |
| `mcp-data-semantics` | MCP | 13 工具：口径结构化 → diff → 扇出 → 下钻 → 契约固化 |
| `mcp-profile-hub` | MCP | 24 工具：实例档案 → 观测 → 分位数基线 → 技术债 → 知识包 |
| `sqltk-orchestrator` | 可选 | 跨 MCP 编排（`next_actions` 追链 + recipe），需装齐 6 个 MCP |

合计 91 个工具 / 3 个共享库。

### 架构

- 仓库边界由 `scripts/check-boundaries.py` 用 AST 强制：MCP 之间不许 `import`，
  共享库不许依赖 MCP。跨 MCP 协作只走结果里的 `next_actions`。
- `dbt-mcp-kit` 并入 `sqltk-core`（11 仓 → 10 仓）。判断标准落到可量化的门槛：
  骨架行数 ≥ 源码行数时不单独成仓。合并同时解除了"MCP 被间接绑上 sqltk-store"的隐性依赖。
- MCP 运行时外壳对 `mcp` SDK 改为**延迟导入**：`import dbcore` 不加载 MCP SDK，
  纯库用法（解析 / 连库 / 协议）与纯算法层都不必装 SDK。

### 修复（代码审查发现，全部有回归用例）

- **回滚语句必然语法错误**：`rollback_change` 把 `kind="check"` 步骤的占位符
  （`compare_with_baseline`）拼进了 SQL。改为只取 `kind=="sql"` 并逐条执行。
- **MySQL 上 DDL dry-run 会真改库却报"已回滚"**：`is_ddl` 用字符串前缀判断，
  `/* 注记 */ ALTER TABLE` 可绕过；MySQL DDL 隐式提交后 commit=False 救不回来。
  改为 AST 白名单判定，并在 `run_in_transaction` 库层再做一次校验（不信任调用方）。
- **PostgreSQL 上"灰度 5%"实际全量执行**：`_limit_sql` 的 PG 分支注释说要限定、
  代码直接返回原 SQL，随后照旧 commit 并回报成功。改为明确拒绝（`E_NOT_IMPLEMENTED`）
  并给替代方案；原 SQL 自带 LIMIT 时取 min，不再静默放行。
- **`WITH t AS (...) SELECT * FROM t` 被判 blocker**：`JOIN002` 把 CTE 名当表，
  报"笛卡尔积"。ci-gate 下会直接拦构建。
- **连接串明文密码进入模型上下文**：`Result.next()` 把带密码的 DSN 放进 `next_actions`，
  随工具返回值进 LLM 上下文与日志。改为协议层统一脱敏。
- **影子表方案一执行就崩**：`_shadow_plan` 引用未定义的变量，走该分支直接 `NameError`；
  表名提取的正则会把 `CREATE TABLE IF NOT EXISTS orders` 抓成 `IF`。改用 AST 提取。
- `fail()` 补 `code` 参数与 `detail.type`：原来同一类异常会同时出现 `E_ARGS` 和 `E_QUERY`，
  让"代码崩了"看起来像"用户传错了参数"。
- **编排器 CLI 有三个缺陷**（从装好的 wheel 实跑才发现）：
  `sqltk tools` 直接崩溃（照抄 MCP 模板，去 import 编排器里不存在的 `TOOLS`）；
  README 与架构文档主打的 `sqltk run --seed/--recipe` **从未实现**；
  同时 `cli.py` 与 `__main__.py` 是两份不同实现（参数风格与退出码都不一样）。
  现合并为一份：`tools` / `call` / `run --seed` / `run --recipe`，
  退出码 `0` 通过、`1` 有步骤失败**或最终判定 fail**、`2` 用法错误 —— 可直接当 CI 门禁。
  示例拆成 `safe_change.json`（L1 变更 + 干净查询 → warn，退出 0）与
  `find_problems.json`（故意写坏的 SQL → fail，退出 1）。

### 可用性

- 13 个工具补齐 `docstring`（原 README 工具表里是 `—`，模型只能靠名字猜用途），
  并对易混工具写清分工，例如 `abort_change`（还没上）vs `rollback_change`（已经上了要撤）。
- `mcp-profile-hub` 合并 `get_history` 到 `query_observations`（唯一真重复的一对），25 → 24 工具。
- 各仓 README 的工具表改为从 `TOOLS` 生成，不再手抄。

### 工程

- **发行名 `dbt-*` → `sqltk-*`**：`dbt-core` 在 PyPI 上已被 dbt Labs 占用（1.12.6），
  我们的 `dbt-core>=0.2.0` 会被解析到它的包，**6 个 MCP 全部装不上**；`dbt` 命令名同样冲突。
  改为 `sqltk-core` / `sqltk-store` / `sqltk-know` / `sqltk-orchestrator`（import 名不变，
  仍是 `dbcore` / `dbstore` / `dbknow` / `dbt_orchestrator`）；6 个 MCP 的 console script
  从 `dbt-sql-review` 改为与发行名一致（`mcp-sql-review`）。
  上传前 `publish.py` 会逐个向 PyPI 核对名字占用，被占用直接拒绝发布。
- 10 个仓的 `pyproject.toml` / `LICENSE` / `py.typed` / CI 由 `scripts/gen-pyprojects.py`
  从 `scripts/repo_config.py` 生成，CI 用 `--check` 挡漂移。
- 补齐发布元数据：MIT 许可 + LICENSE 文件、authors、keywords、classifiers、
  `[project.urls]`、`py.typed`。
- `requires-python` 按实际修正：3 个库 3.9+（已用真实 3.9 解释器验证），
  6 个 MCP 与编排器 3.10+（`mcp` SDK 自身的下限，3.9 上装不上）。
- `scripts/publish.py` 按依赖拓扑序构建 / 上传，`twine check` 全过；
  `docs/PUBLISHING.md` 记录流程与坑。
- 测试：12 个套件 / 271 项，`make check` 一条命令跑完。全部不依赖 pytest 与真实数据库。

### 已知未做

- 规则误报治理（`DML002` 对主键等值、`SUBQ001/002` 对非相关子查询、`TYPE001` 对日期列）；
  需要先定"WHERE 非主键等值时该不该报"的产品标准。
- `sqltk-store` 的连接复用（当前每次 `_conn()` 约 9ms，且每次都跑一遍建表 DDL）。
- 真实数据库的连通性验证与 MCP 客户端的实际挂载验证（现全部为无 DB 单测）。
