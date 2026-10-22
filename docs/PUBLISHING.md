# 发布手册

10 个包，版本锁步，各自独立发布。本文按实际执行顺序写，每一步都能跑命令验证。

---

## 0. 一次性准备

### 0.1 填 GitHub 账号

`scripts/repo_config.py` 里的 `GH_OWNER` 现在是占位符 `YOUR_GH_OWNER`。pyproject 的
`Homepage` / `Repository` / `Issues` 由它拼出来。

```bash
# 改 scripts/repo_config.py 的 GH_OWNER = "你的账号"
make gen          # 同步到 10 个 pyproject
```

为什么必须先填：**PyPI 上的元数据一旦上传就不能改**（只能删包重发，且同名同版本永久占用）。
带着死链上传，用户点进项目主页会看到 404。

### 0.2 账号与 token

- PyPI 和 TestPyPI 是**两套独立账号**，要分别注册。
- 都建议开 API token（`pypi-` 开头），而不是用密码上传。
- token 作用域：首次发布用 "整个账号"，之后可以收窄到具体项目。

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-xxxxxxxx      # TestPyPI 的 token 也放这个变量
```

### 0.3 本地工具

```bash
pip install build twine
```

**网络注意**：`python -m build` 会为每个包创建隔离环境并从索引拉 `setuptools>=77`。
索引可达性随网络环境变化 —— 本机实测过两个方向：默认 PyPI 失败而清华镜像成功、
隔一段时间又反过来。失败现象是 `Could not find a version that satisfies the requirement setuptools`。

所以 `Makefile` 把源做成了变量，随时换：

```bash
make build                                            # 用当前环境的 pip 源
make build INDEX=https://pypi.tuna.tsinghua.edu.cn/simple   # 临时换源
# 或直接调脚本
python scripts/publish.py --check --index-url https://pypi.org/simple
```

---

## 1. 预检

```bash
make check-meta     # 元数据是否漂移（10 个 pyproject / LICENSE / py.typed / CI）
make check          # 边界检查 + 全部测试 + 6 个 MCP 的挂载冒烟
```

`check-meta` 失败说明有人手改了生成的产物。修法不是改回去，而是：

```bash
# 改 scripts/repo_config.py，然后
make gen
```

### 脚本自带的三道预检

`publish.py` 在任何一次运行开头都会查完这些才动手，避免"发到第 7 个包才失败"：

| 检查 | 触发时机 | 为什么必须提前 |
|---|---|---|
| 版本一致（11 处 `__version__`） | 总是 | 漏改一处不会让任何测试失败，但装出来的包会自称另一个版本 |
| `build` / `twine` 已安装 | 总是 | 否则 10 个包各报一次 `No module named build`，真因淹没在噪音里 |
| 上传凭据存在 | 仅 `--upload` | twine 缺凭据时是**交互式提问**而非报错，在脚本里表现为永久挂起 |

凭据判断只看两处：`TWINE_USERNAME` + `TWINE_PASSWORD` 环境变量，或 `~/.pypirc` 里对应
`[pypi]` / `[testpypi]` 段中是否有 `password`。宁可在这里提前退出，也不要让进程挂在那儿。

### 包名占用（已自动化，但值得知道为什么）

`publish.py` 在上传前会逐个查 `https://pypi.org/pypi/<name>/json`，被占用就拒绝上传。
这条检查是 2026-10 实打实换来的：**`dbt-core` 早已被 dbt Labs 占用**（PyPI 上 1.12.6），
`dbt` 这个命令也被它注册。当时 10 个包名都写着 `dbt-*`，本地构建、`twine check` 全过，
但从 wheel 装到干净环境才炸 —— pip 把依赖解析到了别人的 `dbt-core`，`import dbcore` 直接失败。

所以现在的名字是 `sqltk-core` / `sqltk-store` / `sqltk-know` / `sqltk-orchestrator`，
6 个 MCP 保持 `mcp-*`（可用且利于在 MCP 生态里被搜到）。离线时可 `--no-name-check` 跳过核对。

---

## 2. 构建（不联网上传）

```bash
make build
# 等价于
python scripts/publish.py --check --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

它会按依赖拓扑序处理 10 个包：

```
sqltk-core / sqltk-store / sqltk-know → 6 个 MCP → sqltk-orchestrator
```

每个包产出 `dist/` 下的 wheel + sdist，并过 `twine check`（校验 README 渲染、元数据完整性）。

单包调试：

```bash
python scripts/publish.py --check --only mcp-sql-review
```

---

## 3. 从 wheel 装一遍（最有价值的一步）

`twine check` 只校验元数据格式，**不校验依赖声明是否完整**。真正的检验是从构建产物装到一个
干净的 venv 里，跑起来。这条已经脚本化：

```bash
make verify          # = python scripts/verify-wheels.py
```

它开两个干净 venv 各做一件事：

| 场景 | 装什么 | 断言 |
|---|---|---|
| A 按需安装 | 只有 `mcp-sql-review` | 自动带上 `sqltk-core` + `sqltk-store`，且 **`dbknow` 没被拖进来**；`pip check` 干净；`python -m sql_review tools` 能跑 |
| B 整图 | `sqltk-orchestrator` | 6 个 MCP 全部到位；`pip check` 干净；`smoke-mcp.py --installed` 6 个 server 全部挂得上；编排器 CLI 起得来 |

`--keep` 保留 venv 便于手工进去看，`--only-a` 只跑快的那个。

手工排查时等价的命令（场景 A）：

```bash
python -m venv /tmp/vfy
V=/tmp/vfy/Scripts/pip        # Linux/macOS 用 /tmp/vfy/bin/pip
PYV=/tmp/vfy/Scripts/python

# 用本地 wheel 当唯一源，强制 pip 从产物解析依赖
$V install --no-index \
  --find-links sqltk-core/dist --find-links sqltk-store/dist \
  --find-links mcp-sql-review/dist \
  mcp-sql-review

$PYV -c "import sql_review, dbcore, dbstore; print('导入 OK')"
$PYV -m sql_review tools | head -5

# 用**装好的包**做一次协议层挂载冒烟（--installed 不注入源码路径）
$PYV scripts/smoke-mcp.py --installed --only sql-review
```

这一步能抓到的东西，前面几步都抓不到：

| 症状 | 根因 |
|---|---|
| `No matching distribution found for sqltk-core` | 某个 MCP 漏声明了共享库依赖 |
| 装上了但 `ModuleNotFoundError: dbcore` | `packages.find` 的 `where` 写错，源码没进 wheel |
| 装上了但类型提示全丢 | `py.typed` 没打进包（`package-data` 声明了但文件不存在） |
| `pip install` 在 3.9 上解析失败 | `requires-python` 与实际依赖的下限不符（`mcp` SDK 要 3.10+） |

---

## 4. TestPyPI 预演（以及它的局限）

```bash
make publish-test
# 等价于 python scripts/publish.py --upload testpypi
```

**关键局限**：TestPyPI 上**没有你的依赖**。所以

```bash
pip install --index-url https://test.pypi.org/simple/ mcp-sql-review   # 会失败
```

会报找不到 `sqltk-core` —— 因为 sqltk-core 也只在 TestPyPI 上，而 pip 默认不在 TestPyPI 里解析
传递依赖（要加 `--extra-index-url https://pypi.org/simple` 才能混着找，但首次发布时 PyPI 上同样没有）。

所以 TestPyPI 的预演价值是**验证上传流程与元数据展示**（权限、token 作用域、README 渲染、
classifier 是否正确），**不是验证依赖图**。依赖图靠第 3 步验证。

---

## 5. 正式发布

```bash
make publish
# 等价于 python scripts/publish.py --upload pypi
```

脚本的保护：

- `GH_OWNER` 还是占位符时**拒绝**上传正式 PyPI（`--allow-placeholder-url` 可强制，不建议）。
- 预检不通过（版本不一致、缺 LICENSE、依赖成环）直接退出，不会发一半。
- 某个包上传失败就**中止**，不会继续发下游 —— 上游没上，下游的 `>=0.2.0` 必然解析不到。

**建议分两批发**，中间隔一次验证：

```bash
# 第一批：三个库
python scripts/publish.py --upload pypi --only sqltk-core --only sqltk-store --only sqltk-know

# 等 1-2 分钟（PyPI 索引有延迟），验证
pip download sqltk-core==0.2.0 -d /tmp/x --no-deps

# 第二批：6 个 MCP + 编排器
make publish
```

---

## 6. 打 tag

10 个仓各自打 tag（它们有各自的 git 历史）：

```bash
for d in sqltk-core sqltk-store sqltk-know mcp-sql-review mcp-change-risk mcp-dbwatch \
         mcp-migration-diff mcp-data-semantics mcp-profile-hub sqltk-orchestrator; do
  (cd $d && git tag -a v0.2.0 -m "v0.2.0" && git push origin v0.2.0)
done
```

---

## 7. 升版本

只改一处：

```python
# scripts/repo_config.py
VERSION = "0.3.0"
```

```bash
make gen     # 10 个 pyproject 的 version 与依赖下限一起更新
make check   # 确认测试仍全过
```

依赖约束写的是 `>=0.2.0` 这种下限，所以升版本后旧版本仍可共存，不会互相踢掉。

---

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `File already exists` / `400 The use of the filename ... has already been used` | 该版本号已上传过 | **版本号不能复用**。升 VERSION 重发；已发的只能 `yank`（yank 后仍可被 `==` 精确安装） |
| `twine check` 报 `InvalidDistribution: Invalid email` | authors 格式不对 | 改 `repo_config.AUTHORS`，重跑 `make gen` |
| `error: invalid command 'bdist_wheel'` | 缺 wheel | `pip install wheel` |
| 构建时拉不到 setuptools | 默认 PyPI 源不可达 | 加 `--index-url` 用镜像 |
| `401 Unauthorized` | token 错 / 作用域不含新项目 | 用账号级 token，或先在网页端创建项目占位 |
| `403 Invalid or non-existent authentication` | 拿 PyPI token 传了 TestPyPI | 两套 token 不通用 |
| 装上了但 import 报 `ModuleNotFoundError` | 包目录没被 `packages.find` 找到 | 检查 `where = ["src"]` 与目录名 |
| 3.9 上装 MCP 报无可用版本 | `requires-python` 正确拦截 | 预期行为，MCP 需要 3.10+ |
| `publish.py` 跑到一半不动了，没有任何输出 | 缺凭据，twine 在等交互式输入 | 新版本已提前拦截；旧版本检查 `TWINE_PASSWORD` 是否在当前 shell 里 |

---

## 已知未做（发布前可以不做）

- **CI 里的自动发布**：目前发布是手动 `make publish`。加 `pypa/gh-action-pypi-publish` +
  trusted publishing（OIDC，免 token）更安全，但需要先在 PyPI 配好每个仓库的 trusted publisher，
  10 个仓配 10 次。数量级上不急。
- **签名（sigstore）**：`twine` 支持 `--sign-with`，PyPI 也支持 attestations。等有真实用户后再加。
- **文档站**：现在 docs 是仓内 markdown，没有 ReadTheDocs / GitHub Pages。
