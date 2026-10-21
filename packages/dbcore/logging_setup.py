"""统一日志 —— 所有 MCP 共用同一套格式和一份日志文件。

日志不要进 stdout（那是 MCP stdio 传输的通道，混写会污染协议）：
  * MCP stdio 模式自动降级为写文件
  * 非 stdio 模式输出到 stderr
"""

from __future__ import annotations

import logging
import os
import sys

LOG_DIR_ENV = "DBT_LOG_DIR"
_LEVEL_ENV = "DBT_LOG_LEVEL"

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".dbtoolchain", "logs")

_ROOT = logging.getLogger("dbcore")
_CONFIGURED = False
MCP_STDIO_MODE = False


def setup(level: str | None = None, stdio: bool | None = None) -> logging.Logger:
    global _CONFIGURED, MCP_STDIO_MODE
    if _CONFIGURED:
        return _ROOT

    if stdio is None:
        stdio = "--stdio" in sys.argv or os.environ.get("MCP_TRANSPORT") == "stdio"
    MCP_STDIO_MODE = stdio

    lvl = getattr(logging, (level or os.environ.get(_LEVEL_ENV) or "WARNING").upper(), logging.WARNING)
    _ROOT.setLevel(lvl)
    _ROOT.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_dir = os.environ.get(LOG_DIR_ENV, DEFAULT_DIR)
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir, "dbtoolchain.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(lvl)
        _ROOT.addHandler(fh)
    except Exception:
        pass

    # stdout 绝不能沾染 MCP 协议流；非 stdio 模式才打 stderr
    if not stdio:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(lvl)
        _ROOT.addHandler(sh)

    _CONFIGURED = True
    return _ROOT


def get(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup()
    return logging.getLogger(f"dbcore.{name}")


def log_path() -> str:
    return os.path.join(os.environ.get(LOG_DIR_ENV, DEFAULT_DIR), "dbtoolchain.log")
