# -*- coding: utf-8 -*-
"""
逻辑删除保留数据属性测试（common/tests/test_repository_soft_delete_property.py）
==============================================================================
本文件用途：以 Hypothesis 属性测试验证 common.db.repository.Repository 的逻辑
删除（soft_delete / soft_delete_by）满足设计文档 Property 5「逻辑删除保留数据」：
对任意业务记录，执行删除 / 停用 / 移出操作后，记录仍存在于存储中、记录总行数
不变，仅其状态字段变为停用 / 失效。

说明：
- 被测实现：Repository.soft_delete（按主键逻辑删除，通过状态字段更新实现）。
- 被测模型：Shop（含 status 字段：1=启用，0=停用即逻辑删除）。
- 验证方式：使用 SQLite 内存库 + common 模型建表（参照任务 2.8，无需真实 MySQL）。
- 状态串扰规避：每个 Hypothesis example 内部新建独立内存库并建表，example 之间
  互不影响（function_scoped_fixture 与 @given 结合易告警，故在测试内部自建库）。
- 最少迭代 100 次（max_examples=200，满足规范要求）。
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from common.db.repository import Repository
from common.models.base import Base
from common.models.shop_models import Shop

# 生成 1~10 条店铺记录的随机数量（业务键 shop_id 用序号保证唯一）。
_record_counts = st.integers(min_value=1, max_value=10)


@settings(max_examples=200, deadline=None)
@given(
    count=_record_counts,
    # data 用于在已知记录数后再生成「待删除下标集合」，约束到合法输入空间。
    data=st.data(),
)
def test_property_5_soft_delete_preserves_data(count: int, data: st.DataObject):
    # Feature: pdd-auto-reply, Property 5: 逻辑删除保留数据
    # Validates: Requirements 2.8, 3.5, 9.5, 12.5, 19.5, 24.6

    # 每个 example 使用独立的内存库 + 全新建表，避免跨 example 状态串扰。
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            repo: Repository[Shop] = Repository(Shop, session)

            # 插入 count 条店铺记录，初始 status 均为启用（1）。
            # 显式指定主键：SQLite 仅对 INTEGER rowid 自增，BigInteger 主键
            # 不会自动生成，故测试中手动赋递增主键（不影响逻辑删除语义）。
            pks: list[int] = []
            for i in range(count):
                shop = repo.create(
                    id=i + 1,
                    shop_id=f"shop-{i}",
                    shop_name=f"店铺{i}",
                    owner_user_id=1,
                    status=1,
                )
                pks.append(shop.id)
            session.flush()

            total_before = repo.count()
            assert total_before == count

            # 随机选取待逻辑删除的记录下标子集（可能为空，亦为合法情形）。
            to_delete_idx = data.draw(
                st.lists(
                    st.integers(min_value=0, max_value=count - 1),
                    unique=True,
                    max_size=count,
                )
            )
            deleted_pks = {pks[i] for i in to_delete_idx}

            # 执行逻辑删除：仅按主键更新状态字段，不做物理删除。
            for pk in deleted_pks:
                assert repo.soft_delete(pk) is True
            session.flush()

            # 断言一：总行数不变（逻辑删除不减少存储中的记录数）。
            assert repo.count() == total_before

            # 断言二 / 三：逐条核验记录仍存在，且状态字段按是否被删而变化。
            for pk in pks:
                obj = repo.get(pk)
                # 被删与未删记录都必须仍可查到（记录保留）。
                assert obj is not None
                if pk in deleted_pks:
                    # 被删记录：状态字段变为停用值 0。
                    assert obj.status == 0
                else:
                    # 未删记录：状态保持启用值 1，不受影响。
                    assert obj.status == 1
    finally:
        # 释放内存库连接，确保 example 间资源干净。
        engine.dispose()
