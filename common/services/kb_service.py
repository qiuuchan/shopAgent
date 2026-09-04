# -*- coding: utf-8 -*-
"""
common.services.kb_service —— 知识库检索服务（kb.search）
==========================================================
本文件用途：实现「拼多多自动回复」系统的知识库检索逻辑 ``kb.search``，置于
common 公共库供 websocket 服务（AI 回复引擎工具 get_product_knowledge /
search_customer_service_knowledge）复用（设计「关键接口签名」：
``kb.search(shop_id, query?, goods_id?, limit) -> {product_knowledge,
customer_service_knowledge}``）。

检索约束（需求 9.4 / 10.3 / 10.4 / 10.5）：
- 限定店铺：仅检索给定店铺（shop_pk）下的知识，跨店铺数据不返回（数据隔离）；
- 客服知识仅启用：仅返回 ``enabled=True`` 的客服知识，停用记录检索时不返回
  （需求 10.5）；商品知识同理仅返回 ``status=1`` 的启用记录（停用=逻辑删除）；
- 结果不超 limit：商品知识与客服知识各自返回数量均不超过 ``limit``（需求 10.3
  的「结果数量不超过配置上限」）；
- 按 goods_id 精确匹配：提供 ``goods_id`` 时商品知识按其精确匹配（需求 9.4）；
- 按标签筛选：提供 ``tags`` 时客服知识按标签（逗号分隔）筛选（需求 10.4）；
- jieba 分词：提供 ``query`` 时对其中文分词，命中标题 / 内容 / 标签任一分词
  的启用客服知识方为匹配，并按命中分词数量降序排序（需求 10.3）。

设计说明：
- 所有数据访问经 ``common.db.repository.Repository``（SQLAlchemy 参数化查询，
  规范 16），本文件不书写任何原生 SQL；
- 「店铺 + 启用 + goods_id 精确」等等值条件下推数据库过滤；分词命中与标签
  筛选在内存中对「本店铺启用集合」二次过滤（单店铺知识量有限，开销可控）；
- 参数 ``shop_pk`` 对应设计签名中的 ``shop_id``，即店铺主键 shop.id（知识表
  以 ``shop_pk`` 列关联店铺，无外键，规范 10）。

POL-005 向量混合检索（对存量路径零行为变更）：
- 触发条件（三者同时满足）才启用：传入 ``query``；店铺 ``embedding_enabled=True``；
  本店启用记录存在可用向量。任一不满足 → 逐字节走原 jieba 路径（默认行为不变）。
- 向量路径：``embedding_provider.vectorize(query)`` 取查询向量（失败 → 记日志并
  回退 jieba 路径），对本店启用记录算余弦；token 命中数与余弦各自 min-max
  归一化后经 ``kb_hybrid.hybrid_rank`` 加权合并排序（权重默认 0.5，可配）。
- ``goods_id`` 精确匹配路径不动；``websocket/agent/tools.py`` 零改动（检索升级
  对 Agent 循环透明）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import jieba
from sqlalchemy.orm import Session

from common.db.repository import Repository
from common.models.config_models import LlmConfig
from common.models.knowledge_models import (
    CustomerServiceKnowledge,
    KbEmbedding,
    ProductKnowledge,
)
from common.services.embedding_service import (
    DEFAULT_EMBEDDING_TIMEOUT,
    EmbeddingError,
    cosine_similarity,
    decode_vector,
    embed_texts,
    resolve_embedding_config,
)
from common.services.kb_hybrid import DEFAULT_WEIGHT, hybrid_rank
from common.utils.crypto import try_decrypt_text

# 检索结果默认上限：未显式传入 limit 时使用（需求 10.3 配置上限的兜底默认）。
DEFAULT_SEARCH_LIMIT: int = 10

logger = logging.getLogger("common.kb_service")


@dataclass
class KbSearchResult:
    """知识检索结果数据结构。

    对应设计签名返回值 ``{product_knowledge, customer_service_knowledge}``：
    - ``product_knowledge``：命中的商品知识记录列表（启用，按 goods_id 精确匹配）；
    - ``customer_service_knowledge``：命中的客服知识记录列表（仅启用，
      经分词 / 标签筛选，按相关度排序）。
    两个列表长度均不超过检索 ``limit``。
    """

    product_knowledge: list[ProductKnowledge] = field(default_factory=list)
    customer_service_knowledge: list[CustomerServiceKnowledge] = field(
        default_factory=list
    )


class KbEmbeddingProvider:
    """向量混合检索提供者（POL-005）：封装配置探测、查询向量与存储向量读取。

    设计为使 ``search`` 可注入测试替身（免网络 / 免真实配置）：正常经
    ``from_config`` 依据店铺 LlmConfig 构造；测试可传自定义 ``vectorize`` 与
    ``load_vectors`` 回调。

    Attributes:
        weight: 归一化后 token 分权重（∈[0,1]，默认 0.5，其余给余弦分）。
        vectorize: 文本 -> 查询向量（真实实现经 ``embed_texts``；可注入 mock）。
        load_vectors: (source_type, source_ids) -> {source_id: 向量}，读取本店
            启用知识条目的存储向量；可注入 mock 以规避真实库查询。
    """

    def __init__(
        self,
        *,
        weight: float = DEFAULT_WEIGHT,
        vectorize: Callable[[str], list[float]],
        load_vectors: Callable[[str, list[int]], dict[int, list[float]]],
    ) -> None:
        self.weight = weight
        self._vectorize = vectorize
        self._load_vectors = load_vectors

    def vectorize(self, text: str) -> list[float]:
        """返回查询向量（单条文本）。"""
        return self._vectorize(text)

    def load_vectors(self, source_type: str, source_ids: list[int]) -> dict[int, list[float]]:
        """返回 {source_id: 向量} 映射；source_ids 为空时返回空 dict。"""
        if not source_ids:
            return {}
        return self._load_vectors(source_type, source_ids)


def _build_embedding_provider(
    session: Session, shop_pk: int
) -> Optional[KbEmbeddingProvider]:
    """依据店铺 LlmConfig 构造默认向量提供者；未启用或缺密钥时返回 None。

    满足 POL-005 触发条件之一「店铺 embedding_enabled=True」。密钥经
    ``common.utils.crypto.try_decrypt_text`` 解密（同 ``agent_config`` 口径），
    api_base / api_key 缺省回退该店铺 chat 配置（``resolve_embedding_config``）。

    Args:
        session: 当前会话。
        shop_pk: 店铺主键。

    Returns:
        构造好的提供者；店铺未启用 embedding，或 LLM 配置缺密钥时返回 None
        （调用方据此回退 jieba 路径，POL-005 零行为变更）。
    """
    config_repo: Repository[LlmConfig] = Repository(LlmConfig, session)
    config = config_repo.get_by(shop_pk=shop_pk)
    if config is None or not config.embedding_enabled:
        return None
    # 解密 chat 密钥作为 embedding 回退密钥（embedding 无独立 api_key 列时）。
    chat_api_key = try_decrypt_text(config.api_key_enc) or ""
    if not chat_api_key:
        return None
    resolved = resolve_embedding_config(
        model=config.embedding_model,
        chat_api_base=config.api_base,
        chat_api_key=chat_api_key,
    )
    embedding_api_key = resolved["api_key"]
    embedding_api_base = resolved["api_base"]
    embedding_model = resolved["model"]
    embedding_timeout = DEFAULT_EMBEDDING_TIMEOUT

    def _vectorize(text: str) -> list[float]:
        """对单条文本取查询向量；空文本返回空列表。"""
        texts = [text]
        vectors = embed_texts(
            texts,
            api_key=embedding_api_key,
            api_base=embedding_api_base,
            model=embedding_model,
            timeout=embedding_timeout,
        )
        return vectors[0] if vectors else []

    embed_repo: Repository[KbEmbedding] = Repository(KbEmbedding, session)

    def _load_vectors(source_type: str, source_ids: list[int]) -> dict[int, list[float]]:
        """读取本店启用知识条目的存储向量，返回 {source_id: 向量}。"""
        if not source_ids:
            return {}
        rows = embed_repo.list(
            filters={"shop_pk": shop_pk, "source_type": source_type},
            order_by=False,
        )
        wanted = set(source_ids)
        out: dict[int, list[float]] = {}
        for row in rows:
            if not row.enabled:
                continue
            vec = decode_vector(row.vector_json)
            if vec and getattr(row, "source_id", None) in wanted:
                out[row.source_id] = vec
        return out

    return KbEmbeddingProvider(
        weight=DEFAULT_WEIGHT,
        vectorize=_vectorize,
        load_vectors=_load_vectors,
    )


def _normalize_limit(limit: int | None) -> int:
    """规整检索条数上限为正整数；非法 / 缺省时回退默认值。

    Args:
        limit: 调用方传入的结果上限（可能为 None 或非正数）。

    Returns:
        合法的正整数上限。
    """
    if limit is None:
        return DEFAULT_SEARCH_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_SEARCH_LIMIT
    return value if value > 0 else DEFAULT_SEARCH_LIMIT


def _tokenize(query: str) -> list[str]:
    """对查询文本进行 jieba 中文分词，返回去重去空白的分词列表。

    Args:
        query: 原始查询文本。

    Returns:
        小写化、去空白、去重后的分词列表（保持首次出现顺序）；空查询返回空列表。
    """
    if not query or not query.strip():
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    # jieba.lcut 返回分词列表；过滤纯空白分词，统一小写以便不区分大小写匹配
    for raw in jieba.lcut(query):
        token = raw.strip().lower()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _split_tags(tags: str | None) -> list[str]:
    """将逗号分隔的标签字符串拆分为去空白、非空的标签列表（小写）。

    Args:
        tags: 逗号分隔的标签字符串（可能为 None / 空）。

    Returns:
        标签列表；无有效标签返回空列表。
    """
    if not tags:
        return []
    return [t.strip().lower() for t in tags.split(",") if t.strip()]


def _match_token_count(record: CustomerServiceKnowledge, tokens: list[str]) -> int:
    """统计客服知识记录命中的分词数量（标题 / 内容 / 标签任一包含即计 1）。

    Args:
        record: 客服知识记录。
        tokens: 查询分词列表（已小写）。

    Returns:
        命中的分词数量（用于相关度排序）。
    """
    # 拼接可检索文本（标题 + 内容 + 标签），统一小写后做包含匹配
    haystack = " ".join(
        part.lower()
        for part in (record.title, record.content, record.tags)
        if part
    )
    return sum(1 for token in tokens if token in haystack)


def _filter_by_tags(
    record: CustomerServiceKnowledge, want_tags: list[str]
) -> bool:
    """判断客服知识记录是否命中任一指定标签（需求 10.4）。

    Args:
        record: 客服知识记录。
        want_tags: 期望筛选的标签列表（已小写）。

    Returns:
        记录标签与期望标签存在交集时返回 True。
    """
    record_tags = set(_split_tags(record.tags))
    return any(tag in record_tags for tag in want_tags)


def _rank_cs_by_tokens_or_hybrid(
    cs_enabled: list[CustomerServiceKnowledge],
    tokens: list[str],
    max_count: int,
    *,
    query: str | None,
    shop_pk: int,
    session: Session,
    provider: Optional[KbEmbeddingProvider],
    weight: float,
) -> list[CustomerServiceKnowledge]:
    """对启用客服知识排序：默认走原 jieba 命中路径，满足条件时走向量混合。

    POL-005 触发条件（三者同时满足）：传入 ``query``（由 tokens 非空体现）；
    店铺 ``embedding_enabled=True``（provider 有效体现）；本店启用记录存在可用
    向量。任一不满足或向量获取失败，均回退逐字节原 jieba 路径（零行为变更）。

    Args:
        cs_enabled: 本店启用客服知识记录列表。
        tokens: 查询词元（非空表示提供 query）。
        max_count: 结果上限。
        query: 原始查询文本（向量化用；缺失时只走 jieba 路径）。
        shop_pk: 店铺主键（供自动探测 provider 使用）。
        session: 会话（provider 缺省时用于读取配置）。
        provider: 注入或自动探测的向量提供者；None 表示未启用 → 走 jieba 路径。
        weight: 归一化后 token 分权重。

    Returns:
        排序后的记录列表（长度 ≤ max_count）。
    """
    token_scores: dict[int, int] = {
        getattr(rec, "id", 0): _match_token_count(rec, tokens) for rec in cs_enabled
    }
    # 构造候选向量分（键为记录 id；无向量记录缺省视为 0）。
    vector_scores: dict[int, float] = {}

    effective_provider = provider
    if effective_provider is None:
        # 测试注入之外，生产按 config 自动探测（店铺 embedding_enabled=True）。
        try:
            effective_provider = _build_embedding_provider(session, shop_pk)
        except Exception:  # noqa: BLE001 - 探测失败回退 jieba，绝不影响检索
            logger.warning("kb_service：探测 embedding provider 失败，回退 jieba 路径")
            effective_provider = None

    if effective_provider is not None and query:
        source_ids = [getattr(rec, "id", 0) for rec in cs_enabled]
        try:
            stored = effective_provider.load_vectors("cs_knowledge", source_ids)
        except Exception:  # noqa: BLE001 - 读取向量失败回退 jieba
            stored = {}
        if stored:
            # 取查询向量：失败（网络 / 超时）仅记 warning 并回退 jieba。
            try:
                query_vec = effective_provider.vectorize(query)
            except Exception:  # noqa: BLE001
                logger.warning("kb_service：查询向量化失败，回退 jieba 路径")
                query_vec = []
            if query_vec:
                for rec_id, vec in stored.items():
                    vector_scores[rec_id] = cosine_similarity(query_vec, vec)
                # 记录命中了 token 或存在非零余弦的，进入混合排序候选集。
                candidates = [
                    rec for rec in cs_enabled
                    if token_scores.get(getattr(rec, "id", 0), 0) > 0
                    or vector_scores.get(getattr(rec, "id", 0), 0) > 0
                ]
                if candidates:
                    return hybrid_rank(
                        candidates, token_scores, vector_scores, weight, limit=max_count
                    )

    # 回退原 jieba 路径：命中分词 > 0，按命中数降序、id 升序（逐字节一致）。
    scored = [(rec, token_scores.get(getattr(rec, "id", 0), 0)) for rec in cs_enabled]
    scored = [(rec, score) for rec, score in scored if score > 0]
    scored.sort(key=lambda pair: (-pair[1], getattr(pair[0], "id", 0)))
    return [rec for rec, _ in scored]


def search(
    session: Session,
    shop_pk: int,
    query: str | None = None,
    goods_id: str | None = None,
    limit: int | None = DEFAULT_SEARCH_LIMIT,
    tags: list[str] | str | None = None,
    embedding_provider: Optional[KbEmbeddingProvider] = None,
    hybrid_weight: float = DEFAULT_WEIGHT,
) -> KbSearchResult:
    """知识库检索：限定店铺，返回商品知识与客服知识匹配结果。

    对应设计签名 ``kb.search(shop_id, query?, goods_id?, limit)``，供 AI 回复
    引擎的工具调用（商品知识 / 客服知识检索）复用。检索严格满足：
    限定店铺、客服知识仅启用、结果不超 limit、按 goods_id 精确匹配商品知识、
    按标签筛选客服知识、对 query 进行 jieba 分词匹配（需求 9.4 / 10.3 / 10.4 /
    10.5）。

    POL-005 向量混合检索（默认零行为变更）：仅当「传入 query + 店铺
    ``embedding_enabled=True`` + 本店启用记录存在可用向量」三者同时满足才启用
    向量路径；任一不满足或向量获取失败均回退逐字节原 jieba 路径。

    Args:
        session: 当前事务性会话（生命周期由外层管理）。
        shop_pk: 店铺主键 shop.id（对应设计签名中的 shop_id），限定检索范围。
        query: 客服知识检索查询文本；提供时按 jieba 分词命中标题 / 内容 / 标签。
        goods_id: 商品业务标识；提供时商品知识按其精确匹配。
        limit: 结果上限；商品知识与客服知识各自不超过该值，非法时回退默认值。
        tags: 客服知识标签筛选条件（列表或逗号分隔字符串）；提供时按标签筛选。
        embedding_provider: 可选注入的向量提供者；缺省时按 config 自动探测
            （供测试注入 mock 规避网络）。
        hybrid_weight: 归一化后 token 分权重（∈[0,1]，默认 0.5）；仅向量路径生效。

    Returns:
        ``KbSearchResult``，含 product_knowledge 与 customer_service_knowledge
        两个列表，长度均不超过 limit。
    """
    max_count = _normalize_limit(limit)

    # ------------------------------------------------------------------
    # 商品知识检索：限定店铺 + 仅启用（status=1）+ goods_id 精确匹配（可选）
    # ------------------------------------------------------------------
    product_filters: dict[str, object] = {"shop_pk": shop_pk, "status": 1}
    if goods_id is not None and str(goods_id).strip() != "":
        product_filters["goods_id"] = goods_id
    product_repo: Repository[ProductKnowledge] = Repository(ProductKnowledge, session)
    product_records = product_repo.list(filters=product_filters, limit=max_count)

    # ------------------------------------------------------------------
    # 客服知识检索：限定店铺 + 仅启用（enabled=True），再按分词 / 标签二次筛选
    # ------------------------------------------------------------------
    cs_repo: Repository[CustomerServiceKnowledge] = Repository(
        CustomerServiceKnowledge, session
    )
    cs_enabled = cs_repo.list(
        filters={"shop_pk": shop_pk, "enabled": True},
        order_by=False,
    )

    want_tags = _split_tags(tags) if isinstance(tags, str) else [
        t.strip().lower() for t in (tags or []) if str(t).strip()
    ]
    tokens = _tokenize(query) if query else []

    cs_matched: list[CustomerServiceKnowledge]
    if tokens:
        cs_matched = _rank_cs_by_tokens_or_hybrid(
            cs_enabled,
            tokens,
            max_count,
            query=query,
            shop_pk=shop_pk,
            session=session,
            provider=embedding_provider,
            weight=hybrid_weight,
        )
    else:
        # 未提供查询：返回启用记录（保持稳定顺序），供按标签筛选或全量取前 N
        cs_matched = list(cs_enabled)

    # 标签筛选（需求 10.4）：提供 tags 时仅保留命中任一标签的记录
    if want_tags:
        cs_matched = [rec for rec in cs_matched if _filter_by_tags(rec, want_tags)]

    # 结果不超 limit（需求 10.3）
    cs_matched = cs_matched[:max_count]

    return KbSearchResult(
        product_knowledge=product_records,
        customer_service_knowledge=cs_matched,
    )


__all__ = [
    "KbSearchResult",
    "DEFAULT_SEARCH_LIMIT",
    "search",
]
