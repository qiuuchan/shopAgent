# -*- coding: utf-8 -*-
"""
common.tests.test_kb_embedding —— KbEmbedding 向量存储单元测试
==============================================================
本文件用途：以 SQLite 内存库验证 ``common.models.knowledge_models.KbEmbedding``
（POL-003）表结构与仓储行为：
- 建表：autocreate 后表存在、唯一约束已声明；
- upsert 幂等：同一 (shop_pk, source_type, source_id) 更新而非重复插入；
- 按店查询：限定店铺过滤生效；
- 唯一约束生效：插入业务键相同的第二行触发 IntegrityError；
- SchemaMigrator 幂等：两次 init_database 风格迁移不报错、不重复建表/补列。

统一沿用 common 仓库层 Repository 的 upsert / list 接口（规范 12/16），
测试基础设施与既有 common 测试一致：SQLite 内存库 + BigInteger→INTEGER 适配
（conftest 全局注册，此处直接使用内存引擎自行建表）。
"""
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from common.db.init_database import SchemaMigrator, clear_dict_initializers
from common.db.repository import Repository
from common.models.base import Base
from common.models.knowledge_models import KbEmbedding


@pytest.fixture()
def session() -> Session:
    """提供基于 SQLite 内存库的事务性会话，并按模型建表。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _make_kb(session: Session, **overrides) -> KbEmbedding:
    """插入一条 KbEmbedding（默认值 + 可覆盖关键字）。"""
    payload = {
        "shop_pk": 1,
        "source_type": "cs_knowledge",
        "source_id": 100,
        "content_hash": "abc123",
        "model_name": "text-embedding-v3",
        "vector_json": "[0.1, 0.2, 0.3]",
        "enabled": True,
    }
    payload.update(overrides)
    rec = KbEmbedding(**payload)
    session.add(rec)
    session.commit()
    return rec


def test_table_created_with_unique_constraint(session: Session) -> None:
    """建表后表存在，且声明了 (shop_pk, source_type, source_id) 唯一约束。"""
    inspector = inspect(session.get_bind())
    assert "pdd_kb_embedding" in inspector.get_table_names()
    unique_cols = {tuple(uc["column_names"]) for uc in inspector.get_unique_constraints("pdd_kb_embedding")}
    assert ("shop_pk", "source_type", "source_id") in unique_cols


def test_upsert_idempotent(session: Session) -> None:
    """同一业务键 upsert 幂等：更新向量而不重复插入。"""
    repo = Repository(KbEmbedding, session)
    rec = _make_kb(session, vector_json="[0.1, 0.2, 0.3]")
    # 再次 upsert 相同业务键，但内容变化 → 更新而非新增
    repo.upsert(
        {"shop_pk": 1, "source_type": "cs_knowledge", "source_id": 100},
        {"vector_json": "[0.9, 0.8, 0.7]", "content_hash": "newhash"},
    )
    session.flush()
    rows = repo.list(filters={"shop_pk": 1})
    assert len(rows) == 1
    assert rows[0].id == rec.id
    assert rows[0].vector_json == "[0.9, 0.8, 0.7]"
    assert rows[0].content_hash == "newhash"


def test_query_by_shop(session: Session) -> None:
    """按店查询：跨店铺数据不返回。"""
    _make_kb(session, shop_pk=1)
    _make_kb(session, shop_pk=2)
    repo = Repository(KbEmbedding, session)
    rows = repo.list(filters={"shop_pk": 1})
    assert len(rows) == 1
    assert rows[0].shop_pk == 1


def test_unique_constraint_enforced(session: Session) -> None:
    """插入相同 (shop_pk, source_type, source_id) 第二行触发唯一约束。"""
    _make_kb(session, source_id=100)
    with pytest.raises(IntegrityError):
        session.add(
            KbEmbedding(
                shop_pk=1,
                source_type="cs_knowledge",
                source_id=100,
                content_hash="other",
                model_name="text-embedding-v3",
                vector_json="[0.5]",
            )
        )
        session.commit()


def test_schema_migrator_idempotent() -> None:
    """SchemaMigrator 两次运行幂等：第二次不报错、不重复建表/补列。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    clear_dict_initializers()
    try:
        migrator = SchemaMigrator(engine=engine, metadata=Base.metadata)
        first = migrator.run()
        assert first.changed is True
        assert "pdd_kb_embedding" in first.created_tables

        second = migrator.run()
        assert second.changed is False
        assert second.created_tables == []
        assert second.added_columns == []
        assert second.added_unique_indexes == []
    finally:
        clear_dict_initializers()
        engine.dispose()


def test_enabled_flag_default_true(session: Session) -> None:
    """enabled 默认 True。"""
    rec = _make_kb(session)
    session.refresh(rec)
    assert rec.enabled is True
