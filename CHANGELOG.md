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
- **旧档案库一升级就全挂**：索引 DDL 排在补列之前，`observations` 缺 `metric` 列时
  `CREATE INDEX ... (metric)` 抛 `no such column: metric`，而这段 DDL 每次 `_conn()` 都跑 ——
  于是所有读写档案的工具统一报 `E_QUERY`。装了旧版、`~/.dbtoolchain/store.db` 已存在的用户，
  升级后**没有一个工具能用**。改为建表 → 补列 → 建索引，且建索引失败只丢索引不丢功能。
- **6 个 MCP 的 `tools` / `call` 子命令根本不存在**：README 一直写着
  「命令行调试（不挂客户端也能用）：`mcp-sql-review tools`」，但入口只有一句
  `mcp.run()`，argv 被完全忽略 —— 敲 `tools` 实际是起了一个等 stdin 的 server，
  终端表现就是卡死。现由 `dbcore.mcp_kit.cli()` 提供：无参数起 server、
  `tools [--json]` 列表、`call <tool> --args '<json>'` 本地调一次并打印信封，
  退出码 0/1/2 与编排器一致。12 条回归用例钉住这套契约。
- **编排器 CLI 有三个缺陷**（从装好的 wheel 实跑才发现）：
  `sqltk tools` 直接崩溃（照抄 MCP 模板，去 import 编排器里不存在的 `TOOLS`）；
  README 与架构文档主打的 `sqltk run --seed/--recipe` **从未实现**；
  同时 `cli.py` 与 `__main__.py` 是两份不同实现（参数风格与退出码都不一样）。
  现合并为一份：`tools` / `call` / `run --seed` / `run --recipe`，
  退出码 `0` 通过、`1` 有步骤失败**或最终判定 fail**、`2` 用法错误 —— 可直接当 CI 门禁。
  示例拆成 `safe_change.json`（L1 变更 + 干净查询 → warn，退出 0）与
  `find_problems.json`（故意写坏的 SQL → fail，退出 1）。

### 规则误报治理（标准：判不出"这大概率是错的"就不报）

规则跑在没有 schema 的静态分析里，所以按"判得出才断言"重写了四条最容易喊狼的规则。
判定逻辑集中到 `rulebook/heuristics.py`（列名画像 / 点写判定 / 子查询相关性），三个分册共用。

| 规则 | 原行为（误报） | 现在 |
|---|---|---|
| `DML002` 批量写无分批 | 任何没写 LIMIT 的 UPDATE/DELETE 都报 | 点写（等值命中键列 `id`/`*_no`/`*_code`…）不报；`table_rows ≤ 10000` 不报；范围 / OR / 函数 / 非键等值照报 |
| `SUBQ001` SELECT 列表子查询 | 有子查询就报 | 只有**相关**子查询（引用外层列）才报 |
| `SUBQ002` IN (子查询) | 有 `IN (子查询)` 就报 | 只有**相关**子查询才报（非相关会被 semi-join/materialize） |
| `TYPE001` 隐式转换 | 任何字符串字面量都报 | 日期列（`*_at`/`*_date`/`*_ts`）与文本列（`*_name`/`*_code`/`*_no`/`status`…）跳过；数字形态字面量 + 数值列名才判 high |

配套：每条规则的 `META.false_positive` 改写为"什么情况下不报"；新增 18 条用例，
其中既有"不该报"，也有"该报的没被一起放过"（点写带引号的 `id='123'` 仍报隐式转换、
大表仍报 DML002、相关子查询仍报）。标准写进 `docs/ARCHITECTURE.md §5.1`。

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
- 测试：13 个套件 / 355 项，`make check` 一条命令跑完，并逐套件报出用例数
  （总数由 `scripts/test-all.py` 从各套件输出加总，不再手抄）。除真实库套件外均不依赖 pytest；
  真实库套件用标准库 SQLite（零基础设施）端到端验证连接器公共路径。
- **MCP 挂载冒烟** `scripts/smoke-mcp.py`：用真实 stdio 客户端把 6 个 server 各连一次，
  校验握手、工具数、每个工具的 `description` / `inputSchema`，并真调一个工具确认返回是
  `dbt.result/v1` 信封。单测是直接 import 函数，证明不了协议层挂得上 —— 上面那条旧库缺陷
  就是它第一次跑就抓到的。已进 `make check` 与 CI。
- **`scripts/verify-wheels.py`（`make verify`）**：把构建产物装进两个干净 venv 验证 ——
  A 只装 `mcp-sql-review`，断言 `dbknow` 没被拖进来（"按需安装"的唯一硬证明）；
  B 装 `sqltk-orchestrator`，断言 6 个 MCP 全到位、`pip check` 干净、挂载冒烟通过、
  编排器 CLI 起得来。原来这套流程只写在 `docs/PUBLISHING.md` 里靠人手敲。
- `publish.py` 补两道预检：`build`/`twine` 是否安装（否则 10 个包各报一次同样的错）、
  上传凭据是否存在（twine 缺凭据是交互式提问，脚本里表现为永久挂起）。
- `sqltk-store` 的建表 / 补列 / 建索引改为按 `PRAGMA user_version` 每库只做一次：
  单次调用 9~11ms → 约 5ms（原来 16 条 DDL 每次 `_conn()` 都跑一遍）。

### 真实数据库连接与端到端验证（本次新增）

- **连接器补 SQLite 真实连接后端**（标准库、零额外依赖）：`normalize_dialect` 早已把
  `sqlite` 列为受支持方言，但 `_connect()` 只实现了 mysql / postgres，等于"声明支持却连不上"。
  现在 `sqlite:///path/db.sqlite` / `sqlite://rel.db` / `sqlite://:memory:` 三种形式都能真连；
  只读查询、EXPLAIN（走 SQLite 的 `EXPLAIN QUERY PLAN`，能识别全表扫描 SCAN）、
  dry-run 事务回滚、变更窗口检查（SQLite 为嵌入式库，无实例级指标，**明确跳过而非假装通过**）全部打通。
- **修一处真 bug**：`sqlite3.Cursor` 不是上下文管理器，原 `with conn.cursor() as cur:` 在 SQLite
  后端直接 `TypeError`。改为显式 `conn.cursor()`，对 PyMySQL / psycopg 同样成立。
  这条就是「测试全绿 ≠ 可用」—— 之前从没真连过库，所以漏了。
- **新增 `tests/test_real_db.py`（已进 `make test` / CI）**：用**真实 SQLite 文件库**端到端验证
  连接器——真连库、真 SELECT 结果归一化、真 EXPLAIN、dry-run **真回滚（断言数据未被改动）**、
  commit **真落盘**、只读护栏在真实库上拦截写操作与 DDL。默认零基础设施；
  设 `DBT_TEST_MYSQL_URL` / `DBT_TEST_POSTGRES_URL` 可额外验证真实 MySQL / Postgres 分支
  （让实际厂商路径"测试就绪"，待有实例即可跑）。

### 已知未做

- `sqltk-store` 的连接复用（当前每次 `_conn()` 约 5ms，且每次都跑一遍建表 DDL）。
  建表/补列/建索引已改为按 `PRAGMA user_version` 只做一次（9~11ms → 5ms）；
  剩下的开销是每次 open 文件的固定成本（本机 Windows 实测 connect 本身就要 6ms），
  再往下只能常驻连接，那会引入 WAL 增长与多进程写锁的复杂度，暂不做。
- 真实 **MySQL / Postgres** 路径的端到端验证仍需真实实例：本机 3306 上的 MySQL 有密码且无可用
  连接串，故 `tests/test_real_db.py` 默认只跑 SQLite 分支，厂商分支需设
  `DBT_TEST_MYSQL_URL` / `DBT_TEST_POSTGRES_URL` 才执行（已写就，待有实例即跑）。
