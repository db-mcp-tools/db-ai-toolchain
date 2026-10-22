# db-ai-toolchain —— 顶层只做聚合，业务在各子仓
PY ?= python

.PHONY: help test boundaries install install-all check tree

help:
	@echo "make test         跑全部 11 个测试套件"
	@echo "make boundaries   检查跨 MCP 依赖（必须 0 违规）"
	@echo "make check        边界检查 + 全量测试"
	@echo "make install      安装 mcp-sql-review 及其共享库"
	@echo "make install-all  安装全部 6 个 MCP + 编排器"
	@echo "make tree         打印仓库结构与规模"

test:
	$(PY) scripts/test-all.py

boundaries:
	$(PY) scripts/check-boundaries.py

check: boundaries test

install:
	$(PY) scripts/dev-install.py --with-deps

install-all:
	$(PY) scripts/dev-install.py --all --with-deps

tree:
	@for d in dbt-core dbt-store dbt-know dbt-mcp-kit mcp-sql-review mcp-change-risk \
	          mcp-dbwatch mcp-migration-diff mcp-data-semantics mcp-profile-hub dbt-orchestrator; do \
	  n=$$(cat $$d/src/*/*.py 2>/dev/null | wc -l); \
	  t=$$(cat $$d/tests/*.py 2>/dev/null | wc -l); \
	  printf "%-22s src %5s  tests %4s\n" "$$d" "$$n" "$$t"; \
	done
