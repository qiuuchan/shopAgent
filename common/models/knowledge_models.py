# -*- coding: utf-8 -*-
"""
common.models.knowledge_models —— 知识库与商品数据表模型
========================================================
本文件用途：定义「拼多多自动回复」系统知识库与商品业务域的数据表结构模型，
覆盖：
- product_knowledge          商品知识（含价格/规格/抽取内容，供 AI 检索）
- customer_service_knowledge 客服知识（标题/内容/标签，可启停用，批量导入去重）
- product                    商品（同步入库的商品列表）

关键约束（开发规范）：
- 规范 9：每张表均有自增 BIGINT 主键。
- 规范 10：shop_pk / goods_id 等均为普通列，无外键。
- 规范 17：审计时间字段统一北京时间；last_extracted_at 亦为北京时间。

逻辑约束（代码层维护，见注释）：
- product_knowledge：``shop_pk`` + ``goods_id`` 逻辑唯一（upsert 幂等，需求 9.2）。
- product：``shop_pk`` + ``goods_id`` 逻辑唯一（upsert 幂等，需求 15.4）。
- customer_service_knowledge：批量导入按 ``shop_pk`` + ``title`` + ``content``
  去重（需求 10.2）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import AuditMixin, Base


class ProductKnowledge(AuditMixin, Base):
    """商品知识表 product_knowledge。

    存储商品的结构化与抽取知识，供 AI 回复检索（kb.search）。
    逻辑唯一：``shop_pk`` + ``goods_id``（代码层 upsert 幂等校验，需求 9.2）。
    """

    __tablename__ = "pdd_product_knowledge"
    __table_args__ = {"comment": "商品知识库表（按 goods_id 维护商品问答知识）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（业务键一部分，无外键）"
    )
    goods_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="拼多多商品业务标识（业务键一部分，无外键）"
    )
    goods_name: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="商品名称"
    )
    # 价格相关：使用 Numeric 避免浮点误差
    price: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True, comment="价格"
    )
    price_min: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True, comment="最低价"
    )
    price_max: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True, comment="最高价"
    )
    sold_quantity: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="已售数量"
    )
    thumb_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="商品缩略图 URL"
    )
    # 规格：JSON 文本存储
    specifications: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="商品规格（JSON 文本）"
    )
    # 抽取内容：用于 AI 知识检索的文本
    extracted_content: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="抽取内容（供 AI 检索）"
    )
    # 最近一次抽取时间（北京时间）
    last_extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近抽取时间（北京时间）"
    )
    status: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="状态：1=启用，0=停用（逻辑删除）"
    )


class CustomerServiceKnowledge(AuditMixin, Base):
    """客服知识表 customer_service_knowledge。

    存储客服问答知识，供 AI 检索（仅启用项参与检索）。批量导入时按
    ``shop_pk`` + ``title`` + ``content`` 去重（需求 10.2）。
    """

    __tablename__ = "pdd_customer_service_knowledge"
    __table_args__ = {"comment": "客服知识库表（问答标题 / 内容 / 启停用）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（去重键一部分，无外键）"
    )
    title: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="知识标题（去重键一部分）"
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="知识内容（去重键一部分）"
    )
    # 标签：逗号分隔，供按标签筛选检索（需求 10.5）
    tags: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="标签（逗号分隔）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用（仅启用参与检索）"
    )


class KbEmbedding(AuditMixin, Base):
    """知识库向量表 pdd_kb_embedding。

    存储知识条目的文本 embedding 向量（POL-003），供向量混合检索
    （POL-005）使用：按 ``shop_pk`` + ``source_type`` + ``source_id`` 唯一关联
    一条知识（商品知识 / 客服知识），``content_hash`` 用于内容失效检测，
    ``vector_json`` 存 JSON 序列化的浮点向量（TEXT 列，对齐
    ``ProductKnowledge.specifications`` 先例）。

    逻辑唯一：``shop_pk`` + ``source_type`` + ``source_id``（唯一索引，
    与代码层 upsert 幂等双重保证，对齐 pdd_product 表 uix 先例）；无外键（规范 10）。
    """

    __tablename__ = "pdd_kb_embedding"
    __table_args__ = (
        UniqueConstraint(
            "shop_pk", "source_type", "source_id", name="uix_kb_embedding_source"
        ),
        {"comment": "知识库向量表（按知识条目存储文本 embedding 向量）"},
    )

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（业务键一部分，无外键）"
    )
    # 知识来源类型：product_knowledge（商品知识）/ cs_knowledge（客服知识）
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="知识来源类型：product_knowledge / cs_knowledge"
    )
    source_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="知识条目主键 id（业务键一部分，无外键）"
    )
    # 内容哈希：内容变更时变化，用于回填 CLI 检测失效条目（POL-004）
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="知识内容哈希（内容变更时变化，用于失效检测）"
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="生成该向量的嵌入模型名"
    )
    # 向量：JSON 序列化的浮点数组（TEXT 列，与 specifications JSON 文本先例一致）
    vector_json: Mapped[str] = mapped_column(
        Text, nullable=False, comment="embedding 向量（JSON 数组文本）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用（停用不参与检索）"
    )


class Product(AuditMixin, Base):
    """商品表 product。

    存储从拼多多同步入库的商品列表。
    逻辑唯一：``shop_pk`` + ``goods_id``（代码层 upsert 幂等校验，需求 15.4）。
    """

    __tablename__ = "pdd_product"
    __table_args__ = (
        # shop_pk + goods_id 唯一索引：与代码层 upsert 幂等（需求 15.4）双重保证，
        # 防并发同步竞态产生重复商品记录。无外键（规范 10），仅唯一约束。
        UniqueConstraint("shop_pk", "goods_id", name="uix_pdd_product_shop_goods"),
        {"comment": "商品表（拼多多店铺商品信息）"},
    )

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（业务键一部分，无外键）"
    )
    goods_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="拼多多商品业务标识（业务键一部分，无外键）"
    )
    goods_name: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="商品名称"
    )
    price: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True, comment="价格"
    )
    sold_quantity: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="已售数量"
    )
    thumb_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="商品缩略图 URL"
    )
    # 规格：JSON 文本存储（同步商品详情时拉取并落库，供列表 / 详情展示，需求 15）
    specifications: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="商品规格（JSON 文本，来自拼多多商品详情）"
    )
    status: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="状态：1=启用，0=停用（逻辑删除）"
    )


__all__ = [
    "ProductKnowledge",
    "CustomerServiceKnowledge",
    "KbEmbedding",
    "Product",
]
