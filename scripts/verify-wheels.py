#!/usr/bin/env python3
"""把构建产物装进干净 venv，按用户的方式验证一遍。

    python scripts/verify-wheels.py            # 两个场景都跑
    python scripts/verify-wheels.py --keep     # 保留 venv，便于进去手工看
    python scripts/verify-wheels.py --index-url https://pypi.tuna.tsinghua.edu.cn/simple

为什么不用 `twine check` 代替：twine 只校验元数据**格式**，不校验依赖能否解析。
`dbt-core` 那个坑（PyPI 上已被 dbt Labs 占用 1.12.6）就是本地构建全绿、
`twine check` 全过，装到干净环境才炸的 —— pip 把 `dbt-core>=0.2.0` 解析到了
别人的包，`import dbcore` 直接失败。

两个场景：

  A 按需安装：只装 `mcp-sql-review`，必须自动带上 sqltk-core / sqltk-store，
    且**不能**顺带拖进 sqltk-know —— 这是"按需安装"唯一的硬证明。
  B 整图：装 `sqltk-orchestrator`，6 个 MCP 全部到位，再用真实 MCP 客户端
    把每个 server 挂一次（调 smoke-mcp.py --installed）。

前置：先 `make build` 产出 10 个 dist/*.whl。
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile
import venv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import repo_config as C  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def vpy(root: pathlib.Path) -> pathlib.Path:
    p = root / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    if not p.exists():
        p = p.with_suffix(".exe")
    return p


def run(cmd: list[str], timeout: int = 600, **kw) -> subprocess.CompletedProcess:
    """stdin 必须掐断、必须有超时。

    否则 MCP 的 stdio server 一被误当普通命令调用，就会安静地等一个永远不会来的
    输入 —— 表现为整个验证脚本挂死（这不是假设，写这个脚本时真踩了一次）。
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL,
                              timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"超时（{timeout}s）：{' '.join(cmd)}")


def make_venv(tag: str) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix=f"vfy-{tag}-")) / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(d)
    return d


def pip(py: pathlib.Path, *args: str, index_url: str | None = None) -> subprocess.CompletedProcess:
    cmd = [str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    for name in args:
        cmd.append(name)
    if index_url:
        cmd += ["--index-url", index_url]
    return run(cmd)


def links(names: list[str]) -> list[str]:
    """把每个包的 dist/ 加进 find-links —— 依赖必须是本地 wheel 解析出来的。"""
    out: list[str] = []
    for name in names:
        d = ROOT / C.BY_NAME[name]["dir"] / "dist"
        out += ["--find-links", str(d)]
    return out


def missing_wheels() -> list[str]:
    bad = []
    for repo in C.REPOS:
        if not list((ROOT / repo["dir"] / "dist").glob("*.whl")):
            bad.append(repo["name"])
    return bad


def installed(py: pathlib.Path, module: str) -> bool:
    r = run([str(py), "-c",
             f"import importlib.util as u, sys; sys.exit(0 if u.find_spec({module!r}) else 1)"])
    return r.returncode == 0


def scenario_ondemand(index_url: str | None, keep: bool) -> list[str]:
    print("[A] 按需安装：只装 mcp-sql-review")
    venv_dir = make_venv("ondemand")
    if keep:
        print(f"  venv 保留在 {venv_dir}")
    try:
        py = vpy(venv_dir)
        r = pip(py, "mcp-sql-review==" + C.VERSION,
                *links(["sqltk-core", "sqltk-store", "mcp-sql-review"]), index_url=index_url)
        if r.returncode != 0:
            return ["装了 mcp-sql-review 失败（要么共享库依赖漏声明，要么多依赖了没给的包）：\n"
                    + (r.stderr or r.stdout)[-800:]]

        problems = []
        for mod, want in (("dbcore", True), ("dbstore", True), ("dbknow", False)):
            got = installed(py, mod)
            if got != want:
                problems.append(f"{mod} {'应该在' if want else '不该在'}，"
                                f"实际{'在' if got else '不在'}")
        if not problems:
            print("  依赖：dbcore + dbstore 到位，dbknow 未被拖入")

        r = run([str(py), "-m", "pip", "check"])
        if r.returncode != 0:
            problems.append("pip check 不通过：\n" + (r.stdout + r.stderr)[-600:])

        r = run([str(py), "-m", "sql_review", "tools"])
        out = r.stdout or ""
        if r.returncode != 0:
            problems.append("mcp-sql-review tools 跑不起来：\n" + (r.stdout + r.stderr)[-600:])
        elif "个工具" not in out or "review_sql" not in out:
            problems.append("mcp-sql-review tools 没列出工具表：\n" + out[-400:])
        else:
            print("  入口：python -m sql_review tools 正常")
        return problems
    finally:
        if not keep:
            shutil.rmtree(venv_dir.parent, ignore_errors=True)


def scenario_full(index_url: str | None, keep: bool) -> list[str]:
    print("[B] 整图：装 sqltk-orchestrator（应连带 6 个 MCP）")
    venv_dir = make_venv("full")
    if keep:
        print(f"  venv 保留在 {venv_dir}")
    try:
        py = vpy(venv_dir)
        names = [r["name"] for r in C.REPOS]
        r = pip(py, "sqltk-orchestrator==" + C.VERSION, *links(names), index_url=index_url)
        if r.returncode != 0:
            return ["装了 sqltk-orchestrator 失败：\n" + (r.stderr or r.stdout)[-800:]]

        problems = []
        for repo in C.REPOS:
            if repo["kind"] != "mcp":
                continue
            if not installed(py, repo["pkg"]):
                problems.append(f"{repo['name']} 没被连带装上（{repo['pkg']} 不可导入）")
        if not problems:
            print("  依赖：6 个 MCP + 3 个共享库全部到位")

        r = run([str(py), "-m", "pip", "check"])
        if r.returncode != 0:
            problems.append("pip check 不通过：\n" + (r.stdout + r.stderr)[-600:])

        r = run([str(py), str(ROOT / "scripts" / "smoke-mcp.py"), "--installed"])
        print("  " + (r.stdout or r.stderr).strip().replace("\n", "\n  "))
        if r.returncode != 0:
            problems.append("装好的包挂载冒烟失败（见上）")

        r = run([str(py), "-m", "dbt_orchestrator", "--help"])
        if r.returncode == 0:
            print("  入口：sqltk（dbt_orchestrator）正常")
        else:  # --help 在 argparse 下退 0，退别的说明 CLI 起不来
            problems.append("编排器 CLI 起不来：\n" + (r.stdout + r.stderr)[-600:])
        return problems
    finally:
        if not keep:
            shutil.rmtree(venv_dir.parent, ignore_errors=True)
    if not keep:
        shutil.rmtree(venv_dir.parent, ignore_errors=True)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="从 wheel 装进干净 venv 验证")
    ap.add_argument("--index-url", default=None, help="pip 源（拉 sqlglot / mcp 等第三方依赖用）")
    ap.add_argument("--keep", action="store_true", help="保留 venv 不删，便于手工检查")
    ap.add_argument("--only-a", action="store_true", help="只跑按需安装场景（快）")
    args = ap.parse_args()

    bad = missing_wheels()
    if bad:
        print("✗ 这些包还没有 dist/*.whl，先跑 make build：")
        print("  " + ", ".join(bad))
        return 2

    print(f"版本 {C.VERSION}｜Python {sys.version.split()[0]}\n")
    problems = scenario_ondemand(args.index_url, args.keep)
    if not args.only_a:
        problems += scenario_full(args.index_url, args.keep)

    print()
    if problems:
        print(f"✗ {len(problems)} 个问题：")
        for p in problems:
            print("  -", p)
        return 1
    print("✓ 装出来的包可用（按需安装成立、整图完整、协议层挂得上）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
