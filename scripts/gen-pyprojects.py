#!/usr/bin/env python3
"""从 scripts/repo_config.py 生成各仓的发布相关文件。

生成四类文件（每仓）：
    pyproject.toml          发布元数据（补齐 LICENSE / authors / classifiers / urls）
    LICENSE                 许可正文
    src/<pkg>/py.typed      类型标记（原来 package-data 声明了但文件不存在，
                            导致下游看不到任何类型提示）
    .github/workflows/ci.yml  各仓独立 CI（3.10–3.13 矩阵）

用法：
    python scripts/gen-pyprojects.py            # 写入
    python scripts/gen-pyprojects.py --check    # 只比对，不一致则退出 1（CI 用）
    python scripts/gen-pyprojects.py --diff     # 打印差异细节

为什么用生成器而不是手改 10 份：
    这些字段 10 个仓必须完全一致。手抄已经漂过一次（10 份全都是
    license = "Proprietary" 且没有 LICENSE 文件），而漂移是静默的 ——
    没有任何测试会失败，直到 pip 装上之后才会发现链接是死的。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import repo_config as C  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

MIT_TEXT = """MIT License

Copyright (c) {year}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

HEADER = (
    "# 本文件由 scripts/gen-pyprojects.py 从 scripts/repo_config.py 生成，不要手改。\n"
    "# 改元数据请改 repo_config.py 后重跑：python scripts/gen-pyprojects.py\n"
)


def q(text: str) -> str:
    """TOML 基本字符串。"""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def classifiers(repo: dict) -> list[str]:
    """顺序即展示顺序，按语义分组排好，不要 sorted()（3.10 会排到 3.9 前面）。"""
    out = ["Development Status :: 4 - Beta"]
    if repo["kind"] != "lib":
        out.append("Environment :: Console")
    out += [
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3 :: Only",
    ]
    # 版本号按数值排序，避免 "3.10" < "3.9" 的字符串排序错位
    out += [
        f"Programming Language :: Python :: {v}"
        for v in sorted(C.py_versions(repo), key=lambda s: tuple(int(x) for x in s.split(".")))
    ]
    out.append("Topic :: Database")
    if repo["quality_assurance"]:
        out.append("Topic :: Software Development :: Quality Assurance")
    if repo["kind"] == "lib":
        out.append("Topic :: Software Development :: Libraries :: Python Modules")
    out.append("Typing :: Typed")
    return out


def gen_pyproject(repo: dict) -> str:
    lines: list[str] = [HEADER.rstrip("\n"), ""]
    lines += [
        "[build-system]",
        'requires = ["setuptools>=77", "wheel"]',
        'build-backend = "setuptools.build_meta"',
        "",
        "[project]",
        f"name = {q(repo['name'])}",
        f"version = {q(C.VERSION)}",
        f"description = {q(repo['description'])}",
        'readme = "README.md"',
        f"requires-python = {q(C.requires_python(repo))}",
        # PEP 639：SPDX 表达式 + license-files。比 license = {text = "..."} 规范，
        # PyPI 能正确识别并展示协议，sdist/wheel 里也会真的带上 LICENSE。
        f"license = {q(C.LICENSE)}",
        'license-files = ["LICENSE"]',
    ]

    authors = ", ".join(
        "{ name = %s, email = %s }" % (q(a["name"]), q(a["email"])) for a in C.AUTHORS
    )
    lines.append(f"authors = [{authors}]")
    lines.append("maintainers = [%s]" % authors)

    kws = C.SHARED_KEYWORDS + repo["keywords"]
    lines.append("keywords = [%s]" % ", ".join(q(k) for k in kws))

    lines.append("classifiers = [")
    lines += [f"    {q(c)}," for c in classifiers(repo)]
    lines.append("]")
    lines.append("")

    if repo["deps"]:
        lines.append("dependencies = [")
        lines += [f"    {q(d)}," for d in repo["deps"]]
        lines.append("]")
    else:
        lines.append("dependencies = []")
    lines.append("")

    if repo["extras"]:
        lines.append("[project.optional-dependencies]")
        for extra, items in repo["extras"].items():
            lines.append(f"{extra} = [{', '.join(q(i) for i in items)}]")
        lines.append("")

    if repo.get("script"):
        left, right = repo["script"].split(" = ")
        lines += ["[project.scripts]", f"{left} = {q(right)}", ""]

    lines.append("[project.urls]")
    for label, url in C.repo_urls(repo).items():
        lines.append(f"{label} = {q(url)}")
    lines.append("")

    lines += [
        "[tool.setuptools.packages.find]",
        'where = ["src"]',
        "",
        "[tool.setuptools.package-data]",
        '"*" = ["py.typed", "*.yaml", "*.yml", "*.json"]',
        "",
        "[tool.ruff]",
        "line-length = 110",
        'target-version = "py39"' if repo["kind"] == "lib" else 'target-version = "py310"',
        "",
        "[tool.ruff.lint]",
        'select = ["F", "E9"]',
        "",
    ]
    return "\n".join(lines)


def _ci_dep_steps(repo: dict) -> tuple[list[str], list[str]]:
    """返回（checkout 步骤, pip 安装参数）。"""
    steps: list[str] = []
    install_args: list[str] = []
    for spec in repo["deps"]:
        name = spec.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip()
        if name not in C.BY_NAME:
            continue
        extra = ""
        if "[" in spec:
            extra = spec[spec.index("[") : spec.index("]") + 1]
        steps += [
            "      - name: 取回依赖仓 %s（尚未发到 PyPI，用同一 owner 下的源码）" % name,
            "        uses: actions/checkout@v4",
            "        with:",
            "          repository: ${{ github.repository_owner }}/%s" % name,
            "          path: _deps/%s" % name,
            "",
        ]
        install_args.append('-e "_deps/%s%s"' % (name, extra))
    return steps, install_args


def gen_ci(repo: dict) -> str:
    dep_steps, dep_args = _ci_dep_steps(repo)
    versions = ", ".join(f'"{v}"' for v in C.py_versions(repo))

    lines = [
        "# 本文件由 scripts/gen-pyprojects.py 生成，不要手改。",
        "name: ci",
        "",
        "on:",
        "  push:",
        "    branches: [main]",
        '    tags: ["v*"]',
        "  pull_request:",
        "  workflow_dispatch:",
        "",
        "concurrency:",
        "  group: ci-${{ github.ref }}",
        "  cancel-in-progress: true",
        "",
        "jobs:",
        "  test:",
        "    name: py${{ matrix.python }}",
        "    runs-on: ubuntu-latest",
        "    strategy:",
        "      fail-fast: false",
        "      matrix:",
        f"        python: [{versions}]",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "",
        "      - uses: actions/setup-python@v5",
        "        with:",
        "          python-version: ${{ matrix.python }}",
        "",
    ]
    lines += dep_steps

    install_cmd = " ".join(dep_args + ['-e "."'])
    lines += [
        "      - name: 安装",
        "        run: |",
        "          python -m pip install --upgrade pip",
        f"          pip install {install_cmd}",
        "          pip install ruff twine",
        "",
        "      - name: 静态检查",
        "        run: ruff check src tests",
        "",
    ]

    if repo["kind"] == "lib":
        lines += [
            "      - name: 库用法不依赖 MCP SDK（延迟导入是硬承诺）",
            "        run: |",
            "          python -c \"import sys, %s; assert 'mcp' not in sys.modules, 'import 阶段就加载了 mcp'\"" % repo["pkg"],
            "",
        ]

    lines += [
        "      - name: 测试",
        "        run: |",
        "          for f in tests/test_*.py; do",
        '            echo "== $f"',
        '            python "$f"',
        "          done",
        "",
        "      - name: 打包元数据自检",
        "        run: |",
        "          python -m build --outdir _dist .",
        "          twine check _dist/*",
        "",
    ]
    if repo.get("script"):
        cmd = repo["script"].split(" = ")[0]
        lines += [
            "      - name: CLI 可用",
            f"        run: {cmd} tools | head -5",
            "",
        ]
    return "\n".join(lines)


def gen_meta_ci() -> str:
    """顶层 meta 仓的 CI：把 10 个子仓拉齐，跑边界 + 元数据 + 全部测试。"""
    lines = [
        "# 本文件由 scripts/gen-pyprojects.py 生成，不要手改。",
        "name: ci",
        "",
        "on:",
        "  push:",
        "    branches: [main]",
        '    tags: ["v*"]',
        "  pull_request:",
        "  workflow_dispatch:",
        "",
        "concurrency:",
        "  group: ci-${{ github.ref }}",
        "  cancel-in-progress: true",
        "",
        "jobs:",
        "  test:",
        "    name: py${{ matrix.python }}",
        "    runs-on: ubuntu-latest",
        "    strategy:",
        "      fail-fast: false",
        "      matrix:",
        '        python: ["3.10", "3.13"]',
        "    steps:",
        "      - uses: actions/checkout@v4",
        "",
        "      - uses: actions/setup-python@v5",
        "        with:",
        "          python-version: ${{ matrix.python }}",
        "",
        "      # 子仓各自是独立仓库，逐个取回原位（scripts/ 与 tests/ 依赖这个布局）",
    ]
    for repo in C.REPOS:
        lines += [
            f"      - name: 取回 {repo['dir']}",
            "        uses: actions/checkout@v4",
            "        with:",
            "          repository: ${{ github.repository_owner }}/%s" % repo["name"],
            "          path: %s" % repo["dir"],
        ]
    lines += [
        "",
        "      - name: 安装（按依赖拓扑序）",
        "        run: |",
        "          python -m pip install --upgrade pip",
    ]
    for repo in C.install_order():
        lines.append("          pip install -e %s" % repo["dir"])
    lines += [
        "          pip install ruff",
        "",
        "      - name: 元数据未漂移（pyproject / LICENSE / py.typed / CI）",
        "        run: python scripts/gen-pyprojects.py --check",
        "",
        "      - name: 依赖边界（MCP 不许互 import，库不许依赖 MCP）",
        "        run: python scripts/check-boundaries.py",
        "",
        "      - name: 静态检查",
        "        run: ruff check --select F,E9 .",
        "",
        "      - name: 全部测试",
        "        run: python scripts/test-all.py",
        "",
    ]
    return "\n".join(lines)


def gen_license() -> str:
    year = C.COPYRIGHT.split()[0]
    return MIT_TEXT.format(year=year)


def targets() -> list[tuple[pathlib.Path, str]]:
    out: list[tuple[pathlib.Path, str]] = []
    for repo in C.REPOS:
        d = ROOT / repo["dir"]
        out.append((d / "pyproject.toml", gen_pyproject(repo)))
        out.append((d / "LICENSE", gen_license()))
        out.append((d / "src" / repo["pkg"] / "py.typed", ""))
        out.append((d / ".github" / "workflows" / "ci.yml", gen_ci(repo)))
    out.append((ROOT / ".github" / "workflows" / "ci.yml", gen_meta_ci()))
    return out


def main() -> int:
    check = "--check" in sys.argv
    show_diff = "--diff" in sys.argv

    if C.placeholder_urls():
        print(
            f"警告：GH_OWNER 仍是占位符 {C.GH_OWNER!r}。\n"
            "      pyproject 里的 Homepage / Repository / Issues 会指向不存在的地址。\n"
            "      发布前请在 scripts/repo_config.py 里改成真实 GitHub 账号。\n"
        )

    changed, same = [], 0
    for path, content in targets():
        old = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None
        if old == content:
            same += 1
            continue
        changed.append(path)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        if show_diff and old is not None:
            import difflib

            rel = path.relative_to(ROOT)
            diff = difflib.unified_diff(
                old.splitlines(), content.splitlines(), f"{rel} (现状)", f"{rel} (生成)", lineterm=""
            )
            print("\n".join(list(diff)[:40]))

    if check:
        if changed:
            print(f"✗ {len(changed)} 个文件与生成器不一致（{same} 个一致）：")
            for p in changed:
                print("   ", p.relative_to(ROOT))
            print("\n修法：python scripts/gen-pyprojects.py")
            return 1
        print(f"✓ 元数据一致（{same} 个文件）")
        return 0

    print(f"✓ 已生成 {len(changed)} 个文件，{same} 个已是最新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
