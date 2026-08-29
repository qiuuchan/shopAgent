# -*- coding: utf-8 -*-
"""
分页结果约束属性测试（common/tests/test_repository_paginate_property.py）
========================================================================
本文件用途：以 Hypothesis 属性测试验证 common.db.repository.Repository.paginate
满足设计文档 Property 7「分页结果约束」：对任意记录集合与分页参数
(page, page_size)，分页查询返回的条数不超过规整后的 page_size，``total`` 等于
符合条件的记录总数，且当按时间倒序（默认）时结果按时间从新到旧排列。

说明：
- 被测实现：Repository.paginate（结合 common.utils.pagination 的参数规整）。
- 被测模型：Shop（含 created_at 时间字段，可显式赋值以构造时间序列）。
- 验证方式：使用 SQLite 内存库 + common 模型建表（参照任务 2.8，无需真实 MySQL）。
- 输入空间：随机记录总数 N（含 0 与较大值）、随机 page（含非法值）、随机
  page_size（含合法 10/20/50/100 与非法值），非法值由规整逻辑回退为合法值。
- 状态串扰规避：每个 Hypothesis example 内部新建独立内存库并建表，example
  之间互不影响。
- 最少迭代 100 次（max_examples=200，满足规范要求）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from common.db.repository import Repository
from common.models.base import Base
from common.models.shop_models import Shop
from common.utils.pagination import ALLOWED_PAGE_SIZES, normalize_page_size

# 时间基准：用于为每条记录构造可区分的 created_at（北京时间口径）。
_BASE_TIME = datetime(2024, 1, 1, 0, 0, 0)

# 每条记录的 created_at 偏移秒数列表：长度即记录总数 N（含 0 条与较大值）；
# 偏移值允许重复，以覆盖「时间字段相等时由主键 id 二级排序」的边界。
_created_offsets = st.lists(
    st.integers(min_value=0, max_value=10_000),
    min_size=0,
    max_size=60,
)

# 页码：覆盖合法（>=1）与非法（<=0、非整数字符串）输入，由规整逻辑兜底。
_pages = st.one_of(
    st.integers(min_value=-5, max_value=20),
    st.sampled_from(["abc", None, 0, -1, 3.5]),
)

# 每页条数：覆盖合法可选值（10/20/50/100）与非法值（非可选整数、字符串、None）。
_page_sizes = st.one_of(
    st.sampled_from(list(ALLOWED_PAGE_SIZES)),
    st.integers(min_value=-10, max_value=1000),
    st.sampled_from(["xx", None, 0, 7, 999]),
)


@settings(max_examples=200, deadline=None)
@given(offsets=_created_offsets, page=_pages, page_size=_page_sizes)
def test_property_7_paginate_result_constraints(offsets, page, page_size):
    # Feature: pdd-auto-reply, Property 7: 分页结果约束
    # Validates: Requirements 3.3, 6.6, 7.5, 9.3, 10.6, 12.6, 14.1, 15.1, 18.5, 19.3

    n = len(offsets)

    # 每个 example 使用独立内存库 + 全新建表，避免跨 example 状态串扰。
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            repo: Repository[Shop] = Repository(Shop, session)

            # 插入 N 条店铺记录，显式赋予自增主键 id 与 created_at（可含相等值）。
            # 显式指定 id：SQLite 下 BIGINT 主键不会自动自增（仅 INTEGER 主键
            # 才作为 rowid 别名自增），故由测试自行分配确定性主键，既规避方言差异，
            # 又保证「时间字段相等时按主键 id 二级排序」的边界可被验证。
            for i, offset in enumerate(offsets):
                repo.create(
                    id=i + 1,
                    shop_id=f"shop-{i}",
                    shop_name=f"店铺{i}",
                    owner_user_id=1,
                    status=1,
                    created_at=_BASE_TIME + timedelta(seconds=offset),
                )
            session.flush()

            # 默认按时间倒序分页查询（desc_order=True）。
            result = repo.paginate(page=page, page_size=page_size)

            # 规整后的合法每页条数（非法输入被回退为默认 20）。
            expected_size = normalize_page_size(page_size)

            # 断言一：返回条数不超过规整后的 page_size。
            assert len(result.items) <= expected_size

            # 断言二：total 等于全表符合条件的记录总数。
            assert result.total == n

            # 断言三：结果中的 page_size 必为合法可选值（非法已被规整）。
            assert result.page_size in ALLOWED_PAGE_SIZES
            assert result.page_size == expected_size

            # 断言四：页码被规整为合法值（>= 1）。
            assert result.page >= 1

            # 断言五：按时间倒序时结果 created_at 从新到旧（非递增）排列。
            created_list = [item.created_at for item in result.items]
            for prev, cur in zip(created_list, created_list[1:]):
                assert prev >= cur
    finally:
        # 释放内存库连接，确保 example 间资源干净。
        engine.dispose()
