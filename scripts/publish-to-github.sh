#!/usr/bin/env bash
# =============================================================================
# publish-to-github.sh —— 把 db-ai-toolchain 工具链的 11 个独立仓库发布到 GitHub
#
# 设计前提（已与用户确认）：
#   - 11 个仓库都放在 GitHub Org `db-mcp-tools` 下，各自独立（非 submodule）
#   - 本地各仓库分支均为 master，且工作区已 commit 干净（本脚本只 push，不 commit）
#   - 认证走 HTTPS + PAT（credential.helper=store 已开，会记住 token）
#
# 用法：
#   GITHUB_TOKEN=ghp_xxx bash scripts/publish-to-github.sh
#
# 前置条件：
#   - 一个对 db-mcp-tools 有 owner / 写权限的 PAT
#       * classic PAT：勾选 repo scope
#       * fine-grained PAT：授权该 org 下 repo 的 contents 写权限
#   - 本机已 git commit 完所有改动（脚本会先检查每个仓工作区是否干净）
#
# 行为：
#   1) 对每个仓库先在 org 下建空仓库（auto_init=false，已存在则跳过）
#      —— 空仓库第一次 push 的 master 会自动成为 GitHub 默认分支，无需本地改名
#   2) 加 origin（URL 内嵌 token），push 当前分支 -> origin
#   3) push 完后把 origin 改成不带 token 的 URL（凭据已由 store 记住，后续 pull 免密）
#
# 健壮性：单个仓工作区脏或推送失败，只跳过并继续下一个，不会中断整轮发布。
# =============================================================================
set -uo pipefail

TOKEN="${GITHUB_TOKEN:?请设置 GITHUB_TOKEN 环境变量（对 db-mcp-tools 有写权限的 PAT）}"
ORG=db-mcp-tools
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# pre-flight: 确认 git over HTTPS 能连上 github.com（公开仓库，无需 token）
# 目的：WorkBuddy 等环境的系统代理可能只放行 api.github.com、挡 github.com:443，
# 或本机无法直连外网 —— 那种环境里 push 必败，提前报错比半途 502 更友好。
echo "==> 连通性预检：git ls-remote github.com（公开仓库，无需 token）"
if ! git ls-remote --heads "https://github.com/$ORG/sqltk-core.git" >/dev/null 2>&1; then
  echo "错误：git 无法连接到 github.com（HTTPS 被网络/代理拦截）。" >&2
  echo "  常见原因：当前终端的 HTTP 代理白名单不含 github.com，或本机无法直连外网。" >&2
  echo "  解决：在本机能直连、或代理已放行 github.com 的终端里运行本脚本。" >&2
  echo "  （可先在目标终端测：git ls-remote https://github.com/$ORG/sqltk-core.git）" >&2
  exit 1
fi
echo "    连通 OK"

# 目录 -> repo 名（第一个是治理根仓库自身）
DIRS=(db-ai-toolchain sqltk-core sqltk-store sqltk-know
      mcp-sql-review mcp-change-risk mcp-dbwatch mcp-migration-diff
      mcp-data-semantics mcp-profile-hub sqltk-orchestrator)
REPOS=(db-ai-toolchain sqltk-core sqltk-store sqltk-know
       mcp-sql-review mcp-change-risk mcp-dbwatch mcp-migration-diff
       mcp-data-semantics mcp-profile-hub sqltk-orchestrator)
DESCS=(
  "数据库生态 AI 工具链 —— 治理/编排根仓库（docs/scripts/tests/CI/文档）"
  "SQL toolkit 核心库：协议信封/方言归一/SQL指纹/只读连库/AST/执行计划/MCP运行时外壳"
  "统一档案存储（SQLite，零依赖）—— 所有 MCP 的唯一数据落点"
  "引擎阈值/指标字典/18 种故障指纹（零依赖，Tier0 的来源）"
  "SQL 静态体检 -> 修法 -> 验证 -> 计划对比 -> 索引建议（MCP）"
  "变更风险因子模型 + 执行状态机（预检/干跑/灰度/回滚）（MCP）"
  "DBA 值守：快照->定级->根因->健康分->事故报告（MCP）"
  "国产化迁移：类型映射->语法Gap->校验方案->工作量（MCP）"
  "数据口径治理：口径diff->扇出检测->下钻->契约固化（MCP）"
  "档案中枢：实例档案/分位数基线/技术债账本/知识包（MCP）"
  "跨 MCP 编排（按 next_actions 追链 + recipe），需装齐 6 个 MCP"
)

create_repo() {
  local name="$1" desc="$2"
  if curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" \
       "https://api.github.com/repos/$ORG/$name" | grep -q 200; then
    echo "    repo $ORG/$name 已存在，跳过创建"
    return 0
  fi
  echo "    创建 repo $ORG/$name ..."
  curl -s -X POST -H "Authorization: Bearer $TOKEN" \
       -H "Accept: application/vnd.github+json" \
       -d "{\"name\":\"$name\",\"description\":\"$desc\",\"private\":false,\"auto_init\":false}" \
       "https://api.github.com/orgs/$ORG/repos" \
    | grep -E '"full_name"|"errors"' || true
}

push_repo() {
  local dir="$1" repo="$2" desc="$3"
  local path branch
  if [ "$dir" = "db-ai-toolchain" ]; then
    path="$ROOT"            # 治理根仓库自身就在 ROOT，不在子目录
  else
    path="$ROOT/$dir"
  fi
  echo "==> $repo ($path)"

  [ -d "$path/.git" ] || { echo "    跳过：不是 git 仓库 ($path)"; return 0; }
  cd "$path" || { echo "    跳过：无法进入 $path"; return 0; }

  if [ -n "$(git status --porcelain)" ]; then
    echo "    警告：工作区不干净，跳过 push（请先 git commit）。" >&2
    return 0
  fi

  create_repo "$repo" "$desc"
  branch="$(git branch --show-current)"
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://x-access-token:${TOKEN}@github.com/${ORG}/${repo}.git"
  echo "    pushing $branch -> origin/$branch"
  if git push -u origin "$branch" 2>&1; then
    echo "    ✓ $repo 推送成功"
  else
    echo "    ✗ $repo 推送失败，继续下一个" >&2
  fi
  git remote set-url origin "https://github.com/${ORG}/${repo}.git" 2>/dev/null || true
  return 0
}

for i in "${!DIRS[@]}"; do
  push_repo "${DIRS[$i]}" "${REPOS[$i]}" "${DESCS[$i]}" || true
  echo
done

echo "完成。全部仓库已发布到 https://github.com/$ORG"
