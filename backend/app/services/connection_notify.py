# -*- coding: utf-8 -*-
"""
backend.app.services.connection_notify —— 连接断开通知（经统一服务间客户端通知 websocket）
======================================================================================
本文件用途：当店铺被停用时，backend 服务需断开其与拼多多的 WebSocket 长连接
（需求 3.5）。按设计「多服务拆分架构」，长连接由 **websocket 服务**维护，故
backend 通过 **HTTP 调用** websocket 服务的断开接口完成断连，地址经环境变量
``WEBSOCKET_SERVICE_URL`` 配置，**禁止写死 localhost**（规范 21）。

实现要点（任务 19.1 统一）：
- 复用 common 统一服务间 HTTP 客户端 ``common.services.service_client``（规范 36/52，
  不再各自维护一份 urllib 客户端）；请求设较短超时，失败仅记录日志、返回 False，
  不抛异常打断「停用」主流程（停用必须成功落库，断连是尽力而为的副作用）。
- 服务地址取自统一客户端的 ``websocket_base_url()``（环境变量优先）。
"""
from __future__ import annotations

import logging

from common.services import service_client

logger = logging.getLogger(__name__)

# 连接状态查询 / 断开的请求超时（秒）：尽力通知，避免长时间阻塞停用流程。
_NOTIFY_TIMEOUT_SECONDS: float = 5.0

# 启动连接请求超时（秒）：TikTok 通道建浏览器会话（清锁 → 启动 Chromium → 打开
# 聊天页）实测需 5~10 秒，超时过短会误报「启动失败」并导致调用方重试、重复建连
# （2026-08-28 实测暴露）；PDD 长连接启动也远小于该值，统一放宽无副作用。
_CONNECT_TIMEOUT_SECONDS: float = 30.0

# websocket 服务断开连接接口的相对路径（与 websocket 服务路由约定一致）。
_DISCONNECT_PATH: str = "/api/v1/connections/disconnect"

# websocket 服务启动连接接口的相对路径（与 websocket 服务路由约定一致）。
_CONNECT_PATH: str = "/api/v1/connections/connect"

# websocket 服务连接状态查询接口的相对路径（与 websocket 服务路由约定一致）。
_STATUS_PATH: str = "/api/v1/connections/status"

# websocket 服务批量连接状态查询接口的相对路径（与 websocket 服务路由约定一致）。
_STATUS_BATCH_PATH: str = "/api/v1/connections/status-batch"


def query_connected(
    shop_id: str, owner_user_id: int | None, platform: str = "pdd"
) -> bool:
    """查询指定店铺当前是否已建立活跃的拼多多长连接（需求 5.8）。

    经统一服务间客户端 POST 调用 websocket 服务状态接口。网络不可达 / 失败一律
    视为「未连接」返回 False，不抛异常（健壮性兜底，需求 26）。

    Args:
        shop_id: 拼多多店铺业务标识。
        owner_user_id: 店铺归属用户 ID。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。

    Returns:
        已连接返回 True；未连接 / 查询失败返回 False。
    """
    response = service_client.post_json(
        service_client.websocket_base_url(),
        _STATUS_PATH,
        {
            "shop_id": shop_id,
            "owner_user_id": owner_user_id,
            "platform": platform,
        },
        timeout=_NOTIFY_TIMEOUT_SECONDS,
    )
    if response.success and response.data is not None:
        return bool(response.data.get("connected"))
    return False


def query_connected_batch(
    shops: list[tuple[str, int | None]], platform: str = "pdd"
) -> dict[str, bool]:
    """批量查询多个店铺的连接状态（一次 HTTP，避免逐个查询，需求 5.8）。

    经统一服务间客户端 POST 调用 websocket 批量状态接口。网络不可达 / 失败一律
    视为全部「未连接」，不抛异常（健壮性兜底，需求 26）。

    Args:
        shops: 待查询的 (shop_id, owner_user_id) 元组列表。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。

    Returns:
        以 shop_id 为键、是否连接为值的字典；查询失败时各 shop_id 均为 False。
    """
    default = {str(shop_id): False for shop_id, _ in shops}
    if not shops:
        return default

    response = service_client.post_json(
        service_client.websocket_base_url(),
        _STATUS_BATCH_PATH,
        {
            "shops": [
                {
                    "shop_id": shop_id,
                    "owner_user_id": owner_user_id,
                    "platform": platform,
                }
                for shop_id, owner_user_id in shops
            ]
        },
        timeout=_NOTIFY_TIMEOUT_SECONDS,
    )
    if response.success and response.data is not None:
        result = dict(default)
        for item in response.data.get("statuses") or []:
            sid = item.get("shop_id")
            if sid is not None:
                result[str(sid)] = bool(item.get("connected"))
        return result
    return default


def notify_connect(
    shop_pk: int,
    shop_id: str,
    owner_user_id: int | None,
    platform: str = "pdd",
    proxy_server: str | None = None,
) -> bool:
    """通知 websocket 服务启动指定店铺的连接（需求 3 / 5.1）。

    当店铺被新增 / 启用时，backend 经统一服务间客户端 POST 调用 websocket 服务的
    启动接口，由其建立拼多多长连接并装配消息处理全链路。地址由环境变量配置
    （禁止写死 localhost，规范 21）。无论 websocket 是否可达，本函数都不抛异常：
    业务成功返回 True，失败记录日志并返回 False，确保不影响店铺新增 / 启用主流程
    的落库与响应（启动连接是尽力而为的副作用，需求 26）。

    Args:
        shop_pk: 店铺主键（shop.id）。
        shop_id: 拼多多店铺业务标识。
        owner_user_id: 店铺归属用户 ID（用于 websocket 侧定位凭据与连接）。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。
        proxy_server: 店铺出口代理服务器地址（Phase 2 前置，仅 TikTok 通道消费；
            空 / None 表示不走代理），透传给 websocket 侧建浏览器会话。

    Returns:
        通知成功返回 True；失败（网络异常 / 非 2xx / 业务失败）返回 False。
    """
    response = service_client.post_json(
        service_client.websocket_base_url(),
        _CONNECT_PATH,
        {
            "shop_pk": shop_pk,
            "shop_id": shop_id,
            "owner_user_id": owner_user_id,
            "platform": platform,
            "proxy_server": proxy_server,
        },
        timeout=_CONNECT_TIMEOUT_SECONDS,
    )
    if response.success:
        logger.info("已通知 websocket 服务启动店铺连接：shop_id=%s", shop_id)
        return True
    logger.warning(
        "通知 websocket 启动连接失败（不影响店铺启用）：shop_id=%s err=%s",
        shop_id,
        response.message or response.error,
    )
    return False


def notify_disconnect(
    shop_pk: int, shop_id: str, owner_user_id: int | None, platform: str = "pdd"
) -> bool:
    """通知 websocket 服务断开指定店铺的连接（需求 3.5）。

    经统一服务间客户端 POST 调用 websocket 服务断开接口，地址由环境变量配置。
    无论 websocket 服务是否可达，本函数都不抛异常：业务成功返回 True，失败记录
    日志并返回 False，确保不影响「店铺停用」主流程的落库与响应。

    Args:
        shop_pk: 店铺主键（shop.id）。
        shop_id: 拼多多店铺业务标识。
        owner_user_id: 店铺归属用户 ID（用于 websocket 侧定位连接）。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。

    Returns:
        通知成功返回 True；失败（网络异常 / 非 2xx / 业务失败）返回 False。
    """
    response = service_client.post_json(
        service_client.websocket_base_url(),
        _DISCONNECT_PATH,
        {
            "shop_pk": shop_pk,
            "shop_id": shop_id,
            "owner_user_id": owner_user_id,
            "platform": platform,
        },
        timeout=_NOTIFY_TIMEOUT_SECONDS,
    )
    if response.success:
        logger.info("已通知 websocket 服务断开店铺连接：shop_id=%s", shop_id)
        return True
    logger.warning(
        "通知 websocket 断开连接失败（不影响停用）：shop_id=%s err=%s",
        shop_id,
        response.message or response.error,
    )
    return False


__all__ = ["notify_disconnect", "notify_connect", "query_connected", "query_connected_batch"]
