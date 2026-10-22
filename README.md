# db-ai-toolchain

数据库生态的 AI 工具链 —— **10 个可独立安装的仓库**，不是一个大而全的 Agent。

> 每个 MCP 只装自己需要的，装一个不会拖进其他五个。
> 跨 MCP 的协作只通过结果里的 `next_actions` 声明，**不做代码级互相 import**。

## 仓库清单

| 仓库 | 类型 | 规模 | 干什么 |
|---|---|---|---|
| [`sqltk-core`](sqltk-core/) | 库 | 10 模块 | 协议信封 / 方言归一 / SQL 指纹 / 只读连库 / AST / 执行计划归一 / **MCP 运行时外壳** |
| [`sqltk-store`](sqltk-store/) | 库 | 1 模块 | 统一档案存储（SQLite，零依赖）—— 所有 MCP 的唯一数据落点 |
| [`sqltk-know`](sqltk-know/) | 库 | 3 模块 | 引擎阈值 / 指标字典 / 18 种故障指纹（零依赖，Tier0 的来源） |
| [`mcp-sql-review`](mcp-sql-review/) | **MCP** | 14 工具 | SQL 静态体检 → 修法 → 验证 → 计划对比 → 索引建议 |
| [`mcp-change-risk`](mcp-change-risk/) | **MCP** | 13 工具 | 变更风险因子模型 + 执行状态机（预检/干跑/灰度/回滚） |
| [`mcp-dbwatch`](mcp-dbwatch/) | **MCP** | 15 工具 | DBA 值守：快照 → 定级 → 根因 → 健康分 → 事故报告 |
| [`mcp-migration-diff`](mcp-migration-diff/) | **MCP** | 12 工具 | 国产化迁移：类型映射 → 语法 Gap → 校验方案 → 工作量 |
| [`mcp-data-semantics`](mcp-data-semantics/) | **MCP** | 13 工具 | 数据口径治理：口径 diff → 扇出检测 → 下钻 → 契约固化 |
| [`mcp-profile-hub`](mcp-profile-hub/) | **MCP** | 24 工具 | 档案中枢：实例档案 / 分位数基线 / 技术债账本 / 知识包 |
| [`sqltk-orchestrator`](sqltk-orchestrator/) | 可选 | — | 跨 MCP 编排（按 `next_actions` 追链 + recipe）。**要装齐 6 个 MCP** |

合计 **6 个 MCP / 91 个工具**，共享库 3 个。装任意一个 MCP，只会带上 `sqltk-core`（含 MCP 外壳）、`sqltk-store`，需要历史数据的再带 `sqltk-know`。

### 命名：发行名与 import 名是两件事

| `pip install` | `import` | 命令 |
|---|---|---|
| `sqltk-core` | `dbcore` | — |
| `sqltk-store` | `dbstore` | — |
| `sqltk-know` | `dbknow` | — |
| `mcp-sql-review` 等 6 个 | 下划线同名（`sql_review` …） | 与发行名相同 |
| `sqltk-orchestrator` | `dbt_orchestrator` | `sqltk` |

**为什么不用 `dbt-*` 前缀**（2026-10 实测）：`dbt-core` 已被 dbt Labs 占用（PyPI 上 1.12.6）。
我们若声明 `dbt-core>=0.2.0`，pip 会解析到**它的**包，6 个 MCP 全都装不上；
它还占用 `dbt` 这个控制台命令，与编排器 CLI 同名。`sqltk` = SQL toolkit。

改前缀只需改 `scripts/repo_config.py` 里的 `name` 与 `deps`，重跑 `make gen`；
`publish.py` 上传前会逐个问 PyPI 确认名字没被占用。

## 按需安装：你只需要装你用的

| 你的场景 | 装这些 | 不会装上 |
|---|---|---|
| 只想在 CI 里卡 SQL 质量 | `mcp-sql-review` | 其余 5 个 MCP |
| 只想查线上为什么慢 | `mcp-dbwatch` | 其余 5 个 MCP |
| 只想评一条 DDL 能不能上 | `mcp-change-risk` | 其余 5 个 MCP |
| 只在做 Oracle→PG 迁移 | `mcp-migration-diff` | 其余 5 个 MCP |
| 只想解决「两个部门数字对不上」 | `mcp-data-semantics` | 其余 5 个 MCP |
| 想要跨会话的档案与月报 | `mcp-profile-hub` | 其余 5 个 MCP |
| 想一键跑「体检→修法→评估」整条链 | 6 个 MCP + `sqltk-orchestrator` | — |

```bash
pip install mcp-sql-review                # 从 PyPI：一个 MCP + 它需要的 2 个共享库
pip install "mcp-dbwatch[mysql]"          # 需要连 MySQL 时补驱动
pip install ./mcp-sql-review              # 从源码：先 git clone，在工具链根目录执行
```

单独的 `sqltk-core` 不含 MCP SDK —— 只做 SQL 解析/连库/协议时不用装它。
要写 MCP 服务端时才装 `sqltk-core[mcp]`（各 MCP 仓已自带这个 extra）。

## 依赖图（实线 = 安装依赖，虚线 = 只在结果里声明下一步）

```
        ┌──────────────┐   ┌────────────┐   ┌────────────┐
        │ sqltk-core     │   │ sqltk-store  │   │ sqltk-know   │
        │ 协议/方言/AST │   │ 档案存储    │   │ 引擎知识    │
        │ +MCP 运行时外壳│  │            │   │            │
        └──┬───────┬───┴───┬───────┬────┴───┬───────┬────┘
           │       │       │       │        │       │
      sql-review change-risk dbwatch migration semantics profile-hub
           ┆       ┆       ┆       ┆        ┆       ┆
           └───────┴───────┴───────┴────────┴───────┘
                    next_actions（协议层，非 import）
                               │
                    sqltk-orchestrator（可选）
```

**硬约束**：任何 MCP 的源码里都不出现 `from sql_review import ...` 这类跨 MCP import。
`scripts/check-boundaries.py` 会检查这条，CI 里跑。

## 跨 MCP 怎么协作

不是靠调用链硬编码，而是靠**结果里声明**：

```jsonc
// mcp-sql-review.review_sql 的返回片段
"next_actions": [
  { "mcp": "sql-review",  "tool": "suggest_fixes", "args": {"sql": "..."}, "optional": false },
  { "mcp": "change-risk", "tool": "assess_change", "args": {"sql": "..."}, "optional": true  }
]
```

- 单个 MCP 独立使用时，`next_actions` 就是给人和上层 Agent 的建议
- 装了 `sqltk-orchestrator` 时，它照着这份声明自动追链
- 每一步都透传 `correlation_id` / `instance_id`，事后可在 `sqltk-store` 里串起来

## 快速开始（单 MCP）

```bash
pip install ./mcp-sql-review
mcp-sql-review tools
mcp-sql-review call review_sql --args '{"sql": "SELECT * FROM t WHERE 1=1", "dialect": "mysql"}'
```

挂到 MCP 客户端：见各仓库 README 里的 `mcpServers` 片段。

## 开发

```bash
python scripts/dev-install.py            # 把所需的仓以 editable 方式装进当前 venv
python scripts/test-all.py               # 依次跑 12 个测试套件（合计 304 项，逐套件报数）
python scripts/test-all.py --only dbwatch
python scripts/check-boundaries.py       # 检查跨 MCP import（应为 0）
python scripts/smoke-mcp.py              # 用真实 MCP 客户端把 6 个 server 各挂一次
python scripts/verify-wheels.py           # 发布前：wheel 装进干净 venv 验证（先 make build）
```

不想装也能跑：每个测试文件自带兄弟目录探测，`python mcp-dbwatch/tests/test_dbwatch.py` 直接可运行。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 总体架构、Tier 冷启动契约、规则分册编码、版本策略 |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | **跨仓唯一契约**：`dbt.result/v1` 信封、枚举、错误码、关联三件套 |
| [`docs/DATA_MODEL_AND_ALGORITHMS.md`](docs/DATA_MODEL_AND_ALGORITHMS.md) | 数据建模与算法设计（从需求倒推：四种判断本体、领域模型、算法取舍） |

## 版本与发布

10 个包版本锁步（共用同一个 `VERSION`），但各自独立发版、独立安装。
版本号与所有共享元数据的唯一来源是 `scripts/repo_config.py`：改一处 → `make gen` 同步到
10 个 `pyproject.toml` / `LICENSE` / `py.typed` / CI 配置，CI 用 `--check` 挡住漂移。

```bash
make gen            # 重新生成 10 个仓的 pyproject / LICENSE / py.typed / CI
make check-meta     # 只校验是否漂移（不改文件）
make build          # 按依赖拓扑序构建 10 个 wheel 并过 twine check
make publish-test   # 上传 TestPyPI（预演）
make publish        # 上传 PyPI
```

依赖约束随版本自动跟进（`>=0.2.0`），发布顺序由依赖图算出，不会出现"下游先发、上游还没上"的死解。
完整流程、TestPyPI 的局限、常见失败见 [`docs/PUBLISHING.md`](docs/PUBLISHING.md)。

- 许可：MIT
- Python：3 个库 3.9+；6 个 MCP 与编排器 3.10+（`mcp` SDK 自身的下限，非我们的选择）
- 三个库都带 `py.typed`，下游能拿到类型提示；`sqltk-core` 的 MCP 依赖是延迟导入，纯库用法不装 SDK

## 明确不做

- 不做「一个 Agent 包打天下」—— 这是刻意拆成 6 个 MCP 的原因
- 不自动执行修复：所有写操作必须过 `mcp-change-risk` 的状态机与审批
- 不上时序数据库：SQLite 足够（10 实例 3 年的量级已核算）
- 不做 ML 黑盒：所有判断要能让 DBA 对着报告吵架
