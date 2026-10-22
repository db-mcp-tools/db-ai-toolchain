# db-ai-toolchain —— 顶层只做聚合，业务在各子仓
PY ?= python
# 留空 = 用当前环境自己的 pip 源（pip.conf / 全局配置）。
# 需要临时换源：make build INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
INDEX ?=
INDEX_ARG = $(if $(INDEX),--index-url $(INDEX))

.PHONY: help test boundaries check gen check-meta build publish-test publish \
        install install-all tree

help:
	@echo "开发"
	@echo "  make test          跑全部 12 个测试套件"
	@echo "  make boundaries    检查跨 MCP 依赖（必须 0 违规）"
	@echo "  make check         边界检查 + 全量测试"
	@echo "  make install       安装 mcp-sql-review 及其共享库"
	@echo "  make install-all   安装全部 6 个 MCP + 编排器"
	@echo "  make tree          打印仓库结构与规模"
	@echo "发布"
	@echo "  make gen           从 scripts/repo_config.py 生成 pyproject / LICENSE / py.typed / CI"
	@echo "  make check-meta    只校验元数据是否漂移（CI 用，不改文件）"
	@echo "  make build         按依赖拓扑序构建 10 个 wheel 并过 twine check"
	@echo "                     （换源：make build INDEX=https://pypi.tuna.tsinghua.edu.cn/simple）"
	@echo "  make publish-test  上传 TestPyPI（预演，见 docs/PUBLISHING.md）"
	@echo "  make publish       上传 PyPI"

test:
	$(PY) scripts/test-all.py

boundaries:
	$(PY) scripts/check-boundaries.py

check: boundaries test

gen:
	$(PY) scripts/gen-pyprojects.py

check-meta:
	$(PY) scripts/gen-pyprojects.py --check

build:
	$(PY) scripts/publish.py --check $(INDEX_ARG)

publish-test:
	$(PY) scripts/publish.py --upload testpypi $(INDEX_ARG)

publish:
	$(PY) scripts/publish.py --upload pypi $(INDEX_ARG)

install:
	$(PY) scripts/dev-install.py --with-deps

install-all:
	$(PY) scripts/dev-install.py --all --with-deps

tree:
	@for d in sqltk-core sqltk-store sqltk-know mcp-sql-review mcp-change-risk \
	          mcp-dbwatch mcp-migration-diff mcp-data-semantics mcp-profile-hub sqltk-orchestrator; do \
	  n=$$(cat $$d/src/*/*.py 2>/dev/null | wc -l); \
	  t=$$(cat $$d/tests/*.py 2>/dev/null | wc -l); \
	  printf "%-22s src %5s  tests %4s\n" "$$d" "$$n" "$$t"; \
	done
