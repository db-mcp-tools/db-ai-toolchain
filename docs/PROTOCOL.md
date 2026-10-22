# PROTOCOL.md — 跨仓唯一契约 `dbt.result/v1`

> 这份文件是 **11 个仓库共同遵守的唯一契约**。任何 MCP 的返回格式、错误码、
> 关联字段都以这里为准；改协议要同步升所有仓库的次版本号。
>
> 与之配套的两份文档：总体架构见 [`ARCHITECTURE.md`](ARCHITECTURE.md)，
> 算法与数据模型见 [`DATA_MODEL_AND_ALGORITHMS.md`](DATA_MODEL_AND_ALGORITHMS.md)。

版本：v0.2.0 · 最后更新：2026-10-22

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
  "version": "0.1.0",                // 信封协议版本（dbcore.PROTOCOL_VERSION），**不是**包的版本号
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

---

## 4. 跨 MCP 交互的三条硬约定

1. **单一落点**：除 `profile-hub` 外，任何 MCP **不得自建本地存储**。
   （原 `sql_review` 里的 `record_observation` 一律改为调用 `profile-hub` 的客户端。）
2. **关联三件套**：`correlation_id`（一次调用链）、`instance_id`（一个实例）、`sql_fingerprint`（一条 SQL）。
   任何跨 MCP 调用必须透传这三者，否则编排层无法串起来。
3. **`next_actions` 即编排合同**：工具输出里声明"下一步建议调谁"，编排层可以直接照着跑，
   不需要为每个 MCP 单独写胶水代码。这是留给多 Agent 编排的唯一接口。

---

---

## 5. 协议变更流程

1. 只允许**向后兼容**的变更：加字段、加枚举值、加错误码。
2. 语义变更（改字段含义、改默认值）必须升主版本，并在 CHANGELOG 里写明迁移方式。
3. 新增字段必须在 `sqltk-core/protocol.py` 里定义 + 在本文件登记 + 至少一个测试断言。
4. 各 MCP 的 `__version__` 必须 ≥ 它依赖的 `dbcore.PROTOCOL_VERSION` 的兼容下限。
