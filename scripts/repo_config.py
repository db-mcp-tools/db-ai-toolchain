#!/usr/bin/env python3
"""工具链各仓元数据的唯一来源。

10 个仓是 10 个独立 git 仓库，但共享同一套发布元数据（作者、许可、URL、
classifiers、版本号）。手抄 10 份必然会漂移 —— 事实上已经漂过一次：
10 个 pyproject 全是 `license = {text = "Proprietary"}` 且没有 LICENSE 文件、
没有 authors/classifiers/urls，py.typed 声明了但文件不存在。

所以：**改这里，然后跑 `python scripts/gen-pyprojects.py`**。
CI 用 `--check` 挡住漂移。

被 gen-pyprojects.py 与 publish.py 共同 import。
只用标准库 + 兼容 Python 3.9（工具链脚本不该反过来要求高版本）。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 共享元数据

VERSION = "0.2.0"

# MIT：最短、最容易被采用。数据库工具链想被集成进别人的流程，
# 许可条款的摩擦力越小越好。要换 Apache-2.0（带专利授权）改这两行 +
# 重跑 gen-pyprojects.py 即可，LICENSE 正文会一起换。
LICENSE = "MIT"
COPYRIGHT = "2026 罗文星"

AUTHORS = [{"name": "罗文星", "email": "1172566410@qq.com"}]

# 组织名。子仓是独立仓库，所以 URL 由 owner + 仓名拼出来。
# 尚未确定时保留占位符：gen 会给出警告，publish --repo pypi 会拒绝上传
# （带着死链上 PyPI 比晚发一天更糟）。
GH_OWNER = "YOUR_GH_OWNER"
META_REPO = "db-ai-toolchain"

# 纯库支持 3.9；依赖 mcp SDK 的仓必须 3.10（SDK 自身要求，已实测：
# 3.9 上 `pip install mcp` 直接 "No matching distribution found"）。
PY_LIB = "3.9"
PY_MCP = "3.10"

SHARED_KEYWORDS = ["database", "sql", "dba", "database-tooling", "llm", "mcp"]

# ---------------------------------------------------------------- 命名
#
# 发行名（PyPI 上的名字）与 import 名是两件事：
#   sqltk-core  → import dbcore       sqltk-store → import dbstore
#   sqltk-know  → import dbknow       mcp-*       → import <对应下划线名>
#
# 为什么不用 dbt-* 前缀（2026-10 实测）：
#   1. `dbt-core` 已被 dbt Labs 占用（PyPI 上 1.12.6）。pip 解析我们
#      `dbt-core>=0.2.0` 时会拿到它的包，import dbcore 直接失败 ——
#      意味着 6 个 MCP 全都装不上。这不是"改名好看"，是硬阻断。
#   2. 它还占用 `dbt` 这个控制台命令，与编排器的 CLI 同名，两者装一起会互相覆盖。
#   3. `dbt-*` 是 dbt Labs 的包族（dbt-postgres / dbt-snowflake …），
#      我们的 `dbt-store` 摆在旁边容易被误认为它的适配器。
#
# sqltk = SQL toolkit。换前缀只需改这里的 name 与 deps 后重跑 gen；
# publish.py 上传前会逐个问 PyPI 确认名字没被占用。

# ---------------------------------------------------------------- 各仓定义
#
# kind:  lib = 纯库（3.9+，无 mcp 依赖）
#        mcp = 提供 MCP 服务端（3.10+）
#        app = 可选编排器（3.10+，需要装齐 6 个 MCP）
#
# deps 里的版本约束统一用 >=VERSION，与 publish.py 的“按序发布”配合：
# 上游先发，下游的约束才有解。

REPOS = [
    {
        "dir": "sqltk-core",
        "name": "sqltk-core",
        "pkg": "dbcore",
        "kind": "lib",
        "description": (
            "SQL 工具链运行时内核：dbt.result/v1 协议信封、方言归一、SQL 指纹、"
            "只读连库、AST 结构化、执行计划归一、MCP 运行时外壳。不含任何 MCP 业务判断。"
        ),
        "deps": ["sqlglot>=25.0"],
        "extras": {
            "mcp": ["mcp>=1.0"],
            "mysql": ["pymysql>=1.1"],
            "postgres": ["psycopg[binary]>=3.1"],
            "oracle": ["oracledb>=2.0"],
            "all": ["mcp>=1.0", "pymysql>=1.1", "psycopg[binary]>=3.1", "oracledb>=2.0"],
        },
        "keywords": ["sqlglot", "sql-parser", "sql-dialect", "sql-fingerprint"],
        "quality_assurance": False,
    },
    {
        "dir": "sqltk-store",
        "name": "sqltk-store",
        "pkg": "dbstore",
        "kind": "lib",
        "description": (
            "统一档案存储（SQLite，零第三方依赖）：实例档案、指标观测、分位数基线、"
            "发现生命周期、事故、变更事件。所有 MCP 的唯一数据落点。"
        ),
        "deps": [],
        "extras": {},
        "keywords": ["sqlite", "profiling-store", "dba-workflow"],
        "quality_assurance": False,
    },
    {
        "dir": "sqltk-know",
        "name": "sqltk-know",
        "pkg": "dbknow",
        "kind": "lib",
        "description": (
            "数据库引擎知识层（零依赖）：各引擎阈值与版本能力位、逻辑指标字典、"
            "故障指纹库。Tier0 冷启动能力的来源。"
        ),
        "deps": [],
        "extras": {},
        "keywords": ["mysql", "postgresql", "oracle", "knowledge-base", "thresholds"],
        "quality_assurance": False,
    },
    {
        "dir": "mcp-sql-review",
        "name": "mcp-sql-review",
        "pkg": "sql_review",
        "kind": "mcp",
        "description": (
            "SQL 体检 MCP：规则分册静态体检、自动修法、修复验证、执行计划对比、索引建议。"
            "Tier0 零依赖可用。"
        ),
        "deps": [f"sqltk-core[mcp]>={VERSION}", f"sqltk-store>={VERSION}"],
        "extras": {
            "mysql": ["pymysql>=1.1"],
            "postgres": ["psycopg[binary]>=3.1"],
        },
        "script": "mcp-sql-review = sql_review.cli:main",
        "keywords": ["sql-lint", "sql-review", "sql-rewrite", "code-review"],
        "quality_assurance": True,
    },
    {
        "dir": "mcp-change-risk",
        "name": "mcp-change-risk",
        "pkg": "change_risk",
        "kind": "mcp",
        "description": (
            "变更风险 MCP：因子模型风险评估、回滚方案生成、窗口预检、dry-run、"
            "执行状态机（precheck→dryrun→shadow→canary→applied）。"
        ),
        "deps": [f"sqltk-core[mcp]>={VERSION}", f"sqltk-store>={VERSION}", f"sqltk-know>={VERSION}"],
        "extras": {
            "mysql": ["pymysql>=1.1"],
            "postgres": ["psycopg[binary]>=3.1"],
        },
        "script": "mcp-change-risk = change_risk.cli:main",
        "keywords": ["database-migration", "change-management", "ddl-review", "rollback"],
        "quality_assurance": False,
    },
    {
        "dir": "mcp-dbwatch",
        "name": "mcp-dbwatch",
        "pkg": "dbwatch",
        "kind": "mcp",
        "description": (
            "DBA 值守 MCP：只读快照采集、负载定级、根因诊断（假设-证据-置信度）、"
            "健康分维度树、慢日志/锁分析、事故报告。"
        ),
        "deps": [f"sqltk-core[mcp]>={VERSION}", f"sqltk-store>={VERSION}", f"sqltk-know>={VERSION}"],
        "extras": {
            "mysql": ["pymysql>=1.1"],
            "postgres": ["psycopg[binary]>=3.1"],
        },
        "script": "mcp-dbwatch = dbwatch.cli:main",
        "keywords": ["monitoring", "observability", "incident", "rca", "health-score"],
        "quality_assurance": False,
    },
    {
        "dir": "mcp-migration-diff",
        "name": "mcp-migration-diff",
        "pkg": "migration_diff",
        "kind": "mcp",
        "description": (
            "国产化迁移 MCP：类型映射、语法 Gap 检测、SQL 翻译、"
            "一致性校验方案（L1 校验和 + L2 抽样）、对象对齐、工作量估算。"
        ),
        "deps": [f"sqltk-core[mcp]>={VERSION}", f"sqltk-store>={VERSION}"],
        "extras": {},
        "script": "mcp-migration-diff = migration_diff.cli:main",
        "keywords": ["database-migration", "sql-translation", "oracle", "domestic-database"],
        "quality_assurance": False,
    },
    {
        "dir": "mcp-data-semantics",
        "name": "mcp-data-semantics",
        "pkg": "data_semantics",
        "kind": "mcp",
        "description": (
            "数据口径治理 MCP：口径结构化、口径 diff、扇出检测、逐层下钻、"
            "漂移检测、口径契约 YAML 固化。"
        ),
        "deps": [f"sqltk-core[mcp]>={VERSION}", f"sqltk-store>={VERSION}"],
        "extras": {},
        "script": "mcp-data-semantics = data_semantics.cli:main",
        "keywords": ["metrics", "data-governance", "semantic-layer", "drift-detection"],
        "quality_assurance": False,
    },
    {
        "dir": "mcp-profile-hub",
        "name": "mcp-profile-hub",
        "pkg": "profile_hub",
        "kind": "mcp",
        "description": (
            "档案中枢 MCP：实例档案、观测查询、分位数基线、发现生命周期（技术债账本）、"
            "事故、跨实例关联、知识包导入导出。"
        ),
        "deps": [f"sqltk-core[mcp]>={VERSION}", f"sqltk-store>={VERSION}", f"sqltk-know>={VERSION}"],
        "extras": {},
        "script": "mcp-profile-hub = profile_hub.cli:main",
        "keywords": ["profiling", "metadata", "tech-debt", "knowledge-pack"],
        "quality_assurance": False,
    },
    {
        "dir": "sqltk-orchestrator",
        "name": "sqltk-orchestrator",
        "pkg": "dbt_orchestrator",
        "kind": "app",
        "description": (
            "[可选] 跨 MCP 编排引擎：按 next_actions 自动追链 + recipe 确定性流水线。"
            "需要先装齐 6 个 MCP。"
        ),
        "deps": [
            f"sqltk-core>={VERSION}",
            f"sqltk-store>={VERSION}",
            f"mcp-sql-review>={VERSION}",
            f"mcp-change-risk>={VERSION}",
            f"mcp-dbwatch>={VERSION}",
            f"mcp-migration-diff>={VERSION}",
            f"mcp-data-semantics>={VERSION}",
            f"mcp-profile-hub>={VERSION}",
        ],
        "extras": {},
        "script": "sqltk = dbt_orchestrator.cli:main",
        "keywords": ["orchestration", "workflow", "pipeline", "dba-workflow"],
        "quality_assurance": False,
    },
]

BY_DIR = {r["dir"]: r for r in REPOS}
BY_NAME = {r["name"]: r for r in REPOS}


# ---------------------------------------------------------------- 派生逻辑


def requires_python(repo: dict) -> str:
    return ">=3.9" if repo["kind"] == "lib" else ">=3.10"


def py_versions(repo: dict) -> list[str]:
    return ["3.9", "3.10", "3.11", "3.12", "3.13"] if repo["kind"] == "lib" \
        else ["3.10", "3.11", "3.12", "3.13"]


def repo_urls(repo: dict) -> dict:
    base = f"https://github.com/{GH_OWNER}"
    return {
        "Homepage": f"{base}/{repo['name']}",
        "Repository": f"{base}/{repo['name']}",
        "Issues": f"{base}/{repo['name']}/issues",
        "Changelog": f"{base}/{META_REPO}/blob/main/CHANGELOG.md",
    }


def dep_names(repo: dict) -> list[str]:
    """把 `sqltk-core[mcp]>=0.2.0` 解析成 `sqltk-core`。"""
    out = []
    for spec in repo["deps"]:
        name = spec.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip()
        out.append(name)
    return out


def install_order() -> list[dict]:
    """拓扑序：被依赖的仓先发。有环就报错，不静默。"""
    ordered: list[dict] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(repo: dict) -> None:
        if repo["name"] in seen:
            return
        if repo["name"] in visiting:
            raise SystemExit(f"依赖成环：{repo['name']}")
        visiting.add(repo["name"])
        for dep in dep_names(repo):
            if dep in BY_NAME:
                visit(BY_NAME[dep])
        visiting.discard(repo["name"])
        seen.add(repo["name"])
        ordered.append(repo)

    for r in REPOS:
        visit(r)
    return ordered


def placeholder_urls() -> bool:
    return GH_OWNER.startswith("YOUR_") or GH_OWNER in ("", "None")
