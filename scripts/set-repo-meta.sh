#!/usr/bin/env bash
# =============================================================================
# set-repo-meta.sh —— 批量设置 db-ai-toolchain 工具链 11 个仓库的 GitHub 元数据
#
# 设置内容：每个仓库的 description（简介）与 topics（话题标签）。
#
# 数据来源：
#   - 10 个代码仓的 description 与 topics 基础词取自 scripts/repo_config.py
#     （PyPI 的 description / keywords，单一真源，避免双份维护漂移）
#   - 每个仓库的 topics = 该仓 keywords + 公共标签(sql/database/mcp/ai/llm)，去重保序
#   - 治理根仓库 db-ai-toolchain 不在 repo_config 里，单独内联
#
# 用法：
#   GITHUB_TOKEN=ghp_xxx bash scripts/set-repo-meta.sh
#
# 前置：一个对 db-mcp-tools 有写权限的 PAT（classic 勾 repo / fine-grained 授权
#       该 org 下 repo 的 administration + metadata 写权限）。
#
# 幂等：重复运行安全（description 与 topics 都会被覆盖成脚本里的值）。
# =============================================================================
set -euo pipefail

TOKEN="${GITHUB_TOKEN:?请设置 GITHUB_TOKEN 环境变量（对 db-mcp-tools 有写权限的 PAT）}"
ORG=db-mcp-tools
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 治理根仓库（repo_config.py 不含它，单独内联）
META_DESC="数据库生态 AI 工具链 —— 治理/编排根仓库（docs/scripts/tests/CI/文档）"
META_TOPICS="database sql ai mcp toolchain meta-repo"

# 代码仓：从 repo_config 读取 name / description / keywords
mapfile -t ROWS < <(python3 - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/scripts")
import repo_config as c
COMMON = ["sql", "database", "mcp", "ai", "llm"]
for r in c.REPOS:
    kws = [k.lower() for k in r.get("keywords", [])]
    topics = list(dict.fromkeys(kws + COMMON))  # 去重且保序
    print(f"{r['name']}\t{r['description']}\t{','.join(topics)}")
PY
)

# 把 (desc, topics_csv) 组装成 GitHub PATCH 的 JSON body
build_body() {
  python3 - "$1" "$2" <<'PY'
import sys, json
desc = sys.argv[1]
topics = [t for t in sys.argv[2].split(',') if t]
print(json.dumps({"description": desc, "topics": topics}, ensure_ascii=False))
PY
}

apply() {
  local name="$1" desc="$2" topics_csv="$3"
  local body http
  body="$(build_body "$desc" "$topics_csv")"
  http="$(curl -s -o /tmp/set_repo_meta_resp.json -w "%{http_code}" -X PATCH \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    -d "$body" \
    "https://api.github.com/repos/$ORG/$name")"
  if [ "$http" = "200" ]; then
    echo "  ✓ $name 已更新（topics: $(echo "$topics_csv" | tr ',' ' ')）"
  else
    echo "  ✗ $name 失败 http=$http"
    sed -n 's/.*"message"[ ]*:[ ]*"\([^"]*\)".*/\1/p' /tmp/set_repo_meta_resp.json | head -1
  fi
}

echo "==> 治理根仓库: db-ai-toolchain"
apply "db-ai-toolchain" "$META_DESC" "$META_TOPICS"

for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r name desc topics_csv <<< "$row"
  echo "==> $name"
  apply "$name" "$desc" "$topics_csv"
done

echo
echo "完成。可在 https://github.com/orgs/$ORG/repositories 核对 topics 与简介。"
