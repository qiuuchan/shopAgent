# -*- coding: utf-8 -*-
"""
websocket.routes.cookies —— Cookie 刷新接口（供 scheduler 调用）
==============================================================
本文件用途：提供 websocket 服务的「Cookie 刷新」HTTP 接口，供 scheduler 定时任务
服务经服务间 HTTP 调用，定时刷新指定店铺账号的登录态 Cookie（需求 4.6 / 21.2）。

- ``POST /cookies/refresh``：按 (shop_id, owner_user_id) 定位账号名后，复用
  ``channel_pdd.pdd_login.refresh_pdd_cookies`` 无头刷新 Cookie 并维护登录态；
  刷新成功后将新 Cookie 以可逆加密回写 account 表（不返回明文，需求 3.6）。

接口约定（开发规范 1-3）：HTTP 恒返回 200，业务成败由统一响应体
``{code, success, message, data}`` 表达。地址经环境变量配置（禁止写死 localhost，
规范 21）。本路由不抛异常，失败统一规整为失败响应（健壮性兜底，需求 26）；
对外响应不包含 Cookie 等敏感字段（需求 3.6）。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）、日志禁用 debug（规范 38）、复用共通（规范 36/52）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from channel_pdd import pdd_login
from channel_pdd.core.credential_store import (
    load_account_credentials,
    update_account_cookies,
)
from common.schemas.common import ApiResponse, error_response, success_response
from engine.alert_dedup import build_alert_notifier, get_alert_dedup

logger = logging.getLogger("websocket.routes.cookies")

# Cookie 刷新失败告警事件类型（与连接断开链路共用 alert_dedup 去重维度）。
_EVENT_COOKIE_REFRESH_FAILED: str = "cookie_refresh_failed"

# Cookie 刷新路由：标签便于 OpenAPI 分组；前缀由聚合层添加。
router = APIRouter(tags=["Cookie 刷新"])


def _alert_cookie_refresh_failed(shop_pk: int, shop_id: str, reason: str) -> None:
    """触发 Cookie 刷新失败告警（TIK-005：与连接断开共用去重链路）。

    复用全局去重单例按 (shop_pk, cookie_refresh_failed) 维度防抖；Webhook 地址暂未
    提供，send_cb 缺省为 None，仅日志占位。失败不应影响本路由的响应体返回。

    Args:
        shop_pk: 店铺主键 shop.id。
        shop_id: 拼多多店铺业务标识。
        reason: 失败原因（仅用于日志 / 占位内容）。
    """
    try:
        notifier = build_alert_notifier(get_alert_dedup(), shop_pk, send_cb=None)
        notifier(
            _EVENT_COOKIE_REFRESH_FAILED,
            f"店铺 shop_id={shop_id} Cookie 刷新失败：{reason}",
        )
    except Exception as exc:  # noqa: BLE001 - 告警失败不得影响主流程
        logger.warning(
            "Cookie 刷新失败告警触发异常（已忽略）: shop_id=%s, %s", shop_id, exc
        )


class RefreshCookieRequest(BaseModel):
    """Cookie 刷新请求体（与 scheduler service_client 约定一致）。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")
    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")
    platform: str = Field("pdd", description="平台标识（pdd/tiktok，缺省 pdd）")


@router.post(
    "/cookies/refresh",
    response_model=ApiResponse,
    summary="刷新指定店铺账号的登录态 Cookie",
)
async def refresh_cookie(payload: RefreshCookieRequest) -> ApiResponse:
    """刷新指定店铺账号的登录态 Cookie（需求 4.6 / 21.2）。

    先按 (shop_id, owner_user_id) 定位账号名，再复用 ``refresh_pdd_cookies`` 无头
    刷新 Cookie 并维护登录态；刷新成功后将新 Cookie 加密回写 account 表。无论刷新
    成败本路由都不抛异常：成功返回 success；登录态失效 / 失败返回 error_response。
    对外响应不包含 Cookie 明文（需求 3.6）。

    Args:
        payload: 含 shop_pk / shop_id / owner_user_id 的刷新请求体。

    Returns:
        统一响应体：刷新成功返回 success；失败返回 error_response。
    """
    # TikTok 店铺登录态常驻浏览器用户数据目录，无需刷新 Cookie（TIK-016）。
    # 该短路置于 owner_user_id 校验之前；告警链路（TIK-005）与两处触发调用一律不动。
    if payload.platform == "tiktok":
        return success_response(
            data={"shop_id": payload.shop_id, "shop_pk": payload.shop_pk, "skipped": True},
            message="TikTok 店铺登录态常驻浏览器目录，跳过 Cookie 刷新",
        )

    # owner_user_id 缺失无法定位账号凭据，直接返回失败（不抛异常）。
    if payload.owner_user_id is None:
        logger.warning("Cookie 刷新缺少归属用户 ID：shop_id=%s", payload.shop_id)
        return error_response(-1, "缺少归属用户信息，无法刷新 Cookie")

    # 按 (shop_id, owner_user_id) 定位账号名（复用共通凭据适配层）。
    credentials = load_account_credentials(payload.shop_id, payload.owner_user_id)
    if not credentials or not credentials[0]:
        logger.warning(
            "Cookie 刷新未找到账号凭据：shop_id=%s, user_id=%s",
            payload.shop_id,
            payload.owner_user_id,
        )
        return error_response(-1, "未找到账号凭据，无法刷新 Cookie")

    username = credentials[0]
    try:
        info = await pdd_login.refresh_pdd_cookies(username, payload.owner_user_id)
    except Exception as exc:  # noqa: BLE001 - 刷新异常不抛出，规整为失败响应
        logger.error("Cookie 刷新异常：shop_id=%s, %s", payload.shop_id, exc)
        # TIK-005：刷新异常触发告警链路（去重防抖，Webhook 暂未配置）。
        _alert_cookie_refresh_failed(
            payload.shop_pk, payload.shop_id, f"刷新异常：{exc}"
        )
        return error_response(-1, "Cookie 刷新失败，请稍后重试")

    if info is None:
        logger.warning("店铺 shop_id=%s 登录态已失效，Cookie 刷新失败", payload.shop_id)
        # TIK-005：登录态失效触发告警链路（去重防抖，Webhook 暂未配置）。
        _alert_cookie_refresh_failed(
            payload.shop_pk, payload.shop_id, "登录态已失效"
        )
        return error_response(-1, "登录态已失效，需重新登录")

    # 刷新成功：将新 Cookie 加密回写 account 表（不返回明文，需求 3.6）。
    update_account_cookies(
        payload.shop_id, payload.owner_user_id, info.get("cookies")
    )
    logger.info("店铺 shop_id=%s Cookie 刷新成功", payload.shop_id)
    # 仅返回非敏感的店铺标识信息，绝不外泄 Cookie 明文。
    return success_response(
        data={"shop_id": payload.shop_id, "shop_pk": payload.shop_pk},
        message="Cookie 刷新成功",
    )


__all__ = ["router"]
