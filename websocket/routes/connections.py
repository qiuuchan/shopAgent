# -*- coding: utf-8 -*-
"""
websocket.routes.connections —— 连接管理接口（供 backend 调用）
==============================================================
本文件用途：提供 websocket 服务的「连接断开」HTTP 接口，供 backend 服务在店铺停用
时经服务间 HTTP 调用断开其与拼多多的长连接（需求 3.5）。

- ``POST /connections/disconnect``：按 (shop_id, owner_user_id) 停止并注销该店铺的
  长连接（幂等：无活跃连接时视为已断开，返回成功）。

接口约定（开发规范 1-3）：HTTP 恒返回 200，业务成败由统一响应体
``{code, success, message, data}`` 表达。地址经环境变量配置（禁止写死 localhost，
规范 21）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from channel_base import PLATFORM_PDD
from channel_pdd import connection_manager, connection_registry
from channel_pdd.transfer_service import TransferService
from common.db.repository import Repository
from common.db.session import session_scope
from common.models.shop_models import Shop
from common.schemas.common import ApiResponse, error_response, success_response

logger = logging.getLogger("websocket.routes.connections")

# 连接管理路由：标签便于 OpenAPI 分组；前缀由聚合层添加。
router = APIRouter(tags=["连接管理"])


class ConnectRequest(BaseModel):
    """启动连接请求体（与 backend 店铺启用 / 连接约定一致）。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")
    shop_id: str = Field(..., description="店铺业务标识")
    owner_user_id: int = Field(..., description="店铺归属用户 ID")
    platform: str = Field(PLATFORM_PDD, description="平台标识（pdd/tiktok，缺省 pdd）")
    proxy_server: Optional[str] = Field(
        None,
        description="店铺出口代理服务器地址（Phase 2 前置；仅 tiktok 通道建浏览器"
        "会话时消费，空 / None 表示不走代理）",
    )


class DisconnectRequest(BaseModel):
    """断开连接请求体（与 backend connection_notify 约定一致）。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")
    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")


class CsListRequest(BaseModel):
    """客服列表查询请求体（与 backend transfer_client 约定一致，需求 16.1）。"""

    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")


class StatusRequest(BaseModel):
    """连接状态查询请求体（在线聊天展示连接状态用，需求 5.8）。"""

    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")


class StatusBatchItem(BaseModel):
    """批量连接状态查询的单个店铺项。"""

    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")


class StatusBatchRequest(BaseModel):
    """批量连接状态查询请求体（在线聊天店铺列表一次性查多店铺状态）。"""

    shops: list[StatusBatchItem] = Field(
        default_factory=list, description="待查询的店铺列表"
    )


def _load_browser_data_dir(shop_pk: int) -> Optional[str]:
    """按店铺主键读取 TikTok 登录态浏览器目录（Shop.browser_data_dir，幂等降级）。

    建店登录后该列被落库（登录态实际目录），连接时复用即可免二次登录；读取失败
    （如列缺失 / 记录不存在）返回 None，由 connection_manager 按 shop_pk 推导
    默认目录，行为与存量店铺一致。

    Args:
        shop_pk: 店铺主键 shop.id。

    Returns:
        登录态目录字符串；无 / 读取失败返回 None。
    """
    try:
        with session_scope() as session:
            shop = Repository(Shop, session).get(shop_pk)
            if shop is None:
                return None
            return str(getattr(shop, "browser_data_dir", None) or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - 读取失败降级为 None
        logger.warning("读取店铺登录态目录失败（按默认目录处理）: shop_pk=%s, %s", shop_pk, exc)
        return None


@router.post(
    "/connections/connect",
    response_model=ApiResponse,
    summary="启动指定店铺的拼多多长连接并装配消息处理全链路",
)
async def connect_connection(payload: ConnectRequest) -> ApiResponse:
    """启动指定店铺连接并串联消息处理全链路（需求 5.1 / 5.3，任务 19.2）。

    经 ``connection_manager.start_channel`` 创建 PDDChannel 并绑定 MessageConsumer，
    完成「收消息 → 解析入队 → 决策链 → 知识库/AI → 发送回复 → 记日志/通知」装配，
    随后登记到连接注册表供断连 / 状态查询定位。TikTok 店铺连接时自动复用建店登录
    时落库的登录态目录（免二次登录）。

    Args:
        payload: 含 shop_pk / shop_id / owner_user_id 的启动请求体。

    Returns:
        统一响应体：启动成功返回 success；异常规整为失败响应（不抛出）。
    """
    try:
        await connection_manager.start_channel(
            payload.shop_id,
            payload.shop_pk,
            payload.owner_user_id,
            platform=payload.platform,
            proxy_server=payload.proxy_server,
            browser_data_dir=_load_browser_data_dir(payload.shop_pk),
        )
    except Exception as exc:  # noqa: BLE001 - 启动异常不抛出，规整为失败响应
        logger.error("启动店铺连接异常: shop_id=%s, %s", payload.shop_id, exc)
        return error_response(-1, "启动店铺连接失败，请稍后重试")
    return success_response(message="已启动店铺连接")


@router.post(
    "/connections/disconnect",
    response_model=ApiResponse,
    summary="断开指定店铺的拼多多长连接",
)
async def disconnect_connection(payload: DisconnectRequest) -> ApiResponse:
    """断开指定店铺的拼多多长连接（需求 3.5，幂等）。

    Args:
        payload: 含 shop_pk / shop_id / owner_user_id 的断开请求体。

    Returns:
        统一响应体：断开成功（或本就无连接）返回成功；停止出错返回失败提示。
    """
    ok = await connection_registry.disconnect(payload.shop_id, payload.owner_user_id)
    if ok:
        return success_response(message="已断开店铺连接")
    return error_response(-1, "断开店铺连接失败")


@router.post(
    "/connections/cs-list",
    response_model=ApiResponse,
    summary="查询指定店铺可分配的人工客服列表",
)
async def cs_list(payload: CsListRequest) -> ApiResponse:
    """查询指定店铺可分配的人工客服列表（需求 16.1）。

    复用 ``TransferService.get_cs_list`` 按 (shop_id, owner_user_id) 加载 Cookie 后
    调用拼多多接口获取客服列表（客服标识与名称）。底层为同步 HTTP，丢入线程池执行
    避免阻塞事件循环；查询失败 / 异常一律规整为失败响应，不抛出（健壮性兜底）。

    Args:
        payload: 含 shop_id / owner_user_id 的客服列表查询请求体。

    Returns:
        统一响应体：成功返回 data={list: 客服列表}；失败返回中文提示。
    """
    try:
        service = TransferService(
            shop_id=payload.shop_id,
            user_id=payload.owner_user_id,
        )
        result = await asyncio.to_thread(service.get_cs_list)
    except Exception as exc:  # noqa: BLE001 - 查询异常不抛出，规整为失败响应
        logger.error("查询客服列表异常: shop_id=%s, %s", payload.shop_id, exc)
        return error_response(-1, "查询客服列表失败，请稍后重试")

    if result is None:
        return error_response(-1, "查询客服列表失败，请稍后重试")
    return success_response(data={"list": result}, message="查询成功")


@router.post(
    "/connections/status",
    response_model=ApiResponse,
    summary="查询指定店铺的拼多多连接状态",
)
async def connection_status(payload: StatusRequest) -> ApiResponse:
    """查询指定店铺当前是否已建立活跃的拼多多长连接（需求 5.8）。

    Args:
        payload: 含 shop_id / owner_user_id 的状态查询请求体。

    Returns:
        统一响应体：data={connected: bool}。
    """
    connected = connection_registry.is_connected(
        payload.shop_id, payload.owner_user_id
    )
    return success_response(data={"connected": connected}, message="查询成功")


@router.post(
    "/connections/status-batch",
    response_model=ApiResponse,
    summary="批量查询多个店铺的拼多多连接状态",
)
async def connection_status_batch(payload: StatusBatchRequest) -> ApiResponse:
    """批量查询多个店铺的连接状态（在线聊天店铺列表一次性查询，避免逐个 HTTP）。

    Args:
        payload: 含 shops 列表（每项 shop_id / owner_user_id）的批量请求体。

    Returns:
        统一响应体：data={statuses: [{shop_id, connected}]}。
    """
    statuses = [
        {
            "shop_id": item.shop_id,
            "connected": connection_registry.is_connected(
                item.shop_id, item.owner_user_id
            ),
        }
        for item in payload.shops
    ]
    return success_response(data={"statuses": statuses}, message="查询成功")


__all__ = ["router"]
