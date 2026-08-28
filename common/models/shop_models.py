# -*- coding: utf-8 -*-
"""
common.models.shop_models —— 渠道、店铺与账号凭据数据表模型
==========================================================
本文件用途：定义「拼多多自动回复」系统渠道 / 店铺 / 账号凭据业务域的数据表
结构模型，覆盖：
- channel  渠道（如 pinduoduo）
- shop     店铺（拼多多店铺，归属用户，启停用）
- account  账号凭据（账号密码加密、Cookie 加密、登录态）

关键约束（开发规范）：
- 规范 9：每张表均有自增 BIGINT 主键。
- 规范 10：channel_id / owner_user_id / shop_pk / user_id 等均为普通列，无外键。
- 规范 17：审计时间字段统一北京时间。
- 敏感字段 ``password_enc``（密码密文）、``cookies_enc``（Cookie 密文）按设计命名，
  以加密形式存储，对外响应须脱敏，列表不返回明文（需求 3.6）。

逻辑约束（代码层维护，见注释）：
- shop：同 ``owner_user_id`` + ``shop_id`` 逻辑唯一（upsert 幂等，需求 3.2）。
  其中 shop_id 为拼多多店铺业务标识，shop 主键 id 即设计中所称 shop_pk。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import AuditMixin, Base


class Channel(AuditMixin, Base):
    """渠道表 channel。

    描述对接的平台渠道（当前为 pinduoduo），供店铺归属与扩展多渠道使用。
    """

    __tablename__ = "pdd_channel"
    __table_args__ = {"comment": "渠道表（对接平台渠道，如 pinduoduo）"}

    channel_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="渠道名称（如 pinduoduo）"
    )
    description: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="渠道描述"
    )


class Shop(AuditMixin, Base):
    """店铺表 shop。

    存储拼多多店铺信息与归属用户、启停用状态。本表主键 ``id`` 即设计中
    其它表引用的 ``shop_pk``；``shop_id`` 为拼多多店铺业务标识。

    逻辑唯一约束：同 ``owner_user_id`` + ``shop_id`` 唯一（代码层 upsert 幂等
    校验，需求 3.2），不依赖数据库外键 / 唯一约束强制。
    """

    __tablename__ = "pdd_shop"
    __table_args__ = (
        # 逻辑唯一：同一归属用户下店铺业务标识唯一（需求 3.2 upsert 幂等的库层保障）。
        # 启动自检迁移器会在无重复数据时自动补建该唯一索引（有重复则跳过告警，不删数据）。
        UniqueConstraint("owner_user_id", "shop_id", name="uix_pdd_shop_owner_shopid"),
        {"comment": "店铺表（拼多多店铺信息 / 归属用户 / 启停用）"},
    )

    # 渠道 id：普通列，关系由代码维护（无外键）
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="所属渠道 ID（普通列，无外键）"
    )
    # 所属平台：pdd=拼多多 / tiktok=TikTok Shop（需求 24.x 多平台分派前置）。
    # 存量店铺经启动迁移自动归 'pdd'，新接入平台经 TIK-002~TIK-017 逐步打通；
    # 枚举值入 sys_dict（dict_type='platform'），前端据字典渲染中文与徽标。
    platform: Mapped[str] = mapped_column(
        String(32),
        default="pdd",
        nullable=False,
        comment="所属平台：pdd=拼多多 / tiktok=TikTok Shop（字典 platform）",
    )
    # 出口代理服务器地址（Phase 2 前置，店铺级）：如 ``http://host:port`` /
    # ``socks5://host:port``；空（None）= 不走代理（默认关闭）。仅 TikTok 通道
    # 建浏览器会话时消费（BrowserSession.launch 参数 proxy），PDD 通道不使用；
    # 经启动自检迁移器幂等补列，存量店铺为空不受影响。
    proxy_server: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="出口代理服务器地址（如 http://host:port，空=不走代理，店铺级）",
    )
    # 登录态浏览器用户数据目录（TikTok 通道登录期实际使用目录，建店登录后落库，
    # connect 时复用，保证「登录一次、免登复用」；PDD 通道不使用；经启动自检
    # 迁移器幂等补列，存量店铺为空，缺省按 shop_pk 推导目录，行为不变）。
    browser_data_dir: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="TikTok 登录态浏览器用户数据目录（登录后落库，connect 复用）",
    )
    # 拼多多店铺业务标识（非主键，业务键的一部分）
    shop_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="拼多多店铺业务标识（业务键，非主键）"
    )
    shop_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="店铺名称"
    )
    shop_logo: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="店铺 Logo URL"
    )
    # 店铺归属用户 id：数据范围隔离与业务键的一部分（无外键）
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="归属用户 ID（业务键一部分，无外键）"
    )
    remark: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="备注"
    )
    # 启停用状态：1=启用，0=停用（停用即逻辑删除并断开连接，需求 3.5）
    status: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="状态：1=启用，0=停用（逻辑删除）"
    )


class Account(AuditMixin, Base):
    """账号凭据表 account。

    存储店铺登录账号、加密后的密码与 Cookie、以及登录态。敏感字段以密文存储，
    对外响应须脱敏（需求 3.6 / 8.6）。
    """

    __tablename__ = "pdd_account"
    __table_args__ = {"comment": "账号凭据表（登录账号 / 密码密文 / Cookie 密文 / 登录态）"}

    # 关联店铺主键（即 shop_pk）：普通列，无外键
    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    # 归属用户 id：普通列，无外键
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="归属用户 ID（普通列，无外键）"
    )
    username: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="登录账号（拼多多账号）"
    )
    # 密码密文：加密存储，绝不存明文，对外脱敏
    password_enc: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="密码密文（加密存储，对外脱敏）"
    )
    # Cookie 密文：加密存储，列表不返回明文（需求 3.6）
    cookies_enc: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Cookie 密文（加密存储，对外脱敏）"
    )
    # 登录态：未验证/在线/离线/需重新登录，枚举入 sys_dict（需求 5.7）
    login_state: Mapped[str] = mapped_column(
        String(32),
        default="unverified",
        nullable=False,
        comment="登录态：unverified/online/offline/relogin（枚举入字典）",
    )


__all__ = [
    "Channel",
    "Shop",
    "Account",
]
