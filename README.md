# db-ai-toolchain

数据库生态的 AI 工具链 —— **11 个可独立安装的仓库**，不是一个大而全的 Agent。

> 每个 MCP 只装自己需要的，装一个不会拖进其他五个。
> 跨 MCP 的协作只通过结果里的 `next_actions` 声明，**不做代码级互相 import**。

## 仓库清单

| 仓库 | 类型 | 规模 | 干什么 |
|---|---|---|---|
| [`dbt-core`](dbt-core/) | 库 | 10 模块 | 协议信封 / 方言归一 / SQL 指纹 / 只读连库 / AST / 执行计划归一 / **MCP 运行时外壳** |
| [`dbt-store`](dbt-store/) | 库 | 1 模块 | 统一档案存储（SQLite，零依赖）—— 所有 MCP 的唯一数据落点 |
| [`dbt-know`](dbt-know/) | 库 | 3 模块 | 引擎阈值 / 指标字典 / 18 种故障指纹（零依赖，Tier0 的来源） |
| [`mcp-sql-review`](mcp-sql-review/) | **MCP** | 14 工具 | SQL 静态体检 → 修法 → 验证 → 计划对比 → 索引建议 |
| [`mcp-change-risk`](mcp-change-risk/) | **MCP** | 13 工具 | 变更风险因子模型 + 执行状态机（预检/干跑/灰度/回滚） |
| [`mcp-dbwatch`](mcp-dbwatch/) | **MCP** | 15 工具 | DBA 值守：快照 → 定级 → 根因 → 健康分 → 事故报告 |
| [`mcp-migration-diff`](mcp-migration-diff/) | **MCP** | 12 工具 | 国产化迁移：类型映射 → 语法 Gap → 校验方案 → 工作量 |
| [`mcp-data-semantics`](mcp-data-semantics/) | **MCP** | 13 工具 | 数据口径治理：口径 diff → 扇出检测 → 下钻 → 契约固化 |
| [`mcp-profile-hub`](mcp-profile-hub/) | **MCP** | 25 工具 | 档案中枢：实例档案 / 分位数基线 / 技术债账本 / 知识包 |
| [`dbt-orchestrator`](dbt-orchestrator/) | 可选 | — | 跨 MCP 编排（按 `next_actions` 追链 + recipe）。**要装齐 6 个 MCP** |

合计 **6 个 MCP / 92 个工具**，共享库 3 个。装任意一个 MCP，只会带上 `dbt-core`（含 MCP 外壳）、`dbt-store`，需要历史数据的再带 `dbt-know`。

## 按需安装：你只需要装你用的

| 你的场景 | 装这些 | 不会装上 |
|---|---|---|
| 只想在 CI 里卡 SQL 质量 | `mcp-sql-review` | 其余 5 个 MCP |
| 只想查线上为什么慢 | `mcp-dbwatch` | 其余 5 个 MCP |
| 只想评一条 DDL 能不能上 | `mcp-change-risk` | 其余 5 个 MCP |
| 只在做 Oracle→PG 迁移 | `mcp-migration-diff` | 其余 5 个 MCP |
| 只想解决「两个部门数字对不上」 | `mcp-data-semantics` | 其余 5 个 MCP |
| 想要跨会话的档案与月报 | `mcp-profile-hub` | 其余 5 个 MCP |
| 想一键跑「体检→修法→评估」整条链 | 6 个 MCP + `dbt-orchestrator` | — |

```bash
pip install ./mcp-sql-review              # 一个 MCP + 它需要的 2 个共享库
pip install "./mcp-dbwatch[mysql]"        # 需要连 MySQL 时补驱动
```

单独的 `dbt-core` 不含 MCP SDK —— 只做 SQL 解析/连库/协议时不用装它。
要写 MCP 服务端时才装 `dbt-core[mcp]`（各 MCP 仓已自带这个 extra）。

## 依赖图（实线 = 安装依赖，虚线 = 只在结果里声明下一步）

```
        ┌──────────────┐   ┌────────────┐   ┌────────────┐
        │ dbt-core     │   │ dbt-store  │   │ dbt-know   │
        │ 协议/方言/AST │   │ 档案存储    │   │ 引擎知识    │
        │ +MCP 运行时外壳│  │            │   │            │
        └──┬───────┬───┴───┬───────┬────┴───┬───────┬────┘
           │       │       │       │        │       │
      sql-review change-risk dbwatch migration semantics profile-hub
           ┆       ┆       ┆       ┆        ┆       ┆
           └───────┴───────┴───────┴────────┴───────┘
                    next_actions（协议层，非 import）
                               │
                    dbt-orchestrator（可选）
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
- 装了 `dbt-orchestrator` 时，它照着这份声明自动追链
- 每一步都透传 `correlation_id` / `instance_id`，事后可在 `dbt-store` 里串起来

## 快速开始（单 MCP）

```bash
pip install ./mcp-sql-review
dbt-sql-review tools
dbt-sql-review call review_sql --args '{"sql": "SELECT * FROM t WHERE 1=1", "dialect": "mysql"}'
```

挂到 MCP 客户端：见各仓库 README 里的 `mcpServers` 片段。

## 开发

```bash
python scripts/dev-install.py            # 把所需的仓以 editable 方式装进当前 venv
python scripts/test-all.py               # 依次跑 12 个测试套件（合计 225 项）
python scripts/test-all.py --only dbwatch
python scripts/check-boundaries.py       # 检查跨 MCP import（应为 0）
```

不想装也能跑：每个测试文件自带兄弟目录探测，`python mcp-dbwatch/tests/test_dbwatch.py` 直接可运行。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 总体架构、Tier 冷启动契约、规则分册编码、版本策略 |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | **跨仓唯一契约**：`dbt.result/v1` 信封、枚举、错误码、关联三件套 |
| [`docs/DATA_MODEL_AND_ALGORITHMS.md`](docs/DATA_MODEL_AND_ALGORITHMS.md) | 数据建模与算法设计（从需求倒推：四种判断本体、领域模型、算法取舍） |

## 版本与发布

四个共享库与 6 个 MCP 各自语义化版本，独立发版。
MCP 对共享库的依赖写成 `>=0.x.0` 下限；共享库只做向后兼容的新增，破坏性改动升主版本并同步升各 MCP。

## 明确不做

- 不做「一个 Agent 包打天下」—— 这是刻意拆成 6 个 MCP 的原因
- 不自动执行修复：所有写操作必须过 `mcp-change-risk` 的状态机与审批
- 不上时序数据库：SQLite 足够（10 实例 3 年的量级已核算）
- 不做 ML 黑盒：所有判断要能让 DBA 对着报告吵架
