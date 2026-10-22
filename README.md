# db-ai-toolchain —— 数据库生态 AI 工具链

> MCP 先行 → 共享内核 → 多 Agent 编排。
> 目标不是做一个工具，是做一条别人拿不走的数据库能力链。

```bash
# 安装（Python 3.10+）
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt

# 跑测试（不需要真实数据库）
python tests/test_all.py

# CLI
python cli/dbt.py tools                 # 列出全部 86 个工具
python cli/dbt.py review "SELECT * FROM a, b"
python cli/dbt.py watch 主从延迟突增     # 零历史也能用

# 编排：按 next_actions 自动串起跨 MCP 链路
python cli/dbt.py run --seed sql-review.review_sql --sql "SELECT * FROM t ORDER BY RAND() LIMIT 1000"
python cli/dbt.py run --recipe orchestrator/examples/safe_change.json   # 确定性流水线
```

---

## 目录结构

```
db-ai-toolchain/
├── DESIGN.md              ★ 顶层契约：工具清单 / 闭环 / IO 协议 / 交互口（改代码前先看它）
├── packages/dbcore/       共享内核（任何 MCP 都不得重复实现下列能力）
│   ├── protocol.py        统一返回信封 dbt.result/v1 + 错误码
│   ├── baselines.py       ★ Tier0 静态基线（各引擎阈值，每条带依据）
│   ├── signature.py       ★ Tier0 故障指纹库（18 种已知故障模式）
│   ├── rca.py             根因推理（Tier0 可用）
│   ├── rules.py           31 条静态规则 + 规则元数据
│   ├── fix.py             修正引擎（发现 → 给出可执行改法）
│   ├── verify.py          等价性 / 执行计划对比
│   ├── advisor.py         索引建议（含重复/前缀劝退）
│   ├── risk.py            变更风险矩阵（MySQL / PG 两套独立规则）
│   ├── execution.py       变更状态机（dry-run→灰度→回滚）
│   ├── migration.py       异构迁移：方言差异 + 一致性校验
│   ├── semantics.py       口径解析 / diff / 扇出 / 血缘 / 下钻
│   ├── plan.py            执行计划归一化（MySQL/PG/Oracle → 同一结构）
│   ├── metrics.py         指标归一化（同一概念一个名字）
│   ├── bootstrap.py       冷启动一次性采集器（只读）
│   ├── scanner.py         从文件 / git diff 抽 SQL
│   ├── profile_store.py   ★ 共享档案库（唯一数据落点）
│   └── report.py          报告渲染
│
├── servers/               一个 MCP 一个文件夹，可独立发版
│   ├── sql-review/        15 工具  体检 → 修正 → 验证（支持 profile / 规则启停）
│   ├── change-risk/       13 工具  风险 → dry-run → 灰度 → 回滚 → 留痕
│   ├── dbwatch/           15 工具  值守 + 根因（Tier0 可用）
│   ├── migration-diff/    12 工具  国产化迁移正确性验证
│   ├── data-semantics/    13 工具  数据口径治理
│   └── profile-hub/       25 工具  档案中枢（唯一数据落点）
│
├── cli/dbt.py             单一 CLI，可驱动全部能力（CI 用）
├── tests/test_all.py      143 项回归测试（不需要真实数据库）
└── requirements.txt
```

---

## 六个 MCP 与闭环

| MCP | 工具 | 闭环 |
|---|---|---|
| **sql-review** | 14 | 发现问题 → 给出修法 → 复验证明改对了 |
| **change-risk** | 13 | 风险评估 → 窗口检查 → dry-run → 灰度 → 回滚 → 留痕 |
| **dbwatch** | 15 | 症状/快照 → 假设排序 → 检查清单 → 处置 → 事故报告 |
| **migration-diff** | 12 | 差异扫描 → 等价改写 → 两层数据比对 → 就绪度报告 |
| **data-semantics** | 13 | 口径结构化 → 结构化 diff → 逐层下钻 → 契约固化 |
| **profile-hub** | 25 | 所有 MCP 的唯一数据落点 + 知识包导入导出 |

合计 **93 个工具**。完整清单见 `DESIGN.md §3`。

---

## 冷启动：为什么第一天就能给客户交付

> 质疑：*"值守和根因报告依赖积累的档案，但那是客户现场的数据，我拿不到。
> 除非客户先买前面的产品、养几个月档案，才能用上值守 —— 那我永远卖不出第一单。"*

这个质疑成立，所以用 **Tier 分级契约**写死在代码里：

| Tier | 输入 | 能产出 | 置信度 |
|---|---|---|---|
| **Tier0** | 一句症状描述 / 一段慢日志 | 结论 + 检查清单 + 成因 + 处置 + 事故报告 | medium（静态知识） |
| **Tier1** | 一次 `capture_snapshot` | 叠加真实元数据，假设收敛到 1~2 条 | high |
| **Tier2** | ≥2 次观测、跨 ≥7 天 | 趋势、漂移、容量预测、异常检测 | high |

代码层面的三条硬约束：

1. 每个返回必须带 `tier` 字段，**不允许默默降级**
2. Tier0 下不伪造"趋势/环比"，缺失历史时返回 `tier_upgrade` 指引
3. Tier0 的知识来自 `baselines.py` + `signature.py` —— **可增量维护的知识资产，不是硬编码默认值**

**销售路径因此变为**：今天就能交第一份报告 → 每次交付自动写档案 → 第二次起自然升 Tier2。
**积累是使用的副产品，不是前置条件。**

实证（零依赖，可立即复现）：

```bash
python cli/dbt.py watch 主从延迟突增
python cli/dbt.py watch --report 锁等待超时导致大量接口超时
```

第二条解法：`profile-hub.export_profile / import_profile` 让"脱敏的行业常识包"成为交付物 ——
新客户零历史时先导入同行业匿名基线作为 Tier1 起点。

---

## 统一 IO 协议（跨 MCP 交互口）

所有工具返回同一信封 `dbt.result/v1`，编排层可无差别解析：

```jsonc
{
  "ok": true, "schema": "dbt.result/v1",
  "mcp": "sql-review", "tool": "review_sql",
  "tier": "tier0", "verdict": "fail", "confidence": "medium",
  "summary": "发现 3 个问题，最严重：critical",
  "data": { ... },
  "evidence": [{"kind": "ast", "source": "rule.JOIN002", "fact": "...", "supports": "..."}],
  "next_actions": [{"mcp": "change-risk", "tool": "assess_change", "args": {...}}],
  "tier_upgrade": {"to": "tier1", "need": "...", "how": "...", "gain": "..."},
  "warnings": ["..."], "error": null
}
```

三条硬约定：

1. **单一落点** —— 除 `profile-hub` 外，任何 MCP 不得自建本地存储
2. **关联三件套** —— `correlation_id` / `instance_id` / `sql_fingerprint` 全程透传
3. **`next_actions` 即编排合同** —— 工具自己声明"下一步调谁"，编排层照着跑即可

---

## 快速开始

### 1. 挂到 MCP 客户端

```jsonc
// ~/.workbuddy/mcp.json
{
  "mcpServers": {
    "sql-review": {
      "command": "python",
      "args": ["-m", "servers.sql_review"],
      "cwd": "D:/work/luowenxing/db-ai-toolchain",
      "env": { "PYTHONPATH": "packages;." }
    },
    "dbwatch":     { "command": "python", "args": ["-m", "servers.dbwatch"],     "cwd": "..." },
    "change-risk": { "command": "python", "args": ["-m", "servers.change_risk"], "cwd": "..." },
    "profile-hub": { "command": "python", "args": ["-m", "servers.profile_hub"], "cwd": "..." }
  }
}
```

### 2. CI 门禁

```bash
# 只体检本次改动引入的 SQL，有问题则 exit 1
dbt review --diff --fail-on high
```

### 3. 连库做体检（只读账号即可）

```bash
python cli/dbt.py watch --snapshot mysql://readonly:pwd@host:3306/db
python cli/dbt.py review --file src/main/resources/mapper/OrderMapper.xml
```

采集过程**只执行 SELECT**，任何写语句都会先被 `dbcore.guard` 拦截。

---

## 质量门槛

- `python tests/test_all.py` → **143 项通过**（含误报回归用例）
- 新增规则必须同步：规则本体 + 误报说明 + 至少 2 个正反样例 + 修法
- 禁止在没有 `evidence` 的情况下给出 `confidence: high`
- 每个工具在异常输入下必须返回合法信封，不允许崩溃

---

## 路线图

- [x] 内核 + 6 个 MCP + CLI + 179 项测试
- [x] 编排层（`orchestrator/`）：按 `next_actions` 自动串起跨 MCP 链路（自动追链 + recipe 两种驱动）
- [x] 规则分册（`rulebook/`）：按场景拆成 6 个分册 + 单一注册表，支持按方言/分类/严重度/显式名单启停（profile：default/lenient/ci-gate/bank-core）
- [ ] 知识包市场：同行业匿名基线包
- [ ] 报告模板（Word/PDF）与客户门户
