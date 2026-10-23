# db-ai-toolchain —— 顶层只做聚合，业务在各子仓
#
# 这里只是 scripts/*.py 的快捷方式。Windows 上没有 make 就直接跑脚本，
# 每个 target 对应的命令都写在下面，一一对应。
PY ?= python
# 留空 = 用当前环境自己的 pip 源（pip.conf / 全局配置）。
# 需要临时换源：make build INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
INDEX ?=
INDEX_ARG = $(if $(INDEX),--index-url $(INDEX))

.PHONY: help test smoke boundaries check bench gen check-meta build verify publish-test publish \
        install install-all tree

help:
	@echo "开发"
	@echo "  make test          跑全部 12 个测试套件（逐套件报用例数）"
	@echo "  make smoke         用真实 MCP 客户端挂载 6 个 server（需 mcp SDK）"
	@echo "  make boundaries    检查跨 MCP 依赖（必须 0 违规）"
	@echo "  make check         边界检查 + 全量测试 + 挂载冒烟 + 精度基准"
	@echo "  make bench         跑规则引擎精度基准（anti-pattern 级 P/R/F1，回归门禁）"
	@echo "  make install       安装 mcp-sql-review 及其共享库"
	@echo "  make install-all   安装全部 6 个 MCP + 编排器"
	@echo "  make tree          打印仓库结构与规模"
	@echo "发布"
	@echo "  make gen           从 scripts/repo_config.py 生成 pyproject / LICENSE / py.typed / CI"
	@echo "  make check-meta    只校验元数据是否漂移（CI 用，不改文件）"
	@echo "  make build         按依赖拓扑序构建 10 个 wheel 并过 twine check"
	@echo "                     （换源：make build INDEX=https://pypi.tuna.tsinghua.edu.cn/simple）"
	@echo "  make verify        把 wheel 装进干净 venv 验证（按需安装 + 整图 + 挂载冒烟）"
	@echo "  make publish-test  上传 TestPyPI（预演，见 docs/PUBLISHING.md）"
	@echo "  make publish       上传 PyPI"

test:
	$(PY) scripts/test-all.py

smoke:
	$(PY) scripts/smoke-mcp.py

boundaries:
	$(PY) scripts/check-boundaries.py

check: boundaries test smoke bench

bench:
	$(PY) mcp-sql-review/bench/run_bench.py --fail-under 0.90 --max-fp 0

gen:
	$(PY) scripts/gen-pyprojects.py

check-meta:
	$(PY) scripts/gen-pyprojects.py --check

build:
	$(PY) scripts/publish.py --check $(INDEX_ARG)

verify:
	$(PY) scripts/verify-wheels.py $(INDEX_ARG)

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
