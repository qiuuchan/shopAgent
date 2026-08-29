# -*- coding: utf-8 -*-
"""
channel_pdd.connection_manager —— 连接 + 消费器装配（端到端串联收口）
====================================================================
本文件用途：把「收消息（PDDChannel） → 解析入队 → 消费器处理链（决策/AI/降级/
转人工/记日志/通知）」装配为一条可启动的端到端链路（任务 19.2），并在
``connection_registry`` 中登记，供 backend 断连接口与连接状态查询统一定位。

串联方式：
- 为每个店铺构造一个 ``PDDChannel``，并将其 ``message_handler`` 绑定到本店铺的
  ``MessageConsumer.consume_raw``：PDDChannel 收到原始报文写入 FIFO 队列后即触发
  消费器消费，完成解析 → 决策链 → 知识库/AI → 发送回复/卡片降级 → 记日志/通知。
- ``start_channel`` 启动连接并登记到 ``connection_registry``；``stop_channel`` 停止
  并注销；断连复用 ``connection_registry.disconnect``（需求 3.5）。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、单文件 ≤500 行（35）、
文件名用下划线（40）、全中文（50）、复用既有组件（52）。
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Dict, Optional, Set, Tuple

from channel_base import ALLOWED_PLATFORMS, PLATFORM_PDD, PLATFORM_TIKTOK
from channel_base import ChannelAdapter, is_allowed_platform
from channel_pdd import connection_registry
from channel_pdd.message_queue import message_queue_manager
from channel_pdd.pdd_channel import PDDChannel
from channel_tiktok.browser_session import BrowserSession
from channel_tiktok.tiktok_channel import TikTokChannel
from channel_tiktok.tiktok_message import parse_tiktok_raw
from channel_tiktok.tiktok_sender import TikTokSender
from common.core.config import get_settings
from common.db.repository import Repository
from common.db.session import session_scope
from common.models.shop_models import Shop
from engine.alert_dedup import build_alert_notifier, get_alert_dedup
from engine.alert_forwarder import backend_alert_send_cb
from engine.message_consumer import MessageConsumer, build_notifier

logger = logging.getLogger("channel_pdd.connection_manager")

# 店铺启用状态值（与 common.models.shop_models.Shop.status 约定一致：1=启用）。
_SHOP_STATUS_ENABLED: int = 1

# 连接断开告警事件类型（与 alert_dedup 去重维度一致，规范 52 复用常量语义）。
_EVENT_CONNECTION_DISCONNECTED: str = "connection_disconnected"

# TikTok 活跃通道登记键集合（(shop_id, user_id)），用于限制并发浏览器实例数
# （TIKTOK_MAX_BROWSER_INSTANCES，TIK-017）。停止路径不感知平台（registry.disconnect
# 由 routes 直接调用），故采用「检查时惰性清理」：登记键对应注册表已无活跃连接即视为
# 已释放，保证计数最终一致、不因停止路径不可达而只增不减。
_TIKTOK_ACTIVE_KEYS: Set[Tuple[str, Optional[int]]] = set()


def build_message_handler(consumer: MessageConsumer):
    """构造 PDDChannel 的消息消费回调：把原始报文交给消费器处理。

    Args:
        consumer: 本店铺的消息处理消费器。

    Returns:
        异步回调 ``handler(raw_message, shop_id, user_id)``，异常被消费器内部
        吞掉，不影响 PDDChannel 的消息接收循环。
    """

    async def _handler(raw_message: Any, shop_id: str, user_id: int) -> None:
        # 消费器内部已对解析 / 处理异常做兜底，这里再加一层保护避免影响接收循环。
        try:
            await consumer.consume_raw(raw_message)
        except Exception as exc:  # noqa: BLE001 - 消费异常不中断后续消息接收
            logger.error("消费消息异常: shop_id=%s, %s", shop_id, exc)

    return _handler


def _enforce_tiktok_limit(shop_id: str, user_id: Optional[int]) -> None:
    """检查 TikTok 并发浏览器实例上限（TIKTOK_MAX_BROWSER_INSTANCES，TIK-017）。

    超限时抛 ``RuntimeError``（由调用方路由规整为失败响应），并以 error 级别记
    日志告警（企微告警链路在 Phase 2 多店资源管理中细化）。检查前先惰性清理
    「注册表中已无活跃连接」的登记键，保证计数最终一致。

    Args:
        shop_id: 店铺业务标识。
        user_id: 归属用户 ID。

    Raises:
        RuntimeError: 活跃 TikTok 通道数已达配置上限时抛出。
    """
    max_instances = get_settings().tiktok_max_browser_instances
    if max_instances is None or max_instances <= 0:
        return
    # 惰性清理：登记键对应的连接已不在注册表 → 视为已释放。
    stale = [
        key for key in _TIKTOK_ACTIVE_KEYS
        if connection_registry.get(key[0], key[1]) is None
    ]
    for key in stale:
        _TIKTOK_ACTIVE_KEYS.discard(key)
    if len(_TIKTOK_ACTIVE_KEYS) >= max_instances:
        logger.error(
            "TikTok 浏览器实例已达上限 %d 个，拒绝新连接: shop_id=%s, user_id=%s",
            max_instances,
            shop_id,
            user_id,
        )
        raise RuntimeError(
            f"TikTok 浏览器实例已达上限（{max_instances} 个），请先释放其它店铺连接"
        )


def create_channel(
    shop_id: str,
    shop_pk: int,
    user_id: int,
    *,
    platform: str = PLATFORM_PDD,
    channel_name: str = "pinduoduo",
    enable_notify: bool = True,
    consumer: Optional[MessageConsumer] = None,
    proxy_server: Optional[str] = None,
    browser_data_dir: Optional[str] = None,
) -> ChannelAdapter:
    """创建一个「连接 + 消费器」已串联的通道（不自动启动，按 platform 分派）。

    Args:
        shop_id: 店铺业务标识。
        shop_pk: 店铺主键 shop.id。
        user_id: 归属用户 ID。
        platform: 平台标识（默认 PLATFORM_PDD）；非法平台回退 PDD 路径（向后
            兼容，存量调用方缺省即走拼多多旧链路，零行为变更）。
        channel_name: 渠道名称（默认 pinduoduo）。
        enable_notify: 是否启用系统事件通知（经 backend HTTP 推送）。
        consumer: 可注入的消费器（便于测试）；缺省按本店铺构造。
        proxy_server: 店铺出口代理服务器地址（Phase 2 前置，仅 tiktok 分支建
            浏览器会话时注入 BrowserSession；None 表示不走代理，PDD 分支不使用）。
        browser_data_dir: TikTok 登录态浏览器用户数据目录（建店登录后落库
            Shop.browser_data_dir，连接时复用免二次登录；None 表示按 shop_pk
            推导默认目录，兼容存量店铺）。

    Returns:
        已按平台分派的通道实例（PDD 走 PDDChannel；tiktok 走 TikTokChannel 真实现，
        TIK-014 联调落地）。
    """
    # 平台校验：非法/缺省一律回退 PDD，保证向后兼容与 PDD 路径零行为变更。
    if not is_allowed_platform(platform):
        logger.warning("未知平台 '%s'，回退拼多多路径: shop_id=%s", platform, shop_id)
        platform = PLATFORM_PDD

    # TikTok 分支（TIK-014 联调真实现）：装配共享浏览器会话 + TikTokChannel +
    # TikTokSender + 注入 TikTok 解析器的消费器，与 PDD 分支同款契约。
    # 注意：本方法恒在 async 上下文内被调用（start_channel/start_enabled_channels/
    # connect 路由），故可直接取到主循环注入 TikTokSender 的线程桥接。
    if platform == PLATFORM_TIKTOK:
        # 灰度总开关（TIK-017）：TIKTOK_SHOP_ENABLED=false 时直接拒绝 TikTok 连接
        # 请求（抛错 → 路由规整为失败响应），防止灰度期误接入。
        if not get_settings().tiktok_shop_enabled:
            logger.warning(
                "TikTok 通道未启用（TIKTOK_SHOP_ENABLED=false），拒绝连接请求: "
                "shop_id=%s, user_id=%s",
                shop_id,
                user_id,
            )
            raise RuntimeError("TikTok 通道未启用（TIKTOK_SHOP_ENABLED=false）")

        logger.info("创建 TikTok 真实通道: shop_id=%s, user_id=%s", shop_id, user_id)

        # 工厂建一个浏览器会话（注入店铺出口代理与登录态目录，仅 TikTok 通道消费；
        # browser_data_dir 为建店登录后落库的登录态目录，复用免二次登录），同时注入
        # TikTokChannel 与 TikTokSender，避免两者各自自建第二份会话，保证「发送与
        # 监控同页」。
        browser_session = BrowserSession(
            shop_pk=shop_pk,
            proxy_server=proxy_server,
            user_data_dir=browser_data_dir,
        )

        settings = get_settings()
        # 发送器需与主循环桥接（线程内同步调用经 run_coroutine_threadsafe 调度回主循环）。
        sender = TikTokSender(
            shop_id=shop_id,
            user_id=user_id,
            browser_session=browser_session,
            main_loop=asyncio.get_running_loop(),
            send_timeout=settings.tiktok_send_timeout_seconds,
        )

        # 消费器：注入 TikTok 解析器与发送器（解析器绑定本店铺 shop_id）。
        if consumer is None:
            consumer = MessageConsumer(
                shop_id=shop_id,
                shop_pk=shop_pk,
                user_id=user_id,
                channel_name=channel_name,
                message_parser=functools.partial(parse_tiktok_raw, shop_id=shop_id),
                sender=sender,
                notifier=build_notifier(shop_pk) if enable_notify else None,
            )

        # 为本店铺分配独立 FIFO 队列（与 PDD 分支同款键约定）。
        queue = message_queue_manager.get_or_create(f"{user_id}:{shop_id}")

        # 连接断开 / 登录失效告警通知器（复用全局去重单例，与 PDD 分支同款语义）。
        # TIK-018：send_cb 经内部接口转发 backend 推送企微等渠道（原 None 仅日志占位）。
        event_notifier = build_alert_notifier(
            get_alert_dedup(),
            shop_pk,
            send_cb=backend_alert_send_cb,
        )

        # 消息消费回调：原始报文交消费器处理（handler(raw, shop_id, user_id) 签名
        # 与 TikTokChannel 消费循环一致，直接复用）。
        message_handler = build_message_handler(consumer)

        channel = TikTokChannel(
            shop_id=shop_id,
            user_id=user_id,
            shop_pk=shop_pk,
            message_queue=queue,
            message_handler=message_handler,
            browser_session=browser_session,
            event_notifier=event_notifier,
            poll_interval=settings.tiktok_poll_interval_seconds,
            debounce_seconds=settings.tiktok_debounce_seconds,
            sender=sender,
        )
        return channel

    # PDD 分支（存量路径，逐字节零行为变更）：以下逻辑与 TIK-005 交付完全一致。
    if consumer is None:
        consumer = MessageConsumer(
            shop_id=shop_id,
            shop_pk=shop_pk,
            user_id=user_id,
            channel_name=channel_name,
            notifier=build_notifier(shop_pk) if enable_notify else None,
        )

    # 为本店铺分配独立 FIFO 队列（入队顺序 == 消费顺序，需求 5.3）。
    queue = message_queue_manager.get_or_create(f"{user_id}:{shop_id}")

    # TIK-005：注入「连接断开告警」事件通知器。复用全局去重单例，按 shop_pk 维度
    # 防抖（默认 30 分钟静默）。事件类型为 connection_disconnected，与 cookies
    # 刷新失败链路共用同一定义。
    # TIK-018：send_cb 经内部接口转发 backend 推送企微等渠道（原 None 仅日志占位）。
    event_notifier = build_alert_notifier(
        get_alert_dedup(),
        shop_pk,
        send_cb=backend_alert_send_cb,
    )

    channel = PDDChannel(
        shop_id=shop_id,
        user_id=user_id,
        channel_name=channel_name,
        message_queue=queue,
        message_handler=build_message_handler(consumer),
        event_notifier=event_notifier,
    )
    return channel


async def start_channel(
    shop_id: str,
    shop_pk: int,
    user_id: int,
    *,
    platform: str = PLATFORM_PDD,
    channel_name: str = "pinduoduo",
    enable_notify: bool = True,
    proxy_server: Optional[str] = None,
    browser_data_dir: Optional[str] = None,
) -> ChannelAdapter:
    """创建、启动并登记一个店铺连接（端到端链路就绪，按 platform 分派）。

    Args:
        shop_id: 店铺业务标识。
        shop_pk: 店铺主键 shop.id。
        user_id: 归属用户 ID。
        platform: 平台标识（默认 PLATFORM_PDD）；缺省走拼多多旧链路，零变更。
        channel_name: 渠道名称（默认 pinduoduo）。
        enable_notify: 是否启用系统事件通知。
        proxy_server: 店铺出口代理服务器地址（Phase 2 前置，仅 tiktok 分支消费，
            透传给 create_channel → BrowserSession；None 表示不走代理）。
        browser_data_dir: TikTok 登录态浏览器用户数据目录（建店登录后落库
            Shop.browser_data_dir，连接时复用免二次登录；None 按 shop_pk 推导）。

    Returns:
        已启动并登记到连接注册表的通道实例（PDD / TikTok 真实现按平台分派）。
    """
    # 幂等保护：同店铺已有活跃连接则跳过，避免重复建连（参照项目 is_running 判断）。
    existing = connection_registry.get(shop_id, user_id)
    if existing is not None:
        logger.info(
            "店铺已有活跃连接，跳过重复启动: shop_id=%s, user_id=%s", shop_id, user_id
        )
        return existing

    # TikTok 并发实例上限检查（TIK-017）：超限抛错，由调用方规整为失败响应。
    if platform == PLATFORM_TIKTOK:
        _enforce_tiktok_limit(shop_id, user_id)

    channel = create_channel(
        shop_id,
        shop_pk,
        user_id,
        platform=platform,
        channel_name=channel_name,
        enable_notify=enable_notify,
        proxy_server=proxy_server,
        browser_data_dir=browser_data_dir,
    )
    await channel.start()
    connection_registry.register(shop_id, user_id, channel)
    if platform == PLATFORM_TIKTOK:
        _TIKTOK_ACTIVE_KEYS.add((shop_id, user_id))
    logger.info(
        "店铺连接已启动并登记: shop_id=%s, user_id=%s, platform=%s",
        shop_id,
        user_id,
        platform,
    )
    return channel


async def start_enabled_channels() -> int:
    """启动全部「已启用」店铺的连接（服务启动时自动拉起，参照项目「启动所有」）。

    从数据库读取所有 ``status=启用`` 的店铺，逐个调用 ``start_channel`` 启动其
    拼多多长连接并装配消息处理全链路；已有活跃连接的店铺由 ``start_channel``
    幂等跳过。单个店铺启动失败仅记日志、不中断其它店铺（健壮性兜底，需求 26）。

    Returns:
        本次实际新启动的店铺连接数量。
    """
    # 一次性读出启用店铺的最小必要字段（独立短事务，读完即释放连接）。
    # 读出启用店铺的（shop_id, shop_pk, owner_user_id, platform, proxy_server,
    # browser_data_dir）最小必要字段（proxy_server / browser_data_dir 为 TikTok
    # 通道专用：代理为 Phase 2 前置店铺级出口；browser_data_dir 为建店登录后
    # 落库的登录态目录，连接时复用免二次登录）。
    shops: list[tuple[str, int, Optional[int], str, Optional[str], Optional[str]]] = []
    try:
        with session_scope() as session:
            enabled_shops = Repository(Shop, session).list(
                filters={"status": _SHOP_STATUS_ENABLED}, order_by=False
            )
            for shop in enabled_shops:
                shop_id = str(shop.shop_id or "").strip()
                if not shop_id:
                    continue
                # 读出店铺平台（存量列缺省 'pdd'；空值回退拼多多，向后兼容）。
                shop_platform = str(getattr(shop, "platform", None) or PLATFORM_PDD).strip()
                if not is_allowed_platform(shop_platform):
                    shop_platform = PLATFORM_PDD
                # 店铺出口代理（空串 / 缺失归一为 None = 不走代理）。
                proxy_server = str(getattr(shop, "proxy_server", None) or "").strip() or None
                # TikTok 登录态目录（空串 / 缺失归一为 None = 按 shop_pk 推导）。
                browser_data_dir = (
                    str(getattr(shop, "browser_data_dir", None) or "").strip() or None
                )
                shops.append(
                    (
                        shop_id,
                        shop.id,
                        shop.owner_user_id,
                        shop_platform,
                        proxy_server,
                        browser_data_dir,
                    )
                )
    except Exception as exc:  # noqa: BLE001 - 读库失败不应中断服务启动
        logger.error("读取启用店铺列表失败，跳过自动启动连接: %s", exc)
        return 0

    if not shops:
        logger.info("无已启用店铺，跳过自动启动连接")
        return 0

    logger.info("服务启动：开始自动拉起 %d 个已启用店铺的连接", len(shops))
    started = 0
    for shop_id, shop_pk, owner_user_id, shop_platform, proxy_server, browser_data_dir in shops:
        try:
            await start_channel(
                shop_id,
                shop_pk,
                owner_user_id,
                platform=shop_platform,
                proxy_server=proxy_server,
                browser_data_dir=browser_data_dir,
            )
            started += 1
        except Exception as exc:  # noqa: BLE001 - 单店铺启动失败不影响其它店铺
            logger.error("自动启动店铺连接失败: shop_id=%s, %s", shop_id, exc)

    logger.info("自动启动已启用店铺连接完成：成功 %d/%d", started, len(shops))
    return started


async def stop_channel(shop_id: str, user_id: Optional[int]) -> bool:
    """停止并注销指定店铺连接（复用注册表断连，幂等，需求 3.5）。

    停止后从全局队列管理器移除本店铺的 FIFO 队列，避免店铺频繁启停在
    ``message_queue_manager`` 中残留队列对象造成内存泄漏。

    Args:
        shop_id: 拼多多店铺业务标识。
        user_id: 归属用户 ID（可空）。

    Returns:
        断开成功（或本就无连接）返回 True；停止出错返回 False。
    """
    ok = await connection_registry.disconnect(shop_id, user_id)
    # 断连后移除本店铺队列（与 create_channel 的命名一致），释放内存。
    if user_id is not None:
        message_queue_manager.remove(f"{user_id}:{shop_id}")
    return ok


__all__ = [
    "build_message_handler",
    "create_channel",
    "start_channel",
    "start_enabled_channels",
    "stop_channel",
]
