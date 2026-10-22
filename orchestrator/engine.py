"""编排引擎 —— 按 next_actions（或显式 recipe）串起跨 MCP 调用链。

设计原则（与 DESIGN.md §2/§4 一致）：
  * 任何工具返回同一信封 Result，引擎无差别解析
  * 关联三件套 correlation_id / instance_id / sql_fingerprint 沿链路自动透传
  * 幂等收敛：同一 (mcp, tool, args) 不重复执行；全局步数有上限，杜绝死循环
  * 失败隔离：某一步失败只标记该步，不拖垮整条链，且不追它的 next_actions
  * 护栏：受控变更类工具（dry_run/apply）是否自动跑由调用方显式决定，
    引擎本身不替业务拍板「要不要真执行」

两种驱动：
  Orchestrator.run(mcp, tool, args, follow_optional=...)  —— 自动追链
  Orchestrator.run_recipe([...])                           —— 确定性流水线
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from dbcore.protocol import (
    Result,
    ToolError,
    new_correlation_id,
    worst_verdict,
)
from dbcore.ids import sql_fingerprint, normalize_sql_for_fingerprint

from .registry import Registry, build_registry


@dataclass
class Step:
    index: int
    mcp: str
    tool: str
    args: dict
    parent: Optional[int] = None          # 由哪一步的 next_action 触发
    reason: str = ""                       # 触发原因（来自 next_action.reason）
    ok: bool = False
    skipped: bool = False                 # 因重复执行被剪枝
    error: str = ""
    error_code: str = ""
    result: Optional[Result] = None
    started_ms: int = 0
    took_ms: int = 0
    signature: str = ""                    # 去重指纹

    def display_args(self) -> dict:
        """用于展示的参数副本：抹掉可能含密码的 url。"""
        out = {}
        for k, v in self.args.items():
            if k in ("url", "dsn", "connection") and isinstance(v, str):
                out[k] = _redact_url(v)
            else:
                out[k] = v
        return out


@dataclass
class ExecutionTrace:
    correlation_id: str
    seed: dict
    steps: list[Step] = field(default_factory=list)
    created_ms: int = 0
    max_steps: int = 0
    truncated: bool = False                # 是否因达到 max_steps 而截停

    def final_verdict(self) -> str:
        return worst_verdict([s.result.verdict for s in self.steps
                              if s.result and s.result.verdict])

    def final_tier(self) -> str:
        order = {"tier0": 0, "tier1": 1, "tier2": 2}
        tiers = [s.result.tier for s in self.steps if s.result and s.result.tier]
        return min(tiers, key=lambda t: order.get(t, 9)) if tiers else "tier0"

    def final_confidence(self) -> str:
        order = {"high": 0, "medium": 1, "low": 2}
        confs = [s.result.confidence for s in self.steps if s.result and s.result.confidence]
        return max(confs, key=lambda c: order.get(c, 9)) if confs else "low"

    def warnings(self) -> list[str]:
        out: list[str] = []
        for s in self.steps:
            if s.result and s.result.warnings:
                out.extend(s.result.warnings)
        return out

    def defused_next_actions(self) -> list[dict]:
        """各步产出的 next_actions 中，本轮没有自动追的（供人决策）。"""
        out: list[dict] = []
        seen = {(s.mcp, s.tool) for s in self.steps}
        for s in self.steps:
            if not s.result:
                continue
            for na in s.result.next_actions:
                target = (na.mcp, na.tool)
                if target in seen:
                    continue
                out.append({
                    "from_step": s.index, "mcp": na.mcp, "tool": na.tool,
                    "reason": na.reason, "optional": na.optional,
                    "priority": na.priority,
                })
        return out

    def to_dict(self) -> dict:
        return {
            "correlation_id": self.correlation_id,
            "seed": self.seed,
            "created_ms": self.created_ms,
            "max_steps": self.max_steps,
            "truncated": self.truncated,
            "step_count": len(self.steps),
            "final_verdict": self.final_verdict(),
            "final_tier": self.final_tier(),
            "final_confidence": self.final_confidence(),
            "warnings": self.warnings(),
            "steps": [
                {
                    "index": s.index, "mcp": s.mcp, "tool": s.tool,
                    "args": s.display_args(), "parent": s.parent, "reason": s.reason,
                    "ok": s.ok, "skipped": s.skipped, "error": s.error,
                    "error_code": s.error_code,
                    "verdict": s.result.verdict if s.result else None,
                    "tier": s.result.tier if s.result else None,
                    "summary": s.result.summary if s.result else None,
                    "took_ms": s.took_ms,
                }
                for s in self.steps
            ],
            "defused_next_actions": self.defused_next_actions(),
        }


def _redact_url(u: str) -> str:
    # mysql://user:pass@host:3306/db -> mysql://user:***@host:3306/db
    import re
    return re.sub(r"(://[^:/@]+:)[^@/]+(@)", r"\1***\2", u)


def _sig(mcp: str, tool: str, args: dict) -> str:
    """去重指纹：工具名 + 参数稳定序列化（按 key 排序）。"""
    import hashlib, json
    norm = {k: _norm_arg(v) for k, v in args.items() if k != "correlation_id"}
    blob = json.dumps({"mcp": mcp, "tool": tool, "args": norm},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _norm_arg(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return [_norm_arg(x) for x in v]
    if isinstance(v, dict):
        return {k: _norm_arg(x) for k, x in sorted(v.items())}
    return v


class Orchestrator:
    def __init__(self, registry: Registry | None = None, max_steps: int = 50,
                 follow_optional: bool = False) -> None:
        self.reg = registry or build_registry()
        self.max_steps = max_steps
        self.follow_optional = follow_optional
        self._seen: set[str] = set()          # 已执行（用于去重 + 防环）
        self._pending: set[str] = set()       # 已入队（避免同一步重复声明导致的重复执行）

    # -- 公开 API -------------------------------------------------

    def run(self, mcp: str | None, tool: str, args: dict | None = None,
            instance_id: str = "", correlation_id: str = "",
            follow_optional: bool | None = None) -> ExecutionTrace:
        """给一个入口工具，照着 next_actions 自动追链。"""
        follow = self.follow_optional if follow_optional is None else follow_optional
        cid = correlation_id or new_correlation_id()
        trace = ExecutionTrace(correlation_id=cid,
                               seed={"mcp": mcp, "tool": tool, "args": args or {}},
                               created_ms=int(time.time()), max_steps=self.max_steps)
        self._pending = set()
        queue: list[tuple] = [(mcp, tool, args or {}, None, "")]
        while queue and len(trace.steps) < self.max_steps:
            m, t, a, parent, reason = queue.pop(0)
            self._execute(trace, m, t, a, parent, reason, cid, instance_id)
            # 从刚执行的最后一步收集 next_actions
            last = trace.steps[-1] if trace.steps else None
            if last and last.result and not last.skipped:
                for na in last.result.next_actions:
                    if na.optional and not follow:
                        continue
                    nsig = _sig(na.mcp, na.tool, na.args)
                    if nsig in self._seen or nsig in self._pending:
                        continue
                    self._pending.add(nsig)
                    queue.append((na.mcp, na.tool, dict(na.args), last.index, na.reason))
        if len(trace.steps) >= self.max_steps:
            trace.truncated = True
        return trace

    def run_many(self, seeds: Iterable[tuple], **kw) -> ExecutionTrace:
        """多个入口同时追链，共用一个 correlation_id。"""
        seeds = list(seeds)
        if not seeds:
            raise ToolError("E_ARGS", "run_many 需要至少一个入口")
        cid = kw.get("correlation_id") or new_correlation_id()
        first = seeds[0]
        trace = self.run(first[0], first[1], (first[2] if len(first) > 2 else None),
                         instance_id=kw.get("instance_id", ""), correlation_id=cid,
                         follow_optional=kw.get("follow_optional"))
        for s in seeds[1:]:
            sub = self.run(s[0], s[1], (s[2] if len(s) > 2 else None),
                           instance_id=kw.get("instance_id", ""), correlation_id=cid,
                           follow_optional=kw.get("follow_optional"))
            trace.steps.extend(sub.steps)
        return trace

    def run_recipe(self, steps: list[dict], instance_id: str = "",
                   correlation_id: str = "") -> ExecutionTrace:
        """确定性流水线：按给定顺序依次执行，不自动追 next_actions。

        steps 元素：{"mcp":.., "tool":.., "args":..} 或 {"tool":.., "args":..}
        （省略 mcp 时按名字解析）。适合 CI / 受控变更这类要可复现的场景。
        """
        cid = correlation_id or new_correlation_id()
        trace = ExecutionTrace(correlation_id=cid,
                               seed={"recipe": [s.get("tool") or s.get("mcp") for s in steps]},
                               created_ms=int(time.time()), max_steps=self.max_steps)
        for i, step in enumerate(steps):
            m = step.get("mcp")
            t = step.get("tool") or step.get("mcp")
            a = dict(step.get("args") or {})
            self._execute(trace, m, t, a, None, step.get("reason", "recipe"),
                          cid, instance_id)
            if len(trace.steps) >= self.max_steps:
                trace.truncated = True
                break
        return trace

    # -- 内部 -----------------------------------------------------

    def _execute(self, trace, mcp, tool, args, parent, reason, cid, instance_id) -> None:
        t0 = time.time()
        sig = _sig(mcp or "", tool, args)
        if sig in self._seen:
            step = Step(index=len(trace.steps), mcp=mcp or "?", tool=tool,
                        args=args, parent=parent, reason=reason, skipped=True,
                        signature=sig, started_ms=int(t0 * 1000))
            trace.steps.append(step)
            return
        self._seen.add(sig)

        step = Step(index=len(trace.steps), mcp=mcp or "?", tool=tool,
                    args=args, parent=parent, reason=reason, signature=sig,
                    started_ms=int(t0 * 1000))
        try:
            entry = self.reg.resolve(mcp, tool)
            step.mcp = entry.mcp
            step.tool = entry.tool
            fn = entry.func
            call_args = self._inject(entry.func, dict(args), cid, instance_id)
            missing = self._missing_required(fn, call_args)
            if missing:
                step.ok = False
                step.error = f"缺少必填参数：{', '.join(missing)}"
                step.error_code = "E_ARGS"
                step.took_ms = int((time.time() - t0) * 1000)
                trace.steps.append(step)
                return
            result = fn(**call_args)
            if not isinstance(result, Result):
                result = Result(ok=True, mcp=entry.mcp, tool=entry.tool,
                                data={"value": result})
            step.result = result
            step.ok = result.ok
            if not result.ok and result.error:
                step.error = result.error.get("message", "")
                step.error_code = result.error.get("code", "")
        except ToolError as exc:
            step.ok = False
            step.error = exc.message
            step.error_code = exc.code
        except Exception as exc:  # 绝不因单步异常拖垮整条链
            step.ok = False
            step.error = f"{type(exc).__name__}: {exc}"
            step.error_code = "E_QUERY"
        finally:
            step.took_ms = int((time.time() - t0) * 1000)
            trace.steps.append(step)

    @staticmethod
    def _missing_required(fn: Callable, args: dict) -> list[str]:
        """返回函数调用仍缺失的必填参数名（用于给出干净的「缺参数」失败而不是 TypeError）。"""
        try:
            params = inspect.signature(fn).parameters
        except (ValueError, TypeError):
            return []
        missing = []
        for name, p in params.items():
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if p.default is not inspect.Parameter.empty:
                continue
            if name not in args:
                missing.append(name)
        return missing

    @staticmethod
    def _inject(fn: Callable, args: dict, cid: str, instance_id: str) -> dict:
        """把关联字段塞进参数（仅当函数接受时才塞，避免 TypeError）。"""
        try:
            params = inspect.signature(fn).parameters
        except (ValueError, TypeError):
            return args
        out = dict(args)
        for key, val in (("correlation_id", cid), ("instance_id", instance_id)):
            if key in params and params[key].kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                # instance_id 仅在未显式提供时才注入
                if key == "instance_id" and out.get("instance_id"):
                    continue
                out[key] = val
        # 若提供了 sql 且函数接受，附上指纹（供下游一站式关联）
        if "sql" in out and "sql_fingerprint" in params and "sql_fingerprint" not in out:
            try:
                out["sql_fingerprint"] = sql_fingerprint(out["sql"])
            except Exception:
                pass
        # 过滤掉函数不接受的多余参数
        return {k: v for k, v in out.items() if k in params}
