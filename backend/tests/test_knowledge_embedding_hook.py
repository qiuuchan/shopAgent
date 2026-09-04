# -*- coding: utf-8 -*-
"""
backend.tests.test_knowledge_embedding_hook —— 知识库写路径向量挂钩测试
======================================================================
本文件用途：验证 backend 知识库写路径（POL-004）的 best-effort 向量挂钩：
- 知识写入在 embedding 服务不可用时仍成功（不抛异常，有断言）；
- embedding 启用 + 成功后写入对应 KbEmbedding 向量；
- 开关关闭（embedding_enabled=False）时跳过，不触碰向量表。

复用 backend 内存 SQLite 会话夹具（db_session）。为隔离无关的复杂权限判定，
测试将 ``_resolve_shop_in_scope`` 打桩为「返回真实店铺 + 校验通过」，聚焦断言
「向量挂钩不阻断知识写操作 + 成功后落库」，不与数据范围逻辑耦合。
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm import Session

from common.models.knowledge_models import KbEmbedding
from common.models.shop_models import Shop
from common.services import kb_indexing
from backend.app.services import cs_knowledge_service
from backend.app.services.cs_knowledge_service import create_cs_knowledge

# 测试用户（仅提供 id，供 created_by 使用；权限已由 _patch_scope 打桩隔离）。
_user = SimpleNamespace(id=1)


def _seed_shop(db_session: Session) -> Shop:
    """插入一条店铺记录并返回（供数据范围校验打桩使用）。"""
    shop = Shop(shop_id="shop-hook-001", shop_name="向量挂钩测试店", owner_user_id=1, status=1)
    db_session.add(shop)
    db_session.flush()
    return shop


def _patch_scope(monkeypatch, db_session: Session, shop: Shop) -> None:
    """将店铺范围校验打桩为「校验通过」，隔离数据范围逻辑。"""
    monkeypatch.setattr(
        cs_knowledge_service,
        "_resolve_shop_in_scope",
        lambda session, user, shop_pk: (shop, None),
    )


def test_knowledge_create_succeeds_when_embedding_fails(
    db_session: Session, monkeypatch
) -> None:
    """embedding 失败（mock 返回 False）时知识写入仍成功，不抛异常。"""
    shop = _seed_shop(db_session)
    _patch_scope(monkeypatch, db_session, shop)
    monkeypatch.setattr(cs_knowledge_service, "index_knowledge_content", lambda *a, **kw: False)
    resp = create_cs_knowledge(db_session, user=_user, shop_pk=shop.id, title="退换货政策", content="七天无理由退货")
    assert resp.success is True
    assert resp.data["title"] == "退换货政策"
    assert db_session.query(KbEmbedding).count() == 0


def test_knowledge_create_skips_when_embedding_disabled(
    db_session: Session, monkeypatch
) -> None:
    """embedding 未启用（mock 返回 False）时跳过向量生成，知识仍创建成功。"""
    shop = _seed_shop(db_session)
    _patch_scope(monkeypatch, db_session, shop)
    monkeypatch.setattr(cs_knowledge_service, "index_knowledge_content", lambda *a, **kw: False)
    resp = create_cs_knowledge(db_session, user=_user, shop_pk=shop.id, title="物流时效", content="48小时发货")
    assert resp.success is True
    assert resp.data["title"] == "物流时效"
    assert db_session.query(KbEmbedding).count() == 0


def test_knowledge_create_indexes_vector_on_success(
    db_session: Session, monkeypatch
) -> None:
    """embedding 启用且成功时写入对应 KbEmbedding 向量。"""
    import hashlib

    shop = _seed_shop(db_session)
    _patch_scope(monkeypatch, db_session, shop)

    def fake_index(session, **kwargs) -> bool:
        content_hash = hashlib.sha256("退换货政策 七天无理由退货".encode()).hexdigest()[:64]
        session.add(
            KbEmbedding(
                shop_pk=kwargs["shop_pk"],
                source_type=kwargs["source_type"],
                source_id=kwargs["source_id"],
                content_hash=content_hash,
                model_name="text-embedding-v3",
                vector_json="[0.1, 0.2, 0.3]",
            )
        )
        session.flush()
        return True

    monkeypatch.setattr(cs_knowledge_service, "index_knowledge_content", fake_index)
    resp = create_cs_knowledge(db_session, user=_user, shop_pk=shop.id, title="退换货政策", content="七天无理由退货")
    assert resp.success is True
    rows = db_session.query(KbEmbedding).all()
    assert len(rows) == 1
    assert rows[0].source_type == "cs_knowledge"
    assert rows[0].vector_json == "[0.1, 0.2, 0.3]"


def test_knowledge_create_does_not_raise_on_index_exception(
    db_session: Session, monkeypatch
) -> None:
    """挂钩抛异常也不影响知识写入成功（best-effort 容错）。"""
    shop = _seed_shop(db_session)
    _patch_scope(monkeypatch, db_session, shop)

    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cs_knowledge_service, "index_knowledge_content", boom)
    resp = create_cs_knowledge(db_session, user=_user, shop_pk=shop.id, title="退换货政策", content="七天无理由退货")
    assert resp.success is True
    assert db_session.query(KbEmbedding).count() == 0
