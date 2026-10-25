#!/usr/bin/env bash
# =============================================================================
# clone-all.sh —— 一键 clone db-ai-toolchain 工具链的全部 11 个独立 git 仓库
#
# 说明：
#   本工具链是 11 个独立仓库（非 monorepo、非 submodule）。仓库都在 GitHub Org
#   `db-mcp-tools` 下。本脚本把它们一次性 clone 到本地某个父目录里。
#
# 用法：
#   bash scripts/clone-all.sh              # 克隆到当前目录
#   bash scripts/clone-all.sh /path/dest   # 克隆到指定父目录
#   DEST=/path/dest bash scripts/clone-all.sh
#
# 行为：
#   - 已存在的目录自动跳过（可反复跑，不会覆盖本地改动）
#   - 全部为公开仓库，clone 不需要 token
#   - 克隆完成后打印一句如何进入根仓库的提示
# =============================================================================
set -euo pipefail

ORG=db-mcp-tools
DEST="${1:-${DEST:-.}}"

REPOS=(
  db-ai-toolchain          # 治理 / 编排根仓库（docs/scripts/tests/CI/文档）
  sqltk-core
  sqltk-store
  sqltk-know
  mcp-sql-review
  mcp-change-risk
  mcp-dbwatch
  mcp-migration-diff
  mcp-data-semantics
  mcp-profile-hub
  sqltk-orchestrator
)

mkdir -p "$DEST"

for r in "${REPOS[@]}"; do
  if [ -d "$DEST/$r" ]; then
    echo "跳过（已存在）: $r"
  else
    echo "clone: $r"
    git clone "https://github.com/$ORG/$r.git" "$DEST/$r"
  fi
done

echo
echo "完成。11 个仓库位于: $DEST"
echo "进入根仓库: cd $DEST/db-ai-toolchain"
