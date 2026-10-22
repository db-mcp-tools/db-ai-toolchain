# 仓库约定（给 AI 协作者与未来的自己）

## 结构

10 个可独立安装的仓：3 个共享库（`sqltk-core` / `sqltk-store` / `sqltk-know`）
+ 6 个 MCP（`mcp-*`）+ 1 个可选编排器。顶层只放文档、脚本、集成测试。

## 四条不可违反

1. **MCP 之间不许互相 import**。需要别的能力时写进 `next_actions`，不写 `from xxx import`。
   改完跑 `python scripts/check-boundaries.py`，必须 0 违规。
2. **共享库不许知道业务**。`sqltk-core` / `sqltk-store` / `sqltk-know`
   里不许出现任何 MCP 的包名。
3. **只有 `sqltk-store` 能碰数据库文件**。除它之外任何仓不得自建存储、不得自己开 sqlite 连接。
4. **每个工具必须返回 `dbcore.protocol.Result`**。禁止直接返回裸 dict。

## 生成物不许手改

各仓的 `pyproject.toml`、`LICENSE`、`src/<pkg>/py.typed`、`.github/workflows/ci.yml`
都由 `scripts/gen-pyprojects.py` 从 `scripts/repo_config.py` 生成。

- 改版本号 / 作者 / 许可 / URL / 依赖 → 改 `repo_config.py`，跑 `make gen`
- 改完必须 `make check-meta`，CI 里这条是硬门禁
- 直接手改 pyproject 会在下次生成时被覆盖，且 CI 会红

## Tier 契约

任何诊断类工具必须在 Tier0（零历史）下可用。做不到就在入参缺失时明确报错并给出
`tier_upgrade` 指引，**绝不能**：

- 用 0 / 空值冒充「健康」
- 用单个数据点假装能判断趋势
- 默默降级而不在返回里声明

## 加一个 MCP 的检查清单

- [ ] 新目录 `<mcp-name>/`，含 `src/<pkg>/`、`tests/`、`pyproject.toml`、`README.md`
- [ ] `pyproject.toml` 只依赖共享库，**不依赖其他 `mcp-*`**
- [ ] 声明唯一的 console_script（`mcp-<name>`，与发行名一致）与 `python -m <pkg>` 入口
- [ ] 所有工具注册进 `TOOLS` 字典（README 的工具表由它生成）
- [ ] README 写清：定位、装什么、MCP 配置片段、工具表、**边界（不做什么）**
- [ ] 更新顶层 `README.md` 的仓库清单与按需安装矩阵
- [ ] 更新 `docs/ARCHITECTURE.md §3` 的 MCP 索引表
- [ ] 跑 `scripts/check-boundaries.py`、`scripts/test-all.py`，
      并在 `scripts/smoke-mcp.py` 的 SERVERS 表里登记（名字 / 工具数 / 探针工具）

## 加工具的检查清单

- [ ] 先改 `docs/PROTOCOL.md`（若涉及新字段/新错误码），再改代码
- [ ] 函数签名带 `instance_id` / `correlation_id`（跨 MCP 关联用）
- [ ] 至少一条 `add_evidence`（没有依据的结论不许 `confidence: high`）
- [ ] 声明 `next_actions`（下一步该调谁；跨 MCP 也照写，编排层会照此执行）
- [ ] 在所属仓的 `tests/test_*.py` 加一条信封一致性断言
- [ ] docstring 非空 —— 它就是 `tools/list` 里的 description，模型据此决定要不要调用
- [ ] 更新 `TOOLS` 后重新生成 README 工具表

## 命名

- 工具名：小写下划线
- 规则 ID：`<场景码><序号>`，场景码见 `docs/ARCHITECTURE.md §5`
- 证据 source：`模块.路径`，例如 `baselines.mysql.repl_lag_warn_s`、`rule.JOIN002`
- MCP 名：连字符（`sql-review`），Python 包名：下划线（`sql_review`）

## 提交

一个仓一个关注点。跨仓的接口变更（如协议加字段）按「先 `sqltk-core` → 再 6 个 MCP」的顺序提交，
每个仓各自 commit，message 里写明依赖的共享库版本下限。
