# -*- coding: utf-8 -*-
"""
tools.tests.test_backfill —— 知识库向量回填纯逻辑与索引测试
============================================================
本文件用途：验证 ``tools.kb_embedding``（POL-004）的纯逻辑与
``common.services.kb_indexing`` 的索引行为：
- decide_backfill / build_content_text：差额判定与文本拼接（纯函数）；
- build_content_hash：内容哈希稳定性（同输入同输出）；
- index_knowledge_content：embedding 未启用 / 失败 / 成功三分支（mock）。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from common.models.base import Base
from common.models.config_models import LlmConfig
from common.models.knowledge_models import KbEmbedding
from common.services.kb_indexing import (
    build_content_hash,
    index_knowledge_content,
    is_embedding_enabled,
)
from tools.kb_embedding.backfill import build_content_text, decide_backfill


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------
def test_decide_backfill_missing_vector_rebuild() -> None:
    """无向量 → rebuild。"""
    assert decide_backfill({"vector_json": None, "content_hash": "x"}) == "rebuild"
    assert decide_backfill({"content_hash": "x"}) == "rebuild"


def test_decide_backfill_has_vector_skip() -> None:
    """有向量 → skip。"""
    assert decide_backfill({"vector_json": "[0.1, 0.2]", "content_hash": "x"}) == "skip"


def test_build_content_text_joins_parts() -> None:
    """拼接标题 + 内容 + 标签。"""
    assert build_content_text("退换货", "七天无理由", "售后") == "退换货 七天无理由 售后"
    assert build_content_text("", "", "") == ""


def test_build_content_hash_stable() -> None:
    """同一文本两次哈希一致。"""
    assert build_content_hash("测试文本") == build_content_hash("测试文本")


# ---------------------------------------------------------------------------
# 索引行为测试（内存 SQLite）
# ---------------------------------------------------------------------------
@pytest.fixture()
def session() -> Session:
    """提供 SQLite 内存库会话并按模型建表。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _enable_embedding(session: Session, shop_pk: int) -> None:
    """为该店铺启用 embedding（写入 LlmConfig）。"""
    from common.utils.crypto import encrypt_text

    session.add(
        LlmConfig(
            shop_pk=shop_pk,
            provider_type="openai_compatible",
            model_name="chat-model",
            api_key_enc=encrypt_text("sk-test-key"),
            api_base="https://api.example.com/v1",
            ai_enabled=True,
            embedding_enabled=True,
            embedding_model="text-embedding-v3",
        )
    )
    session.commit()


def test_is_embedding_enabled_false_by_default(session: Session) -> None:
    """未配置 embedding → False。"""
    assert is_embedding_enabled(session, shop_pk=1) is False


def test_is_embedding_enabled_true_after_config(session: Session) -> None:
    """配置 embedding_enabled=True → True。"""
    _enable_embedding(session, 1)
    assert is_embedding_enabled(session, shop_pk=1) is True


def test_index_knowledge_content_success(session: Session, monkeypatch) -> None:
    """embedding 启用 + 网络成功 → upsert 写入 KbEmbedding。"""
    _enable_embedding(session, 1)
    # mock embed_texts 避免真实网络，返回 2 维向量。
    import common.services.kb_indexing as ki

    monkeypatch.setattr(
        ki, "embed_texts", lambda *a, **kw: [[0.1, 0.2, 0.3]]
    )
    ok = index_knowledge_content(
        session, shop_pk=1, source_type="cs_knowledge", source_id=10, content_text="退换货政策"
    )
    assert ok is True
    row = session.query(KbEmbedding).filter_by(source_id=10).one()
    assert row.vector_json == "[0.1, 0.2, 0.3]"
    assert row.content_hash == build_content_hash("退换货政策")


def test_index_knowledge_content_disabled_skips(session: Session) -> None:
    """embedding 未启用 → 返回 False 且不写入。"""
    ok = index_knowledge_content(
        session, shop_pk=1, source_type="cs_knowledge", source_id=10, content_text="内容"
    )
    assert ok is False
    assert session.query(KbEmbedding).count() == 0


def test_index_knowledge_content_failure_is_safe(session: Session, monkeypatch) -> None:
    """网络失败 → 返回 False，不抛异常、不阻断写操作。"""
    _enable_embedding(session, 1)
    import common.services.kb_indexing as ki

    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(ki, "embed_texts", boom)
    ok = index_knowledge_content(
        session, shop_pk=1, source_type="cs_knowledge", source_id=10, content_text="内容"
    )
    assert ok is False
    assert session.query(KbEmbedding).count() == 0


def test_index_knowledge_content_empty_text_skips(session: Session, monkeypatch) -> None:
    """空文本 → 返回 False，不调用网络。"""
    _enable_embedding(session, 1)
    import common.services.kb_indexing as ki

    called = {"n": 0}

    def fake_embed(*a, **kw):
        called["n"] += 1
        return [[0.1]]

    monkeypatch.setattr(ki, "embed_texts", fake_embed)
    ok = index_knowledge_content(
        session, shop_pk=1, source_type="cs_knowledge", source_id=10, content_text="   "
    )
    assert ok is False
    assert called["n"] == 0
