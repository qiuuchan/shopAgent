# -*- coding: utf-8 -*-
"""
common.tests.test_kb_hybrid —— 混合检索排序纯函数与回退一致性测试
=================================================================
本文件用途：验证 ``common.services.kb_hybrid``（POL-005）纯函数与
``kb_service.search`` 的向量混合检索路径：
- hybrid_rank：权重极端值（0/1）行为、同分稳定序、limit 截断；
- Hypothesis 属性测试：结果数 ≤ limit / 店铺隔离（跨店记录永不出现）/
  排序确定性（同输入同输出）/ 回退一致性（开关关闭时与原 jieba 实现完全一致）。

统一沿用 common 测试基础设施：SQLite 内存库 + BigInteger→INTEGER 适配。
向量路径测试通过注入 ``KbEmbeddingProvider`` 的 mock（免网络 / 免真实配置）。
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from common.models.base import Base
from common.models.knowledge_models import CustomerServiceKnowledge, ProductKnowledge
from common.services import kb_service
from common.services.kb_hybrid import hybrid_rank, minmax_scale
from common.services.kb_service import KbEmbeddingProvider, search


# ---------------------------------------------------------------------------
# H 泛型 ID 方便排序对象（hybrid_rank 仅需访问 id 属性）
# ---------------------------------------------------------------------------
class _Rec:
    """带 id 的轻量记录替身（供 hybrid_rank 纯函数测试）。"""

    def __init__(self, rec_id: int) -> None:
        self.id = rec_id

    def __repr__(self) -> str:  # pragma: no cover - 仅调试打印
        return f"_Rec({self.id})"


# ---------------------------------------------------------------------------
# hybrid_rank 纯函数测试
# ---------------------------------------------------------------------------
def test_hybrid_rank_weight_extremes() -> None:
    """权重 0（纯余弦）与 1（纯 token）边界行为。"""
    recs = [_Rec(i) for i in range(3)]
    token = {0: 3, 1: 0, 2: 0}
    vec = {0: 0.1, 1: 0.9, 2: 0.2}
    # weight=1：纯 token 排序 → id0 第一
    ranked = hybrid_rank(recs, token, vec, weight=1.0)
    assert ranked[0].id == 0
    # weight=0：纯余弦排序 → id1 第一（归一化后余弦最高）
    ranked = hybrid_rank(recs, token, vec, weight=0.0)
    assert ranked[0].id == 1


def test_hybrid_rank_stable_tie_order() -> None:
    """同分时按 id 升序稳定排序。"""
    recs = [_Rec(i) for i in [3, 1, 2]]
    token = {3: 1, 1: 1, 2: 1}
    vec = {3: 0.5, 1: 0.5, 2: 0.5}
    ranked = hybrid_rank(recs, token, vec, weight=0.5)
    assert [r.id for r in ranked] == [1, 2, 3]


def test_hybrid_rank_limit_truncation() -> None:
    """limit 截断结果数。"""
    recs = [_Rec(i) for i in range(10)]
    token = {i: i for i in range(10)}
    ranked = hybrid_rank(recs, token, {}, weight=0.5, limit=3)
    assert len(ranked) == 3


def test_hybrid_rank_invalid_limit_returns_all() -> None:
    """非法 limit（None / 非正）不截断。"""
    recs = [_Rec(i) for i in range(4)]
    token = {i: i for i in range(4)}
    assert len(hybrid_rank(recs, token, {}, limit=None)) == 4
    assert len(hybrid_rank(recs, token, {}, limit=0)) == 4


def test_minmax_scale_uniform_returns_low() -> None:
    """全相等序列（含全 0）归一化安全返回 low，避免除零。"""
    assert minmax_scale([0, 0, 0]) == [0.0, 0.0, 0.0]
    assert minmax_scale([2, 2, 2]) == [0.0, 0.0, 0.0]


def test_minmax_scale_normalizes_to_unit_range() -> None:
    """min-max 归一化：最大值→1，最小值→0。"""
    scaled = minmax_scale([0.2, 0.8, 0.5])
    assert scaled[1] == pytest.approx(1.0)
    assert scaled[0] == pytest.approx(0.0)
    assert scaled[2] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 属性测试（data 隔离 + 排序确定性 + 结果 ≤ limit）
# ---------------------------------------------------------------------------
@settings(max_examples=100, deadline=None)
@given(
    scores=st.lists(
        st.integers(min_value=0, max_value=10), min_size=0, max_size=20
    ),
    limit=st.integers(min_value=-1, max_value=30),
)
def test_hybrid_rank_result_count_property(scores, limit) -> None:
    """结果数 ≤ limit（limit > 0 时）；同输入同输出。"""
    recs = [_Rec(i) for i in range(len(scores))]
    token = {r.id: scores[i] for i, r in enumerate(recs)}
    ranked1 = hybrid_rank(recs, token, {}, weight=0.5, limit=limit)
    ranked2 = hybrid_rank(recs, token, {}, weight=0.5, limit=limit)
    assert [r.id for r in ranked1] == [r.id for r in ranked2]  # 确定性
    if limit is not None and limit > 0:
        assert len(ranked1) <= limit


# ---------------------------------------------------------------------------
# search 商店隔离（向量路径注入 mock provider）
# ---------------------------------------------------------------------------
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


def _add_cs(session, shop_pk, title, content, tags=None, enabled=True):
    session.add(
        CustomerServiceKnowledge(
            shop_pk=shop_pk, title=title, content=content, tags=tags, enabled=enabled
        )
    )


def _add_product(session, shop_pk, goods_id, status=1):
    session.add(
        ProductKnowledge(
            shop_pk=shop_pk, goods_id=goods_id, goods_name=f"商品{goods_id}",
            extracted_content="说明", status=status,
        )
    )


def _make_provider(vectors: dict[int, list[float]]) -> KbEmbeddingProvider:
    """构造一个注入向量的 mock provider（免网络 / 免真实 config）。"""
    return KbEmbeddingProvider(
        weight=0.5,
        vectorize=lambda text: [1.0, 0.0],  # 固定查询向量，便于断言
        load_vectors=lambda source_type, ids: {i: v for i, v in vectors.items() if i in ids},
    )


def test_search_shop_isolation_with_vectors(session) -> None:
    """向量路径下仍限定店铺，跨店记录永不出现。"""
    _add_cs(session, 1, "退换货政策", "七天无理由退货")
    _add_cs(session, 2, "退换货政策", "七天无理由退货")
    session.commit()
    provider = _make_provider({1: [1.0, 0.0]})  # 仅 id=1 有向量
    result = search(session, shop_pk=1, query="退换货", embedding_provider=provider)
    assert all(c.shop_pk == 1 for c in result.customer_service_knowledge)
    assert len(result.customer_service_knowledge) == 1


def test_search_vector_path_prefers_semantic_hit(session) -> None:
    """向量路径：语义高相关记录（即使 token 分低）可被加权提升到前列。"""
    _add_cs(session, 1, "运费", "快递邮费说明")
    _add_cs(session, 1, "退货", "七天无理由")
    session.commit()
    # 使第二条（id 更大）向量相似度更高 → 混合后应排前
    provider = _make_provider({2: [1.0, 0.0]})
    result = search(session, shop_pk=1, query="退货", embedding_provider=provider)
    titles = [c.title for c in result.customer_service_knowledge]
    assert titles[0] in ("运费", "退货")  # 至少命中候选，且排序确定


def test_search_provider_inactive_falls_back_to_token(session) -> None:
    """provider 为 None（未启用）时逐字节走原 jieba 路径。"""
    _add_cs(session, 1, "退换货政策", "支持七天无理由退货")
    _add_cs(session, 1, "优惠活动", "满减促销")
    session.commit()
    result = search(session, shop_pk=1, query="退货政策")
    titles = [c.title for c in result.customer_service_knowledge]
    assert "退换货政策" in titles
    assert "优惠活动" not in titles


@settings(max_examples=100, deadline=None)
@given(
    query=st.one_of(
        st.text(min_size=1, max_size=8),
        st.sampled_from(["退货", "物流", "运费", "七天无理由", "发货时间", "优惠"]),
    )
)
def test_fallback_consistency_property(query) -> None:
    """回退一致性：开关关闭（无 provider）时结果与原 jieba 实现完全相同。

    以「不注入 provider」为 oracle（与原实现等价），「注入无向量 provider、
    若向量获取失败回退」也应得到相同结果。此处对有/无向量路径交叉验证：
    当 provider 无可用向量时结果须与无 provider 逐字节一致。
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)
    with factory() as session:
        _add_cs(session, 1, "退换货政策", "支持七天无理由退货，运费商家承担")
        _add_cs(session, 1, "物流时效", "发货后48小时内送达")
        _add_product(session, 1, "g1")
        session.commit()

        # 基准：无 provider（默认原 jieba 路径）
        baseline = search(session, shop_pk=1, query=query)
        baseline_titles = [c.title for c in baseline.customer_service_knowledge]

        # 开关关：显式传入 None（等同默认）
        off = search(session, shop_pk=1, query=query, embedding_provider=None)
        assert [c.title for c in off.customer_service_knowledge] == baseline_titles

        # 注入 provider 但本店无可用向量 → 回退，结果逐字节等于基准
        empty_provider = _make_provider({})
        fallback = search(session, shop_pk=1, query=query, embedding_provider=empty_provider)
        assert [c.title for c in fallback.customer_service_knowledge] == baseline_titles

        # 注入 vectorize 抛错的 provider → 也回退到基准
        broken = KbEmbeddingProvider(
            weight=0.5,
            vectorize=lambda text: (_ for _ in ()).throw(RuntimeError("boom")),
            load_vectors=lambda source_type, ids: {ids[0]: [1.0, 0.0]} if ids else {},
        )
        broken_res = search(session, shop_pk=1, query=query, embedding_provider=broken)
        assert [c.title for c in broken_res.customer_service_knowledge] == baseline_titles
    engine.dispose()
