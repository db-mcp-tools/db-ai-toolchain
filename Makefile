SHELL := bash
PY ?= python
PYPATH := packages;.

.PHONY: help test test-fast lint install tools review clean

help:
	@echo "make install     安装依赖"
	@echo "make test        全量回归测试（不需要真实数据库）"
	@echo "make tools       列出全部 MCP 与工具"
	@echo "make review      演示：体检一条 SQL"
	@echo "make tier0       演示：零历史也能出报告"
	@echo "make clean       清理缓存"

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) tests/test_all.py

tools:
	$(PY) cli/dbt.py tools

review:
	$(PY) cli/dbt.py review "SELECT * FROM orders WHERE DATE(created_at)='2026-01-01' ORDER BY id LIMIT 10 OFFSET 50000"

tier0:
	$(PY) cli/dbt.py watch 主从延迟突增
	@echo
	$(PY) cli/dbt.py watch --report 锁等待超时导致大量接口超时

lint:
	$(PY) -m ruff check packages servers cli tests || true

clean:
	find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache 2>/dev/null || true
