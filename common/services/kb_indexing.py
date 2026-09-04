# -*- coding: utf-8 -*-
"""
common.services.kb_indexing —— 知识库向量索引（best-effort 写路径）
====================================================================
本文件用途：为「拼多多自动回复」系统提供知识库内容变更后的 best-effort 向量
索引能力（POL-004）：
- ``index_knowledge_content``：对单条知识（商品 / 客服）在写入后尝试生成向量并
  upsert 到 ``KbEmbedding``；**失败绝不阻断知识库写操作**，仅记 warning 日志，
  留待回填 CLI（tools/kb_embedding/backfill.py）补建。
- 纯函数：``build_content_hash``（内容哈希）与 ``is_embedding_enabled``（店铺
  embedding_enabled 判定）与 I/O 分离，便于单测。

设计要点：
- **特性开关默认关**：店铺 ``embedding_enabled=False``（或缺 LLM 密钥）时直接
  跳过，对存量路径零行为变更。
- **解密与回退**：密钥经 ``try_decrypt_text`` 解密，api_base/api_key 缺省回退
  该店铺 chat 配置（``resolve_embedding_config``）。
- **保密**：异常 / 日志文本不含密钥，仅含端点描述。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from sqlalchemy.orm import Session

from common.db.repository import Repository
from common.models.config_models import LlmConfig
from common.models.knowledge_models import KbEmbedding
from common.services.embedding_service import (
    DEFAULT_EMBEDDING_TIMEOUT,
    embed_texts,
    encode_vector,
    resolve_embedding_config,
)
from common.utils.crypto import try_decrypt_text

logger = logging.getLogger("common.kb_indexing")


def build_content_hash(text: str) -> str:
    """构建知识内容哈希（SHA-256 前 64 位），用于失效检测（POL-004）。

    Args:
        text: 知识文本（标题 + 内容 + 标签）。

    Returns:
        64 位十六进制哈希串。
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:64]


def is_embedding_enabled(session: Session, shop_pk: int) -> bool:
    """判断店铺是否启用向量 embedding（POL-005 触发条件一）。

    Args:
        session: 会话。
        shop_pk: 店铺主键。

    Returns:
        店铺级 LlmConfig 存在且 embedding_enabled=True 时返回 True。
    """
    config = Repository(LlmConfig, session).get_by(shop_pk=shop_pk)
    return bool(config and config.embedding_enabled)


def _resolve_embedding_secret(session: Session, shop_pk: int) -> Optional[dict]:
    """解析店铺 embedding 请求参数（密钥解密 + 回退 chat 配置）。

    Args:
        session: 会话。
        shop_pk: 店铺主键。

    Returns:
        {"api_key", "api_base", "model"}；店铺未启用或密钥缺失返回 None。
    """
    config = Repository(LlmConfig, session).get_by(shop_pk=shop_pk)
    if config is None or not config.embedding_enabled:
        return None
    chat_key = try_decrypt_text(config.api_key_enc) or ""
    if not chat_key:
        return None
    resolved = resolve_embedding_config(
        model=config.embedding_model,
        chat_api_base=config.api_base,
        chat_api_key=chat_key,
    )
    if not resolved["api_key"]:
        return None
    return resolved


def index_knowledge_content(
    session: Session,
    *,
    shop_pk: int,
    source_type: str,
    source_id: int,
    content_text: str,
) -> bool:
    """对单条知识内容 best-effort 生成向量并 upsert 到 KbEmbedding。

    全链路 try/except 包裹：任何失败（网络 / 超时 / 配置）仅记 warning 日志并
    返回 False，绝不抛给上层 / 阻断知识库写操作。重复调用对相同 content 幂等
    （content_hash 相同则命中 upsert 更新）。

    Args:
        session: 会话。
        shop_pk: 店铺主键。
        source_type: "product_knowledge" / "cs_knowledge"。
        source_id: 知识条目主键 id。
        content_text: 待向量化知识文本（标题 + 内容 + 标签）。

    Returns:
        True=已生成 / 更新向量；False=跳过或失败（embedding 未启用 / 缺密钥 /
        网络失败 / 向量化异常）。
    """
    try:
        if not is_embedding_enabled(session, shop_pk):
            return False
        resolved = _resolve_embedding_secret(session, shop_pk)
        if not resolved:
            return False
        # 空文本：不生成向量（无向量语义，由检索层按缺向量回退）。
        if not content_text or not content_text.strip():
            return False
        vectors = embed_texts(
            [content_text],
            api_key=resolved["api_key"],
            api_base=resolved["api_base"],
            model=resolved["model"],
            timeout=DEFAULT_EMBEDDING_TIMEOUT,
        )
        if not vectors:
            return False
        content_hash = build_content_hash(content_text)
        Repository(KbEmbedding, session).upsert(
            {"shop_pk": shop_pk, "source_type": source_type, "source_id": source_id},
            {
                "content_hash": content_hash,
                "model_name": resolved["model"],
                "vector_json": encode_vector(vectors[0]),
                "enabled": True,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort：失败仅记日志不阻断写操作
        # 异常文本不含密钥（embed_texts 已保证端点描述无 key），此处仅加固。
        logger.warning(
            "知识向量索引失败（source_type=%s source_id=%s）：%s，交由回填 CLI 补建",
            source_type, source_id, exc,
        )
        return False


__all__ = [
    "build_content_hash",
    "is_embedding_enabled",
    "index_knowledge_content",
]
