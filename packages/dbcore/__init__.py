"""dbcore - 数据库 AI 工具链共享内核。

设计约束：
1. 纯 Python，除 sqlglot 外不引入重依赖；
2. 数据库驱动（pymysql / psycopg）为可选依赖，缺失时给出清晰报错而不是崩溃；
3. 任何对目标库的连接必须是只读会话 + 语句超时 + 单语句。
"""

from dbcore.advisor import advise_indexes, plan_index
from dbcore.dialect import normalize_dialect, parse_statements, split_sql
from dbcore.fix import suggest_fixes
from dbcore.guard import GuardError, assert_readonly, ensure_single_statement
from dbcore.risk import assess_change, generate_rollback
from dbcore.rules import review_sql
from dbcore.verify import equivalence_check, plan_compare

__all__ = [
    "normalize_dialect",
    "parse_statements",
    "split_sql",
    "GuardError",
    "assert_readonly",
    "ensure_single_statement",
    "review_sql",
    "suggest_fixes",
    "assess_change",
    "generate_rollback",
    "plan_compare",
    "equivalence_check",
    "plan_index",
    "advise_indexes",
]

__version__ = "0.1.0"
