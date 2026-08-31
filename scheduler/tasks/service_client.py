# -*- coding: utf-8 -*-
"""
scheduler.tasks.service_client —— 跨服务 HTTP 调用（复用 common 统一客户端）
==========================================================================
本文件用途：scheduler 定时任务需「跨服务」触发能力时（Cookie 刷新由 websocket
服务执行、商品同步由 backend 服务执行），统一经 **HTTP 调用**对应服务完成，地址
一律经环境变量 ``WEBSOCKET_SERVICE_URL`` / ``BACKEND_WEB_SERVICE_URL`` 配置，
**禁止写死 localhost**（规范 21 / 需求 25.4）。

实现要点（任务 19.1 统一）：
- 复用 common 统一服务间 HTTP 客户端 ``common.services.service_client``（规范 36/52），
  不再各自维护一份 urllib 客户端；本模块仅保留 scheduler 专属的「触发」语义封装与
  ``CallResult`` 结果结构（保持对 task_runners 的既有 API 兼容）。
- 网络不可达 / 超时 / 非 2xx / 解析失败一律规整为 ``CallResult(ok=False, ...)``，
  不抛异常打断调度主流程（健壮性兜底）；由调用方据 ok 写成功 / 失败执行日志。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from common.core.config import get_settings
from common.services import service_client

# 模块级日志记录器（禁用 debug 级别 —— 规范 38）。
logger = logging.getLogger("scheduler.service_client")

# 跨服务调用默认超时（秒）：定时任务触发为非交互式，给予较宽裕但有界的超时。
_DEFAULT_TIMEOUT_SECONDS: float = 60.0

# 跨服务调用重试参数：对「传输层失败」（不可达 / 超时 / 非 2xx）做有界指数退避重试，
# 避免对端短暂抖动导致整次调度任务失败、需等到下个周期才重试（需求 13 / 健壮性）。
_MAX_RETRIES: int = 3
_RETRY_INITIAL_DELAY: float = 1.0
_RETRY_MAX_DELAY: float = 8.0
_RETRY_BACKOFF: float = 2.0

# websocket 服务「Cookie 刷新」接口相对路径（与 websocket 服务路由约定一致）。
_COOKIE_REFRESH_PATH: str = "/api/v1/cookies/refresh"
# backend 服务「商品同步」接口相对路径（与 backend products 路由约定一致）。
_PRODUCT_SYNC_PATH: str = "/api/v1/products/sync"
# websocket 服务「连接管理」接口相对路径（与 websocket routes/connections 约定一致）：
# connect（启动连接，带 platform）/ disconnect（断开连接）/ status-batch（批量查状态）。
_CONNECT_PATH: str = "/api/v1/connections/connect"
_DISCONNECT_PATH: str = "/api/v1/connections/disconnect"
_STATUS_BATCH_PATH: str = "/api/v1/connections/status-batch"
# backend 服务「系统事件通知」内部接口相对路径（与 backend 路由约定一致，TIK-026）：
# 回复率跌破阈值等告警事件经该接口走通知渠道（企微群机器人）并落通知记录。
_NOTIFY_EVENTS_PATH: str = "/api/v1/internal/notify-events"


@dataclass
class CallResult:
    """跨服务 HTTP 调用结果（对各服务统一响应体的规整）。

    Attributes:
        ok: 调用是否成功（对应响应体 success=true）。
        message: 结果信息（中文）：成功概述或失败原因。
        data: 成功时返回的业务数据（可选）。
    """

    ok: bool = False
    message: str = ""
    data: Optional[Dict[str, Any]] = None


def _to_call_result(response: service_client.ServiceResponse) -> CallResult:
    """将统一客户端的 ``ServiceResponse`` 规整为 scheduler 的 ``CallResult``。

    Args:
        response: 统一服务间客户端返回的响应。

    Returns:
        规整后的 ``CallResult``。
    """
    # 传输层失败：目标服务不可达 / 返回非法。
    if not response.ok or response.body is None:
        return CallResult(ok=False, message=response.error or "目标服务暂不可用")
    success = response.success
    message = response.message or ("成功" if success else "调用失败")
    return CallResult(ok=success, message=message, data=response.data)


def _post_with_retry(
    base_url: str,
    path: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> service_client.ServiceResponse:
    """对跨服务 POST 做「传输层失败」的有界指数退避重试。

    仅对「目标服务不可达 / 超时 / 非 2xx / 响应非法」这类传输层失败重试；业务层
    失败（响应体 success=false）为确定性结果，不重试（重试无意义且可能重复副作用）。

    Args:
        base_url: 目标服务基础地址（经环境变量配置）。
        path: 接口相对路径。
        payload: 请求体。
        headers: 可选请求头（如内部接口鉴权 ``X-Internal-Token``，TIK-026）。

    Returns:
        最后一次调用的 ``ServiceResponse``（成功或已耗尽重试）。
    """
    delay = _RETRY_INITIAL_DELAY
    response = service_client.post_json(
        base_url, path, payload, timeout=_DEFAULT_TIMEOUT_SECONDS, headers=headers
    )
    for attempt in range(1, _MAX_RETRIES):
        # 传输层成功（可达且响应体合法）：无论业务成败都不再重试。
        if response.ok and response.body is not None:
            return response
        logger.warning(
            "跨服务调用传输失败，%.1f 秒后重试（%d/%d）：%s%s，原因=%s",
            delay,
            attempt,
            _MAX_RETRIES - 1,
            base_url,
            path,
            response.error or "目标服务暂不可用",
        )
        time.sleep(delay)
        delay = min(delay * _RETRY_BACKOFF, _RETRY_MAX_DELAY)
        response = service_client.post_json(
            base_url, path, payload, timeout=_DEFAULT_TIMEOUT_SECONDS, headers=headers
        )
    return response


def trigger_cookie_refresh(
    shop_pk: int,
    shop_id: str,
    owner_user_id: Optional[int],
    platform: str = "pdd",
) -> CallResult:
    """经 HTTP 调用 websocket 服务刷新指定店铺的 Cookie（需求 4.6 / 21.2）。

    地址经环境变量 ``WEBSOCKET_SERVICE_URL`` 配置（禁止写死 localhost，规范 21）。
    传输层失败时按指数退避重试，避免对端短暂抖动导致本次任务失败。

    Args:
        shop_pk: 店铺主键（shop.id）。
        shop_id: 拼多多店铺业务标识。
        owner_user_id: 店铺归属用户 ID（用于 websocket 侧定位凭据）。
        platform: 平台标识（pdd / tiktok，透传给 websocket 侧；TikTok 登录态常驻
            浏览器目录，websocket 侧跳过刷新返回 success，防止误走 PDD 刷新路径）。

    Returns:
        规整后的 ``CallResult``。
    """
    response = _post_with_retry(
        service_client.websocket_base_url(),
        _COOKIE_REFRESH_PATH,
        {
            "shop_pk": shop_pk,
            "shop_id": shop_id,
            "owner_user_id": owner_user_id,
            "platform": platform,
        },
    )
    return _to_call_result(response)


def trigger_product_sync(shop_pk: int) -> CallResult:
    """经 HTTP 调用 backend 服务触发指定店铺的商品同步（需求 15.2 / 21.2）。

    地址经环境变量 ``BACKEND_WEB_SERVICE_URL`` 配置（禁止写死 localhost，规范 21）。
    传输层失败时按指数退避重试，避免对端短暂抖动导致本次任务失败。

    Args:
        shop_pk: 店铺主键（shop.id）。

    Returns:
        规整后的 ``CallResult``。
    """
    response = _post_with_retry(
        service_client.backend_base_url(),
        _PRODUCT_SYNC_PATH,
        {"shop_pk": shop_pk},
    )
    return _to_call_result(response)


def trigger_connect(
    shop_pk: int,
    shop_id: str,
    owner_user_id: Optional[int],
    *,
    platform: str = "pdd",
    proxy_server: Optional[str] = None,
) -> CallResult:
    """经 HTTP 调用 websocket 服务启动指定店铺的连接（需求 24.x，TIK-015）。

    地址经环境变量 ``WEBSOCKET_SERVICE_URL`` 配置（禁止写死 localhost，规范 21）。
    传输层失败时按指数退避重试，避免对端短暂抖动导致本次任务失败。

    Args:
        shop_pk: 店铺主键（shop.id）。
        shop_id: 店铺业务标识。
        owner_user_id: 店铺归属用户 ID。
        platform: 平台标识（pdd/tiktok，透传给 websocket 侧工厂分派）。
        proxy_server: 店铺出口代理服务器地址（Phase 2 前置，仅 tiktok 店铺消费，
            空 / None 表示不走代理），透传给 websocket 侧建浏览器会话。

    Returns:
        规整后的 ``CallResult``。
    """
    response = _post_with_retry(
        service_client.websocket_base_url(),
        _CONNECT_PATH,
        {
            "shop_pk": shop_pk,
            "shop_id": shop_id,
            "owner_user_id": owner_user_id,
            "platform": platform,
            "proxy_server": proxy_server,
        },
    )
    return _to_call_result(response)


def trigger_disconnect(
    shop_pk: int, shop_id: str, owner_user_id: Optional[int]
) -> CallResult:
    """经 HTTP 调用 websocket 服务断开指定店铺的连接（需求 24.x，TIK-015，幂等）。

    地址经环境变量 ``WEBSOCKET_SERVICE_URL`` 配置（禁止写死 localhost，规范 21）。
    传输层失败时按指数退避重试，避免对端短暂抖动导致本次任务失败。

    Args:
        shop_pk: 店铺主键（shop.id）。
        shop_id: 店铺业务标识。
        owner_user_id: 店铺归属用户 ID。

    Returns:
        规整后的 ``CallResult``。
    """
    response = _post_with_retry(
        service_client.websocket_base_url(),
        _DISCONNECT_PATH,
        {
            "shop_pk": shop_pk,
            "shop_id": shop_id,
            "owner_user_id": owner_user_id,
        },
    )
    return _to_call_result(response)


def query_status_batch(
    shops: list[dict[str, Any]],
) -> CallResult:
    """经 HTTP 调用 websocket 服务批量查询多个店铺的连接状态（需求 24.x，TIK-015）。

    一次请求查询全部店铺，避免逐个 HTTP 调用；地址经环境变量
    ``WEBSOCKET_SERVICE_URL`` 配置（禁止写死 localhost，规范 21）。传输层失败时
    按指数退避重试，避免对端短暂抖动导致本次任务失败。

    Args:
        shops: 待查询店铺列表，每项 ``{"shop_id": ..., "owner_user_id": ...}``。

    Returns:
        规整后的 ``CallResult``；成功时 ``data`` 含 ``{"statuses": [...]}``。
    """
    response = _post_with_retry(
        service_client.websocket_base_url(),
        _STATUS_BATCH_PATH,
        {"shops": shops},
    )
    return _to_call_result(response)


def trigger_notify_event(event_type: str, content: str, shop_pk: int) -> CallResult:
    """经 HTTP 调用 backend 内部接口推送一条系统事件告警（TIK-026，零新增服务）。

    回复率跌破阈值等告警事件经 backend ``/api/v1/internal/notify-events`` 走既有
    通知链路：按店铺已启用渠道（企微群机器人等）推送并落 ``pdd_notify_record``。
    服务间共享密钥经 ``common.core.config`` 读取并以 ``X-Internal-Token`` 请求头
    携带（与 websocket 侧 alert_forwarder 同款鉴权约定）。传输层失败时按指数退避
    重试，避免对端短暂抖动导致告警丢失。

    Args:
        event_type: 事件类型（须为 backend ``notify_service`` 已注册类型，
            如 ``reply_rate_below_threshold``，否则被白名单拒绝）。
        content: 通知内容（中文）。
        shop_pk: 事件归属店铺主键。

    Returns:
        规整后的 ``CallResult``；成功时 ``data`` 含推送统计
        ``{"total", "success", "failed"}``。
    """
    token = get_settings().internal_service_token
    response = _post_with_retry(
        service_client.backend_base_url(),
        _NOTIFY_EVENTS_PATH,
        {
            "event_type": event_type,
            "content": content,
            "shop_pk": int(shop_pk),
        },
        headers={"X-Internal-Token": token},
    )
    return _to_call_result(response)


__all__ = [
    "CallResult",
    "trigger_cookie_refresh",
    "trigger_product_sync",
    "trigger_connect",
    "trigger_disconnect",
    "query_status_batch",
    "trigger_notify_event",
]
