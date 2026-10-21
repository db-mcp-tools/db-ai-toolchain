"""SQL 扫描器 —— 从文件与 git diff 里把 SQL 抓出来。

CI 场景的真实需求是"只体检本次改动的 SQL"，而不是每次全量扫。
所以要能：
  * 从 .sql 文件里按语句切分
  * 从 MyBatis/MyBatis-Plus 的 XML mapper 里抽出 <select>/<insert>/<update>/<delete>
  * 从 Java/Kotlin 的 @Select/@Update 注解和字符串常量里抽 SQL
  * 从 git diff 里只取新增/修改的行，定位到真实 SQL 片段

这是 CLI 门禁（dbt sql-review --diff）的基础。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, asdict

# MyBatis 标签 MyBatis-Plus 也一样
_XML_SQL_TAGS = ("select", "insert", "update", "delete", "sql")
_XML_RE = re.compile(
    r"<(?P<tag>" + "|".join(_XML_SQL_TAGS) + r")\b[^>]*>(?P<body>.*?)</(?P=tag)>",
    re.I | re.S,
)
_XML_DYNAMIC_RE = re.compile(
    r"<(if|where|set|foreach|choose|trim|bind)\b[^>]*>|</(if|where|set|foreach|choose|trim|bind)>",
    re.I,
)

_ANNOTATION_RE = re.compile(
    r"@(Select|Insert|Update|Delete|SelectProvider|UpdateProvider|InsertProvider|DeleteProvider)"
    r"\s*\(\s*(?:value\s*=\s*)?\"(?P<body>[^\"]+)\"",
    re.I | re.S,
)
_STRING_SQL_RE = re.compile(
    r"""(?P<q>["']{1,3})\s*(?P<body>(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WITH|CREATE\s+TABLE|ALTER\s+TABLE|TRUNCATE)\b[^"']{20,}?)(?P=q)""",
    re.I | re.S,
)

# 判断一行文本是否"像 SQL"：至少要有一个强特征
_SQLISH_RE = re.compile(
    r"\b(SELECT\s+.{3,}\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE|TRUNCATE)\b",
    re.I | re.S,
)

_EXT_MAP = {
    ".sql": "sql", ".mysql": "sql", ".pgsql": "sql", ".psql": "sql",
    ".xml": "xml", ".java": "java", ".kt": "java", ".py": "py",
    ".go": "go", ".ts": "ts", ".js": "ts",
}

DEFAULT_EXTS = tuple(_EXT_MAP)


@dataclass
class SqlItem:
    sql: str
    file: str = ""
    line: int = 0
    source: str = ""          # file | git-diff | xml | annotation
    statement: str = ""       # xml id 之类

    def to_dict(self) -> dict:
        return asdict(self)


def looks_like_sql(text: str) -> bool:
    return bool(_SQLISH_RE.search(text or ""))


def clean_xml_body(body: str) -> str:
    """去掉 MyBatis 的动态标签与 CDATA 包裹，剩下的才是 SQL。"""
    b = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", body, flags=re.S)
    b = _XML_DYNAMIC_RE.sub(" ", b)
    # MyBatis 占位符 -> 通用占位符便于解析
    b = re.sub(r"#\{[^}]*\}", "? ", b)
    b = re.sub(r"\$\{[^}]*\}", "? ", b)
    b = re.sub(r"\s+", " ", b)
    return b.strip()


def extract_from_text(text: str, file: str = "", source: str = "file",
                      kind: str | None = None) -> list[SqlItem]:
    ext = os.path.splitext(file or "")[1].lower()
    kind = kind or _EXT_MAP.get(ext, "sql")
    items: list[SqlItem] = []

    def add(sql: str, line: int, src: str, stmt: str = "") -> None:
        sql = sql.strip().strip(";").strip()
        if len(sql) < 8 or not looks_like_sql(sql):
            return
        items.append(SqlItem(sql=sql, file=file, line=line, source=src, statement=stmt))

    if kind == "xml":
        for m in _XML_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            idm = re.search(r"id\s*=\s*[\"']([^\"']+)", m.group(0)[:200])
            add(clean_xml_body(m.group("body")), line, f"{source}:{m.group('tag')}",
                idm.group(1) if idm else "")
        if not items:
            add(clean_xml_body(text), 1, f"{source}:xml")
    elif kind == "java":
        for m in _ANNOTATION_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            add(m.group("body"), line, f"{source}:annotation")
        for m in _STRING_SQL_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            add(m.group("body"), line, f"{source}:string")
    else:
        # .sql 直接按 ; 切（忽略引号内的分号）
        for chunk in split_sql_statements(text):
            line = text[:max(0, text.find(chunk[:40]))].count("\n") + 1 if chunk[:40] in text else 1
            add(chunk, line, f"{source}:sql")
    return items


def split_sql_statements(text: str) -> list[str]:
    """按分号切分，跳过字符串/注释里的分号。"""
    out, buf = [], []
    quote: str | None = None
    in_line_comment = in_block_comment = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                buf.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == "\\":
                if i + 1 < len(text):
                    buf.append(nxt)
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == ";":
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def extract_from_file(path: str) -> list[SqlItem]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError as exc:
        return [SqlItem(sql="", file=path, line=0, source=f"error:{exc}")]
    return extract_from_text(text, file=path, source="file")


def extract_from_dir(root: str, exts: tuple[str, ...] = DEFAULT_EXTS,
                     max_files: int = 2000) -> list[SqlItem]:
    items: list[SqlItem] = []
    count = 0
    skip_dirs = {".git", "node_modules", "__pycache__", "target", "build", "dist", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            if count >= max_files:
                return items
            count += 1
            items.extend(extract_from_file(os.path.join(dirpath, fn)))
    return items


def _git(repo: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "ignore")[:300])
        return r.stdout.decode("utf-8", "ignore")
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 git，无法计算 diff（请确认 git 已安装并在 PATH 中）") from exc


def diff_items(repo_root: str, base: str = "HEAD~1", head: str = "HEAD",
               exts: tuple[str, ...] = DEFAULT_EXTS) -> list[SqlItem]:
    """取出 base..head 之间新增/修改行里的 SQL 片段。

    git diff 的 + 行可能是一个不完整的 SQL 片段（例如 XML 的一行），
    所以这里按"连续 +/- 块"重组后再提取，而不是逐行扫描。
    """
    diff = _git(repo_root, "diff", "--unified=0", base, head)
    items: list[SqlItem] = []
    cur_file: str | None = None
    added_buffer: list[str] = []
    buf_start_line = 0

    def flush(file: str, buffer: list[str], start: int) -> None:
        text = "\n".join(buffer)
        if not file or not text.strip():
            return
        kind = _EXT_MAP.get(os.path.splitext(file)[1].lower(), "sql")
        if looks_like_sql(text):
            for it in extract_from_text(text, file=file, source="git-diff", kind=kind):
                it.line = max(start, it.line)
                items.append(it)

    hunk_line_no = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            flush(cur_file or "", added_buffer, buf_start_line)
            added_buffer = []
            cur_file = line[6:]
            ext = os.path.splitext(cur_file or "")[1].lower()
            if ext not in exts:
                cur_file = None
            continue
        if line.startswith("@@"):
            flush(cur_file or "", added_buffer, buf_start_line)
            added_buffer = []
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            hunk_line_no = int(m.group(1)) if m else 0
            buf_start_line = hunk_line_no
            continue
        if cur_file is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added_buffer.append(line[1:])
        elif added_buffer:
            pass
    flush(cur_file or "", added_buffer, buf_start_line)
    return items


def filter_dialect(items: list[SqlItem], dialect: str | None) -> list[SqlItem]:
    """MyBatis XML 常用 sharding 语法，过滤掉明显不是 SQL 的碎片。"""
    return [i for i in items if len(i.sql) >= 10]
