# -*- coding: utf-8 -*-
"""
tools 测试公共夹具与路径配置
==============================
本文件用途：保证 tools 测试可导入被测模块（tools.tiktok_acceptance.*）与公共库
（common.*）。仓库根目录加入 sys.path 以支持 `import tools.*` 与 `import common.*`。

SQLite 测试方言适配：common 模型主键统一为 BigInteger 自增（规范 9，面向生产
MySQL）；SQLite 仅对 INTEGER PRIMARY KEY 自动赋值自增 rowid，因此在测试基础设施层
将 BigInteger 在 sqlite 方言下编译为 INTEGER，使内存库可正常建表与插入。此适配仅
作用于测试，不改变生产模型定义与 MySQL 行为。
"""
import os
import sys

# 仓库根目录 = tools/tests 的向上三级（tools/tests -> tools -> 仓库根）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(BigInteger, "sqlite")
def _compile_big_integer_sqlite(type_, compiler, **kw):  # pragma: no cover - 测试基础设施
    """在 SQLite 方言下将 BigInteger 渲染为 INTEGER，以支持自增主键。"""
    return "INTEGER"
