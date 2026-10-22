#!/usr/bin/env python3
"""按依赖顺序构建 / 校验 / 上传 10 个包。

为什么需要它：这些包互相依赖，且版本锁步。发错顺序（先发下游）会导致
上游还没上 PyPI，pip 解析失败；漏发一个则会留下一个装不上的包。
所以顺序由 repo_config 的依赖图算出来，不靠记忆。

用法：
    python scripts/publish.py --check                 # 只构建 + twine check（默认，不联网上传）
    python scripts/publish.py --check --only sqltk-core # 单个包
    python scripts/publish.py --upload testpypi       # 传到 TestPyPI（预演）
    python scripts/publish.py --upload pypi           # 正式发布
    python scripts/publish.py --upload pypi --only mcp-sql-review   # 补发单个

前置：
    pip install build twine
    国内建议加 --index-url https://pypi.tuna.tsinghua.edu.cn/simple
    （默认 PyPI 在部分网络下不可达，构建时会因拉不到 setuptools 而失败）

上传凭据走 twine 的标准方式（任选）：
    export TWINE_USERNAME=__token__
    export TWINE_PASSWORD=pypi-xxxx
    或 ~/.pypirc
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import repo_config as C  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run(cmd: list[str], cwd: pathlib.Path, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env:
        e.update(env)
    # stdin 必须掐断：twine 在缺凭据时会交互式提问，capture_output 下会静默挂住，
    # 看上去像「卡住不动」，实际是在等一个永远不会来的输入。
    return subprocess.run(cmd, cwd=str(cwd), env=e, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def check_tooling() -> list[str]:
    """构建工具是否装了。

    不查这里的后果是 10 个包各报一次「No module named build」，
    真正的原因淹没在重复噪音里。
    """
    import importlib.util

    problems = []
    for mod in ("build", "twine"):
        if importlib.util.find_spec(mod) is None:
            problems.append(f"缺 {mod}：pip install build twine")
    return problems


def pypirc_path() -> pathlib.Path:
    return pathlib.Path.home() / ".pypirc"


def check_credentials(index: str) -> list[str]:
    """上传前确认凭据存在。

    twine 缺凭据时是**交互式提示**而不是报错 —— 在脚本 / CI 里表现为永久挂起。
    宁可在这里提前退出。
    """
    if os.environ.get("TWINE_PASSWORD") and os.environ.get("TWINE_USERNAME"):
        return []
    rc = pypirc_path()
    if rc.exists():
        text = rc.read_text(encoding="utf-8", errors="replace")
        # 只做粗判断：有对应 [section] 且该段里有 password。解析细节交给 twine。
        m = re.search(rf"^\[{re.escape(index)}\](.*?)(?=^\[|\Z)", text, re.M | re.S)
        if m and "password" in m.group(1):
            return []
    return [
        f"没有 {index} 的上传凭据，twine 会转去交互式提问（脚本里会挂住）。\n"
        f"        export TWINE_USERNAME=__token__\n"
        f"        export TWINE_PASSWORD=pypi-xxxx\n"
        f"        或写 ~/.pypirc（当前路径：{rc}）"
    ]


def pypi_name_taken(name: str) -> bool | None:
    """True=已占用，False=可用，None=查不到（离线/超时，不做判断）。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=8) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except Exception:
        return None


def check_names(only: list[dict]) -> list[str]:
    """上传前的名字核对。

    为什么必做：`dbt-core` 在 PyPI 上已被 dbt Labs 占用（1.12.6）。
    名字冲突不会在本地构建时暴露 —— 构建、twine check 全过，
    直到用户 `pip install` 才失败，或者更糟：pip 装上了别人的包，
    import 时才发现没有 dbcore 模块。
    """
    problems: list[str] = []
    unknown: list[str] = []
    for repo in only:
        taken = pypi_name_taken(repo["name"])
        if taken is True:
            problems.append(f"{repo['name']}: PyPI 上已被占用，换 name 后重跑 make gen")
        elif taken is None:
            unknown.append(repo["name"])
    if unknown:
        print(f"（无法联网核对以下包名，跳过：{', '.join(unknown)}）")
    return problems


def check_versions() -> list[str]:
    """`__init__.__version__` 与 repo_config.VERSION 必须一致。

    版本号有 11 处物理存在（repo_config + 10 个 __init__.py）。
    漏改一处不会让任何测试失败，但装出来的包会自称另一个版本 ——
    报 bug 时对不上号。
    """
    problems = []
    for repo in C.REPOS:
        init = ROOT / repo["dir"] / "src" / repo["pkg"] / "__init__.py"
        if not init.exists():
            problems.append(f"{repo['dir']}: 缺 src/{repo['pkg']}/__init__.py")
            continue
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
        if not m:
            problems.append(f"{repo['dir']}: __init__.py 没有 __version__")
        elif m.group(1) != C.VERSION:
            problems.append(f"{repo['dir']}: __version__={m.group(1)} ≠ VERSION={C.VERSION}")
    return problems


def preflight() -> list[str]:
    """构建前能查的错，全部一次报出来，不要发到一半才失败。"""
    problems: list[str] = []

    versions = {}
    for repo in C.REPOS:
        p = ROOT / repo["dir"] / "pyproject.toml"
        if not p.exists():
            problems.append(f"{repo['dir']}: 缺 pyproject.toml")
            continue
        text = p.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("version = "):
                versions.setdefault(line.split("=")[1].strip().strip('"'), []).append(repo["dir"])
                break

    if len(versions) > 1:
        problems.append(f"版本号不一致：{versions}")
    for repo in C.REPOS:
        d = ROOT / repo["dir"]
        for f in ("LICENSE", "README.md", "src/%s/py.typed" % repo["pkg"]):
            if not (d / f).exists():
                problems.append(f"{repo['dir']}: 缺 {f}")
    try:
        C.install_order()
    except SystemExit as exc:
        problems.append(str(exc))
    problems += check_versions()
    return problems


def build_one(repo: dict, index_url: str | None) -> tuple[bool, str]:
    d = ROOT / repo["dir"]
    dist = d / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    for junk in ("build",):
        if (d / junk).exists():
            shutil.rmtree(d / junk)
    for egg in d.glob("*.egg-info"):
        shutil.rmtree(egg, ignore_errors=True)

    env = {"PIP_INDEX_URL": index_url} if index_url else None
    r = run([sys.executable, "-m", "build", "--outdir", "dist", "."], d, env)
    if r.returncode != 0:
        return False, "构建失败：\n" + (r.stderr or r.stdout)[-1200:]

    arts = sorted(p.name for p in dist.iterdir())
    if not any(a.endswith(".whl") for a in arts):
        return False, f"没产出 wheel：{arts}"

    r = run([sys.executable, "-m", "twine", "check", *[str(dist / a) for a in arts]], d)
    if r.returncode != 0:
        return False, "twine check 未通过：\n" + (r.stdout + r.stderr)[-1200:]
    return True, "  ".join(arts)


def upload_one(repo: dict, index: str) -> tuple[bool, str]:
    d = ROOT / repo["dir"]
    arts = [str(p) for p in sorted((d / "dist").iterdir())]
    r = run([sys.executable, "-m", "twine", "upload", "--repository", index,
             "--skip-existing", *arts], d)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr)[-1500:]
    return True, f"已上传 {len(arts)} 个文件 → {index}"


def main() -> int:
    ap = argparse.ArgumentParser(description="按依赖顺序构建 / 上传工具链各包")
    ap.add_argument("--check", action="store_true", help="只构建 + 校验（默认）")
    ap.add_argument("--upload", choices=["testpypi", "pypi"], help="构建后上传到指定索引")
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="只处理指定包（可多次），顺序仍按依赖关系")
    ap.add_argument("--index-url", default=None,
                    help="构建时 pip 用的源，如 https://pypi.tuna.tsinghua.edu.cn/simple")
    ap.add_argument("--skip-build", action="store_true", help="复用已有 dist/，只上传")
    ap.add_argument("--allow-placeholder-url", action="store_true",
                    help="允许 GH_OWNER 仍为占位符时上传（默认禁止：死链上 PyPI 比晚发更糟）")
    ap.add_argument("--no-name-check", action="store_true",
                    help="跳过 PyPI 包名占用核对（离线时用）")
    args = ap.parse_args()

    # ---- 预检
    problems = preflight() + check_tooling()
    if args.upload:
        problems += check_credentials(args.upload)
    if problems:
        print("✗ 预检未通过：")
        for p in problems:
            print("  -", p)
        return 1

    order = C.install_order()
    if args.only:
        unknown = set(args.only) - set(C.BY_NAME)
        if unknown:
            print(f"✗ 未知包名：{sorted(unknown)}")
            return 1
        order = [r for r in order if r["name"] in args.only]

    if args.upload == "pypi" and C.placeholder_urls() and not args.allow_placeholder_url:
        print(
            f"✗ 拒绝上传到正式 PyPI：GH_OWNER 仍是占位符 {C.GH_OWNER!r}。\n"
            "  包元数据里的 Homepage / Repository / Issues 会指向不存在的地址 ——\n"
            "  这类元数据一旦上传就无法修改（只能删包重发）。\n"
            "  改 scripts/repo_config.py 的 GH_OWNER 后重跑 gen-pyprojects.py。\n"
            "  确实要带占位符上传，加 --allow-placeholder-url。"
        )
        return 1

    print(f"版本 {C.VERSION}｜{len(order)} 个包｜顺序：" +
          " → ".join(r["name"] for r in order) + "\n")

    # 名字核对只在上传时做（构建不需要联网）。名字被占用是硬阻断：
    # 本地构建、twine check 全都看不出来，只有用户 pip install 时才会炸。
    if args.upload and not args.no_name_check:
        name_problems = check_names(order)
        if name_problems:
            print("✗ 包名核对未通过：")
            for p in name_problems:
                print("  -", p)
            return 1
        print("✓ 包名可用\n")

    failed: list[tuple[str, str]] = []
    for i, repo in enumerate(order, 1):
        tag = f"[{i}/{len(order)}] {repo['name']}"
        if args.skip_build:
            ok, msg = True, "跳过构建"
        else:
            ok, msg = build_one(repo, args.index_url)
        if not ok:
            print(f"{tag}  ✗")
            failed.append((repo["name"], msg))
            print("       " + msg.replace("\n", "\n       ")[:1500])
            continue
        print(f"{tag}  ✓ 构建  {msg}")

        if args.upload:
            ok, msg = upload_one(repo, args.upload)
            if not ok:
                print(f"{tag}  ✗ 上传失败")
                failed.append((repo["name"], msg))
                print("       " + msg.replace("\n", "\n       ")[:1500])
                # 上游没上，下游的依赖必然解析不到，继续发只会连环失败
                rest = [r["name"] for r in order[i:]]
                if rest:
                    print(f"       已中止，未处理：{', '.join(rest)}")
                break
            print(f"{tag}  ✓ {msg}")

    if failed:
        print(f"\n✗ {len(failed)} 个包失败：" + ", ".join(n for n, _ in failed))
        return 1

    if not args.upload:
        print("\n✓ 全部构建并通过 twine check。加 --upload testpypi 先做预演。")
    else:
        print(f"\n✓ 全部处理完成（{args.upload}）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
