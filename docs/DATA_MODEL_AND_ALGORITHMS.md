# 数据建模与算法设计

> **定位**：本文件回答「这个工具链的算法与数据应该长什么样」。
> 写作顺序是**需求 → 判断本体 → 领域模型 → 算法 → 存储模型 → 现状差距**，实现细节一律服从本文件。
> 与 `DESIGN.md` 的分工：DESIGN 管「MCP 与工具契约」，本文件管「判断的正确性与数据的形状」。两者冲突时，先改本文件再改 DESIGN。

版本：v1.0 · 2026-10-22

---

## 0. 第一性问题：买家买的是四种判断

抛开所有实现，客户付费是为了让系统替他做判断。把五个场景（SQL 体检、变更护栏、DBA 值守、迁移验证、口径治理）逐一拆到最底层，**所有需求都归结为四种判断**：

| 判断类型 | 业务问题原文 | 对应场景 | 交付物 |
|---|---|---|---|
| **J1 好坏判断** | 这条 SQL / 这个变更 / 这个实例，**现在**好不好？ | 体检、变更风险、健康分 | verdict + 分级 + 修法 |
| **J2 因果判断** | 为什么变慢？为什么锁住？为什么两个部门数字对不上？ | 根因、口径下钻 | 结论 + 证据链 + 处置 |
| **J3 趋势判断** | 会不会出事？还撑多久？跟上周比是不是不对劲？ | 容量预测、异常检测、口径漂移 | 预测区间 + 剩余天数 + 异常事件 |
| **J4 等价判断** | 改过之后还等价吗？迁移过去数据一致吗？口径定义相同吗？ | 修复验证、计划对比、迁移校验、口径 diff | 等价结论 + 残余风险 |

**推论一**：四种判断共享同一个输入基座——**观测（Observation）**。
**推论二**：四种判断共享同一个输出契约——`dbt.result/v1`（已有，不变）。
**推论三**：四种判断对历史数据的依赖不同，这正好映射 Tier 契约：

| 判断 | Tier0（零历史） | Tier1（一次快照） | Tier2（时序） |
|---|---|---|---|
| J1 好坏 | ✅ 静态规则即可 | ✅ 叠加真实元数据更准 | 复利用途（历史 findings 聚合） |
| J2 因果 | ✅ 症状→假设库 | ✅ 用真实指标检验假设 | ✅ 基线偏离作为证据 |
| J3 趋势 | ❌ **原理上不可行**，必须如实报 E_NOT_TIER2 | 部分（单点 vs 静态阈值） | ✅ 唯一能做对的地方 |
| J4 等价 | ✅ 静态等价 | ✅ dry-run / 校验和 | 历史（修复前后对比） |

**J3 在 Tier0 不可行是物理事实，不是实现缺陷**——设计上必须让它在样本不足时显式失败并引导升级，任何"零数据也给趋势"的实现都是错的。

---

## 1. 领域模型

### 1.1 实体总览（六域）

```
身份域          观测域                知识域
┌──────────┐   ┌──────────────┐   ┌──────────────────┐
│ Instance │──→│ Observation  │←──│ MetricDef(指标字典)│
│ SqlProfile│  │  (唯一写入口) │   │ Baseline(静态/动态)│
│ Change   │   │ Symptom      │   │ Signature(故障指纹)│
└──────────┘   └──────┬───────┘   │ Rule(体检规则)     │
                      │           └────────┬─────────┘
                      ↓                    ↓
       ┌─────────────────────────────────────────────┐
       │              判断域（四种判断的产出实体）          │
       │  Finding(J1)  Diagnosis(J2)  Forecast(J3)    │
       │  HealthScore(J1) AnomalyEvent(J3)            │
       │  EquivalenceVerdict(J4)                      │
       └──────────────────┬──────────────────────────┘
                          ↓
       ┌─────────────────────────────────────────────┐
       │ 行动域：Remediation(修法) MetricContract(契约)  │
       │ 闭环域：FindingLifecycle(技术债账本) Incident   │
       └─────────────────────────────────────────────┘
```

### 1.2 身份域

| 实体 | 主键 | 说明 | 关键字段 |
|---|---|---|---|
| `Instance` | instance_id（DSN 派生哈希，不含密码） | 被管数据库实例 | dialect, version, role(primary/replica), env(prod/staging), first_seen, last_seen |
| `SqlProfile` | sql_fingerprint | 一条 SQL 的**一生** | first_seen, last_seen, occurrence_count, sample_sql, kind |
| `Change` | change_id | 变更单（状态机，已有） | state, sql, risk_level, approver, rollback_plan |

`SqlProfile` 是当前建模**完全缺失**的实体。没有它，"这条 SQL 这个月被抓了 40 次"这种最高价值的信息无处安放。

### 1.3 观测域（★ 整个系统的地基）

**Observation 是唯一的数据写入口**，事件溯源式 append-only。派生数据（基线、趋势、健康分）全部是 Observation 上的视图，**不允许任何模块绕过它自建存储**（profile-hub 例外，它是 Observation 的管理员）。

每种 Observation 有**强类型 payload 契约**（不是自由 JSON）：

| kind | payload 契约（必填字段） | 谁写入 | 用途 |
|---|---|---|---|
| `metric_sample` | `{metric, value, dims?, source}` | capture_snapshot / record_observation | J3 全部算法的原料 |
| `snapshot` | `{version, params{}, top_tables[], locks[], replicas[]}` | bootstrap / capture_snapshot | J2 证据、J1 元数据 |
| `finding` | `{finding_id, rule_id, object, severity, status}` | sql_review / change_risk | 技术债账本 |
| `change_event` | `{change_id, from_state, to_state, actor}` | change_risk 状态机 | 变更-异常时间关联 |
| `diagnosis_event` | `{symptom, top_hypothesis, confidence}` | dbwatch.diagnose | 事故复盘、假设库校准 |
| `symptom` | `{text, detected_at, source(anomaly\|human)}` | anomaly_report / 人输入 | J2 的触发器 |

**观测数据的关键建模决策（当前实现没做、必须补）：**

1. **Counter / Gauge 二分**。`Queries`、`Innodb_rows_read` 这类 status variable 是**自启动以来的累计值**，直接当指标用会得出荒谬结论（"QPS = 自启动以来总查询数"）。重新建模：
   - `MetricDef.kind ∈ {gauge, counter}`
   - counter 入库前必须差分为 rate：`rate = (v_now − v_prev) / Δt`；若 `v_now < v_prev` 判定实例重启过，**丢弃该点并在 warnings 声明**，不许输出负 rate。
   - Gauge（如 `connections_used`）原样入库。
2. **指标三元组**：一维序列 = `(instance_id, metric, ts, value, dims)`。`dims` 留给分维度指标（如按 db/role、按表），默认空。
3. **时间分层**：
   - `raw`：原始点，保留 90 天；
   - `window`：5m/1h/1d 三档聚合（min/avg/max/p95/count），永久保留；
   - 一切趋势/基线算法**只消费 window 档**，保证量级可控（见 §4.3 容量估算）。
4. **基线是分布，不是数**。当前 `baselines` 表是"一个指标一个值"，无法回答"偏离基线多少"。重新建模为分位数：`baseline(instance_id, metric, window('1h'|'1d'), p50, p95, p99, samples, updated_at)`。Tier0 阶段该表为空，算法回落到静态基线 `baselines.py`——这正好落在 Tier 契约上。

### 1.4 知识域

| 实体 | 性质 | 变更频率 | 来源 |
|---|---|---|---|
| `MetricDef` | 指标字典：名字/单位/kind/告警方向/各引擎取数 SQL | 低 | 代码维护 |
| `Baseline(静态)` | 各引擎版本阈值，带 why/ref（已有 baselines.py） | 低 | 人工/社区 |
| `Baseline(动态)` | 实例自身历史的分位数（§1.3） | 每次观测后重算 | 数据 |
| `Signature` | 故障指纹：症状→检查项→成因→处置→升级标准（已有 18 种） | 低 | 人工沉淀 |
| `Rule` | 体检规则：AST 匹配 + why + 误报说明（已有 31 条） | 低 | 人工沉淀 |

知识域与数据域**严格分离**：知识可随版本发不出厂（Tier0 的底气），数据只属于客户实例（Tier2 的资产）。知识包导入导出（`export_profile/import_profile`）导的是**动态基线与匿名统计**，绝不混入代码级知识。

### 1.5 判断域

所有判断产出实体共享三个字段：`correlation_id`（哪次调用链产的）、`tier`（判断时用了什么级别的历史）、`confidence`。这是审计与复盘的最小要求。

- **Finding**（J1）：`{finding_id, rule_id, object(表/指纹), severity, message, suggestion, ts}`
- **Diagnosis**（J2）：见 §2.2 的假设-证据模型
- **AnomalyEvent**（J3）：`{metric, ts, value, expected_range, deviation, duration_windows}`
- **Forecast**（J3）：`{metric, horizon, predicted_p50, predicted_p95, upper/lower, method, samples_used}`
- **EquivalenceVerdict**（J4）：`{level, method, before, after, verdict, residual_risk}`
- **HealthScore**（J1）：见 §2.1 的维度树

### 1.6 行动域与闭环域

- **Remediation**：修法（SQL 变换 / 参数调整 / 处置步骤），必须可被 verify 闭环。
- **MetricContract**：口径契约（YAML），是 data-semantics 的"定案"产物。
- **FindingLifecycle（技术债账本，★ 当前完全缺失，复利的具体形态）**：

```
Finding 生命周期：open → confirmed → fixing → fixed → verified
                                  ↘ dismissed (误报，需理由)
```

  同一 `(instance_id, sql_fingerprint|object, rule_id)` 跨时间聚合为一条生命周期记录。价值：
  1. 客户月报："本月新发现 23 项、修复 12 项、仍开放 8 项"——**这是续费的凭证**；
  2. 同一问题反复出现 = 规则价值最高的证据，反哺规则优先级；
  3. `dismissed` 记录误报理由，是规则调优的数据来源（对应"一条乱报的规则比没规则更糟"）。

- **Incident（事故）**：`AnomalyEvent / Symptom / Diagnosis / change_event / remediation` 在时间轴上的对齐封装，是 DBA 值守的最终交付物。

---

## 2. 算法设计（按四种判断）

### 2.1 J1 好坏判断

#### 2.1.1 SQL 体检（规则引擎，已成型）

AST 规则匹配 + 元数据上下文增强。唯一要补的算法点：**风险随表行数分级**（同一条 `SELECT *`，10 行的配置表和 2 亿行的订单表严重度不同）。规则给出 `row_threshold` 元数据，引擎按 Tier1 真实行数升级/降级严重度。

#### 2.1.2 变更风险：从 if-else 矩阵升级为**因子模型**

现状是引擎矩阵查表。重新设计为显式多因子加权和，**每个因子贡献可解释**：

```
Risk(L0-L3) = clamp( Σ  w_i · factor_i , 0, 100 )
```

| 因子 f_i | 取值方式 | 默认权重 |
|---|---|---|
| 操作破坏性 | 不可逆 DDL > 锁表 DDL > 大 DML > 小 DML | 30 |
| 影响行数 | log 标度：`min(1, log10(rows)/8)` | 20 |
| 锁持有语义 | 排他锁时长估算 vs `lock_wait_timeout` | 15 |
| 回滚可行性 | 有回滚方案=0，理论上可逆=0.5，不可逆=1 | 15 |
| 执行时机 | 峰值窗口=1，业务低峰=0.2 | 10 |
| 对象热度 | 该表在慢日志/审计中的出现频率（Tier2） | 10 |

输出必须带 `factors[]`（每个因子的取值、贡献分、依据）——**DBA 要能对着报告吵架**，吵赢的因子可以按客户调整权重（权重本身做成可配置资产）。

#### 2.1.3 健康评分：维度树聚合

```
Health(0-100) = Σ_d  W_d · dim_score_d
dim_score_d = 100 × Π_s (1 − violation_s)        # 违规度连乘
violation_s = σ( (value_s − threshold_s) / spread_s )   # sigmoid，越界越深越接近 1
```

维度与子指标：

| 维度 d | 权重 | 子指标示例 |
|---|---|---|
| 可用性 | 25 | 连接使用率、拒绝数 |
| 性能 | 25 | 慢查询率、延迟偏离基线幅度（Tier2 用动态基线，Tier0 用静态阈值） |
| 锁与并发 | 15 | 锁等待数、长事务数 |
| 复制 | 15 | 延迟秒数、IO/SQL 线程状态 |
| 容量 | 10 | 磁盘余量、按当前增速的剩余天数（J3 联动） |
| 配置卫生 | 10 | 危险参数、无主键表比例、冗余索引数 |

Tier0/Tier1/Tier2 的差别只体现在 `threshold_s` 的来源（静态基线 → 实例元数据 → 动态分位数基线），**聚合结构完全不变**。

### 2.2 J2 因果判断：假设-证据-置信度模型

现状是"规则命中即结论"。重新设计为**显式的假设检验结构**——DBA 报告的行业惯例是"我怀疑 X，因为 A、B，已排除 C"，这必须建模出来：

```
Hypothesis {
  id            # 如 REPL001
  statement     # 自然语言：「主从复制中断导致读到旧数据」
  prior         # 行业先验：这类故障占该症状的比例（知识域维护）
  checks[]      # 检验函数列表，每条：
    { needs: [metric|param|meta],   # 需要什么证据
      judge: (facts) -> (support | refute | unknown, strength 0-1, fact) }
  falsified_by  # 何种证据出现即推翻本假设（可证伪性是硬要求）
}

Diagnosis 算法：
  1. 症状 → 候选假设集（文本检索 Signature，Tier0）
  2. 对每个 h：收集 checks 需要的证据（Tier1 快照 / Tier2 基线偏离）
  3. score_h = prior_h × Π support_strength − Σ refute_penalty
  4. 归一化排序，输出 top-k，每条附：支撑证据、反对证据、缺什么证据
  5. top1.score / Σscores < 0.5 ⇒ confidence=low，明确说「证据不足以区分」
```

三条硬约束：

1. **可证伪**：每条结论必须带 `falsified_by`。不给推翻条件的结论不是诊断，是玄学。
2. **证据不足 ≠ 结论**：需要的 metric 采不到（如没开 performance_schema），该 check 返回 `unknown` 并把缺口写进 `tier_upgrade` / `next_actions`（"开启 X 后可确认"）。
3. **Diagnosis 是事件**：每次诊断以 `diagnosis_event` 入库。三个月后假设库校准（prior 调整）有数据可依——这是知识域唯一允许被数据反哺的地方，且调整必须留审计。

#### 2.2.1 变更-异常时间关联（J2 的高价值特例）

`change_event` 之后 T 分钟内出现的 `AnomalyEvent`，自动建立 `suspect_link`（疑似关联）。规则简单，但这是 DBA 排障第一直觉（"刚才谁改了什么？"）的自动化，**值守场景价值密度最高的一条算法**。

### 2.3 J3 趋势判断

前提：window 档数据（§1.3-3）。**所有算法只要求可解释、免训练、免维护**——运维场景没有数据科学家驻场。

#### 2.3.1 动态基线

```
对每个 (instance_id, metric, window)：
  维护滚动分位数 p50 / p95 / p99 与样本数 n
  n < 30      ⇒ 基线状态 = insufficient（回落静态基线，不猜）
  正常带      = [max(p50 − k·spread, 0), p95]      # spread = p95 − p50
```

#### 2.3.2 异常检测

```
触发 = value > 上界(或 < 下界，按 MetricDef.direction)
持续 = 连续 N 个窗口越界（防抖，5m 窗口默认 N=3）
抑制 = 同一 (metric, deviation_bucket) 15 分钟内不重复报
输出 AnomalyEvent { value, expected_range, deviation, duration }
```

不用 z-score/孤立森林之类的模型——**告警必须能让值班的人手算复现**，这是排障信任的基础。

#### 2.3.3 容量预测

```
输入：metric 的 '1d' window 聚合序列（至少 14 天，不足 ⇒ E_NOT_TIER2）
方法：线性最小二乘拟合 + 残差标准差
输出：Forecast { 斜率, 剩余天数 = (上限 − 当前)/斜率,
                预测区间 = 拟合线 ± 2σ（给区间不给单点） }
克制：不做季节性分解/ARIMA。数据不足时给区间与置信声明，
     数据够了再演进——先用最笨的对的方法。
```

#### 2.3.4 口径漂移

同一口径 signature 在两个数据点的值比率超出容差带（已有 detect_drift 雏形），补齐：容差按指标类型分级（计数类 ±5%，金额类 ±0.5%），连续 2 点超差才告警。

### 2.4 J4 等价判断：四层等价模型

把散在 verify/plan/migration/semantics 的能力统一为一个判断本体：

| 层 | 回答 | 方法 | 置信度 |
|---|---|---|---|
| L1 结构等价 | SQL 改写后语义相同吗 | AST 归一化对比（投影/谓词/排序/分页/聚合） | high |
| L2 计划等价 | 改写后执行路径相同/更好吗 | dry-run + 计划 diff（行数变化%、全扫/文件排序消除） | high（连库时） |
| L3 数据等价 | 迁移后数据一致吗 | 校验和（全量）+ 抽样比对（分层抽样，抽样置信度声明） | 依抽样率 |
| L4 语义等价 | 两个口径定义相同吗 | 口径 signature 九类 diff（已有） | high |

统一输出 `EquivalenceVerdict {level, method, verdict, residual_risk}`。**残余风险必须显式**：L3 抽样没测的部分、L2 的统计信息失真，都要写出来——"验证过"和"验证过且知道没验证什么"是两种商品。

### 2.5 索引建议（J1 的子算法，补深度）

```
1. 候选生成：谓词列（等值 > 范围）、JOIN 列、ORDER BY 列 → 组合候选集
2. 剪枝：被现有索引前缀覆盖的候选丢弃
3. 收益估算：Tier1 时用真实行数 + 列基数估算选择性 → 预估扫描行数下降
4. 代价项：写放大 ≈ 表写频率（Tier2 有数据时）× 索引列数
5. 输出：按 (预估收益 − 代价) 排序，附"为什么不是另一个候选"
```

Tier0 时第 3/4 步数据不可得，输出候选与理由但明确标注"收益未验证，建议 EXPLAIN 确认"——同样落在 Tier 契约上。

---

## 3. 判断 → 工具的映射（对 DESIGN §3 的约束）

每个工具只允许做上述算法的**组合**，不允许自创判断逻辑：

| 工具（现状） | 内部算法 |
|---|---|
| review_sql / review_batch | §2.1.1 |
| assess_change / estimate_impact | §2.1.2（因子模型） |
| health_score | §2.1.3（维度树） |
| diagnose / explain_symptom / incident_report | §2.2（假设-证据） |
| anomaly_report / capacity_forecast | §2.3 |
| verify_fix / compare_plans | §2.4 L1/L2 |
| migration 一致性 | §2.4 L3 |
| semantics diff / drift | §2.4 L4 + §2.3.4 |
| advise_indexes | §2.5 |

---

## 4. 存储模型

### 4.1 SQLite Schema v3（增量演进，v2 数据可迁移）

```sql
-- 观测域（重构 observations：payload 拆为结构化列）
CREATE TABLE observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instance_id TEXT NOT NULL,
  kind TEXT NOT NULL,              -- metric_sample|snapshot|finding|change_event|diagnosis_event|symptom
  ts REAL NOT NULL,
  metric TEXT,                     -- kind=metric_sample 时必填
  value REAL,                      -- gauge 原值 / counter 已差分为 rate
  dims TEXT,                       -- JSON，分维度指标用
  payload TEXT NOT NULL,           -- JSON，按 kind 的字段契约（§1.3）
  correlation_id TEXT
);
CREATE INDEX idx_obs_series ON observations(instance_id, metric, ts) WHERE kind='metric_sample';
CREATE INDEX idx_obs_kind_ts ON observations(instance_id, kind, ts);

-- window 聚合档（永久保留）
CREATE TABLE metric_windows (
  instance_id TEXT NOT NULL, metric TEXT NOT NULL,
  window TEXT NOT NULL,            -- '5m'|'1h'|'1d'
  bucket_ts REAL NOT NULL,         -- 窗口起点
  cnt INTEGER, avg REAL, min REAL, max REAL, p95 REAL,
  PRIMARY KEY(instance_id, metric, window, bucket_ts)
);

-- 动态基线（分布，不是单值）
CREATE TABLE metric_baselines (
  instance_id TEXT NOT NULL, metric TEXT NOT NULL, window TEXT NOT NULL,
  p50 REAL, p95 REAL, p99 REAL, spread REAL, samples INTEGER, updated_at REAL,
  PRIMARY KEY(instance_id, metric, window)
);

-- SQL 档案
CREATE TABLE sql_profiles (
  fingerprint TEXT PRIMARY KEY,
  sample_sql TEXT, kind TEXT,
  first_seen REAL, last_seen REAL, occurrence_count INTEGER DEFAULT 1
);

-- 技术债账本
CREATE TABLE finding_lifecycle (
  finding_id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL, fingerprint TEXT, object TEXT, rule_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',   -- open|confirmed|fixing|fixed|verified|dismissed
  severity TEXT, occurrences INTEGER DEFAULT 1,
  first_seen REAL, last_seen REAL,
  fixed_by_change TEXT, dismiss_reason TEXT
);

-- 事故与关联
CREATE TABLE incidents (
  incident_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
  symptom TEXT, opened_at REAL, closed_at REAL,
  diagnosis TEXT,                  -- JSON：Diagnosis 全文
  remediation TEXT, timeline TEXT  -- JSON：事件时间线
);
CREATE TABLE suspect_links (
  anomaly_id TEXT NOT NULL, change_id TEXT NOT NULL,
  delta_seconds REAL, PRIMARY KEY(anomaly_id, change_id)
);
```

保留 v2 的 `instances / changes / dictionary / links / audit_log` 不动。

### 4.2 迁移与兼容

- `SCHEMA_VERSION = 3`，`_migrate()` 按版本号逐级升级；
- v2 `observations` 旧 payload 整体归入新表 payload 字段（kind 保留原文），不做有损转换；
- 所有旧函数签名（record_observation / get_history / …）保持不变，内部写新结构。

### 4.3 容量可行性（为什么 SQLite 够用）

单实例、5 分钟一轮、100 个指标 ⇒ raw 2.88 万行/天；90 天 ≈ 260 万行；window '1h' 档 100 行/天 × 永久 = 3.65 万行/年。10 个实例三年总量 < 1 亿行中的很小一部分，SQLite（WAL + 覆盖索引 + raw 定期降档删除）在单机工具场景完全够用。**不上时序数据库**——那是部署负担，不是本产品的卖点。

---

## 5. 现状差距分析（按优先级）

| # | 模块 | 现状 | 目标 | 差距定性 | 优先级 |
|---|---|---|---|---|---|
| G1 | metrics.py | 34 个逻辑指标，**无 counter/gauge 区分** | MetricDef.kind + 差分管道 | **正确性 bug 级**（counter 直读当 rate 用） | **P0** |
| G2 | profile_store | observations.payload 无 schema 自由 JSON | §1.3 六种 kind 强契约 + 结构化列 | 一切 J2/J3 算法的地基 | **P0** |
| G3 | baselines 表 | 单值基线 | 分位数基线 metric_baselines | J2/J3 的"偏离度"无从谈起 | **P0** |
| G4 | （缺失） | 无技术债账本 | finding_lifecycle 生命周期 | 复利的具体形态、续费凭证 | **P1** |
| G5 | rca.py | 规则命中即结论 | §2.2 假设-证据-置信度 + 可证伪 | J2 的品质天花板 | **P1** |
| G6 | （缺失） | 无 SQL 档案 | sql_profiles | "这条 SQL 抓了 40 次"无处安放 | **P1** |
| G7 | risk.py | 引擎矩阵查表 | §2.1.2 因子模型（可解释权重） | 可解释性与可配置性 | P2 |
| G8 | dbwatch.health_score | 简单加权 | §2.1.3 维度树 + sigmoid 违规度 | 评分可信度 | P2 |
| G9 | （缺失） | 无变更-异常关联 | suspect_links 时间关联 | 值守价值密度最高的一条 | P2 |
| G10 | advisor.py | 候选罗列 | §2.5 收益-代价估算 | 建议的专业度 | P2 |
| G11 | （缺失） | 无 incident 实体 | incidents + 时间线 | 值守交付物完整性 | P2 |
| G12 | semantics.detect_drift | 单点比较 | 容差分级 + 连续 2 点确认 | 误报治理 | P3 |

### 演进路线

- **M1（正确性地基，P0）**：G1 + G2 + G3。做完这三条，J3 类算法才有正确输入；动 profile_store 需要同步改所有写入方。
- **M2（判断升级，P1）**：G4 + G5 + G6。RCA 重写为假设-证据模型，技术债账本与 SQL 档案让"每次交付都在积累"从口号变成表里查得到的数据。
- **M3（专业度，P2/P3）**：G7-G12。

---

## 6. 负空间：明确不做的算法

| 不做 | 理由 |
|---|---|
| ML 黑盒（异常检测模型、风险预测模型） | ① 排障结论必须可人工复现，黑盒毁掉信任；② 单客户数据量撑不起训练；③ 维护成本转嫁给部署方 |
| 通用时序数据库 | §4.3 量级计算证明 SQLite 够用；引入 TSDB = 给每个客户加部署负担 |
| 自动调参 / 自动执行修复 | 修复必须过变更状态机 + 人工 approver，这是产品的安全边界不是限制 |
| 跨客户联邦学习式基线 | 隐私红线；知识包走匿名统计导出（已有 export_profile 脱敏）就够 |
| 季节性预测 / ARIMA / LSTM | 数据不足时给区间比给一个精确的错误数字诚实；等 M3 落地后按真实需求再议 |
