# -*- coding: utf-8 -*-
"""
common.models.config_models —— 配置与控制数据表模型
==================================================
本文件用途：定义「拼多多自动回复」系统配置与控制业务域的数据表结构模型，覆盖：
- business_hours        营业时间（起止时刻，支持跨午夜，未配置默认全天）
- message_filter_rule   消息过滤规则（条件类型 / 条件值 / 启停用）
- blacklist             黑名单（移出 = 逻辑失效）
- risk_rule             风控规则（单会话/单店铺频率上限与统计窗口）
- transfer_keyword      转人工关键词
- llm_config            LLM 配置（模型 / API 密钥密文 / 指令 / AI 开关）

关键约束（开发规范）：
- 规范 9：每张表均有自增 BIGINT 主键。
- 规范 10：shop_pk 等关联键均为普通列，无外键。
- 规范 17：审计时间字段统一北京时间。
- 敏感字段 ``api_key_enc``（API 密钥密文）以加密形式存储，对外脱敏（需求 8.6）。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import AuditMixin, Base


class BusinessHours(AuditMixin, Base):
    """营业时间表 business_hours。

    配置店铺营业起止时刻，支持跨午夜区间；未配置时业务侧默认全天营业
    （需求 11.x）。判定基于北京时间。
    """

    __tablename__ = "pdd_business_hours"
    __table_args__ = {"comment": "营业时间表（起止时刻 / 启停用，北京时间）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    start_time: Mapped[object | None] = mapped_column(
        Time, nullable=True, comment="营业开始时刻（北京时间）"
    )
    end_time: Mapped[object | None] = mapped_column(
        Time, nullable=True, comment="营业结束时刻（北京时间，可跨午夜）"
    )
    # 星期维度：逗号分隔的星期序号（1=周一 … 7=周日），如 "1,2,3,4,5" 表示周一至周五；
    # 空串 = 不限定星期 = 每天（兼容旧数据，旧数据零迁移负担）。需求 11.x 扩展。
    weekdays: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default="", server_default="",
        comment="营业星期维度（逗号分隔 1~7，空=每天；1=周一…7=周日）",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )


class MessageFilterRule(AuditMixin, Base):
    """消息过滤规则表 message_filter_rule。

    命中过滤规则的消息不进入自动回复（需求 12.2）。条件类型枚举入字典。
    """

    __tablename__ = "pdd_message_filter_rule"
    __table_args__ = {"comment": "消息过滤规则表（过滤条件 / 启停用）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    # 过滤条件类型：枚举入字典（如 contains/regex/msg_type 等）
    condition_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="过滤条件类型（枚举入字典）"
    )
    condition_value: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="过滤条件值"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )


class Blacklist(AuditMixin, Base):
    """黑名单表 blacklist。

    黑名单内客户不自动回复。移出黑名单通过 ``is_active`` 置为失效实现
    （逻辑删除，禁止物理删除，需求 12.5）。
    """

    __tablename__ = "pdd_blacklist"
    __table_args__ = {"comment": "黑名单表（屏蔽客户 / 逻辑失效）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    customer_uid: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="客户唯一标识 customer_uid"
    )
    # 是否有效：移出黑名单 = 置 False（逻辑失效，需求 12.5）
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否有效（移出=False，逻辑失效）"
    )


class RiskRule(AuditMixin, Base):
    """风控规则表 risk_rule。

    配置单会话 / 单店铺在统计窗口内的回复频率上限，达上限暂停并记风控日志
    （需求 13.2）。
    """

    __tablename__ = "pdd_risk_rule"
    __table_args__ = {"comment": "风控规则表（单会话 / 单店铺频率上限与统计窗口）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    session_reply_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="单会话窗口内回复次数上限"
    )
    shop_reply_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="单店铺窗口内回复次数上限"
    )
    window_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="统计窗口（秒）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )


class TransferKeyword(AuditMixin, Base):
    """转人工关键词表 transfer_keyword。

    命中转人工关键词时暂停自动回复并转人工（需求 16.3）。
    """

    __tablename__ = "pdd_transfer_keyword"
    __table_args__ = {"comment": "转人工关键词表（触发转人工的关键词配置）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    keyword: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="转人工关键词"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )


class LlmConfig(AuditMixin, Base):
    """LLM 配置表 llm_config。

    配置 AI 回复使用的模型、API 密钥（密文）、接口地址、指令与开关。
    ``shop_pk`` 为空时表示全局配置。敏感字段 ``api_key_enc`` 对外脱敏（需求 8.6）。
    """

    __tablename__ = "pdd_llm_config"
    __table_args__ = {"comment": "LLM 配置表（接口类型 / 模型 / 密钥密文 / 指令 / AI 开关）"}

    # 店铺主键，为空表示全局配置（普通列，无外键）
    shop_pk: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="关联店铺主键 shop.id；为空表示全局（无外键）"
    )
    # 接口类型（服务商协议）：openai_compatible/anthropic/gemini/dashscope_app
    # 枚举入 sys_dict（规范 15）。缺省 openai_compatible，兼容历史无此字段的配置。
    provider_type: Mapped[str] = mapped_column(
        String(32),
        default="openai_compatible",
        nullable=False,
        server_default="openai_compatible",
        comment="接口类型：openai_compatible/anthropic/gemini/dashscope_app",
    )
    model_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="模型名称"
    )
    # API 密钥密文：加密存储，对外脱敏
    api_key_enc: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="API 密钥密文（加密存储，对外脱敏）"
    )
    api_base: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="API 接口地址"
    )
    # 指令：JSON 文本
    instructions: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="模型指令（JSON 文本）"
    )
    ai_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否启用 AI 回复"
    )


__all__ = [
    "BusinessHours",
    "MessageFilterRule",
    "Blacklist",
    "RiskRule",
    "TransferKeyword",
    "LlmConfig",
]
