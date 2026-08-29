# -*- coding: utf-8 -*-
"""
backend.app.api.routes.internal_notify —— 系统事件通知内部接收路由（websocket 服务回调）
==========================================================================================
本文件用途：接收 **websocket** 服务经内部接口推送的系统事件（连接断开 / 登录态失效），
转交 ``notify_service.push_system_event`` 按店铺已启用通知渠道（企微群机器人等）推送
并落 ``pdd_notify_record``（需求 18.3 / 18.4）。

背景（TIK-018 实测补充）：websocket 侧告警通知器长期 ``send_cb=None``（仅日志占位），
告警无法到达企微。本路由补齐服务间链路：websocket ``X-Internal-Token`` 鉴权后调用，
系统内部调用 ``operator_id=None`` 不做用户级数据隔离（与 push_system_event 约定一致）。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、统一响应体（1-3）、
内部接口密钥鉴权（与 internal_chat 同款模式）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.services import notify_service
from common.core.config import get_settings
from common.db.session import get_db
from common.schemas.common import ApiResponse, error_response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["内部接口"])


class InternalNotifyEventRequest(BaseModel):
    """系统事件通知内部推送请求体（websocket 服务推送）。"""

    event_type: str = Field(..., description="事件类型（connection_disconnected/login_expired/risk_triggered）")
    content: str = Field(..., description="通知内容（中文）")
    shop_pk: Optional[int] = Field(None, description="事件归属店铺主键（店铺级通知）")


@router.post(
    "/internal/notify-events",
    response_model=ApiResponse,
    summary="接收 websocket 服务推送的系统事件并经通知渠道发送",
)
def receive_notify_event(
    payload: InternalNotifyEventRequest,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """接收一条系统事件通知并经该店铺全部已启用渠道推送（企微群机器人等）。

    Args:
        payload: 系统事件请求体。
        x_internal_token: 服务间共享密钥（请求头 X-Internal-Token）。
        db: 数据库会话。

    Returns:
        统一响应体：data 为推送统计 {total, success, failed}；密钥不符返回失败。
    """
    expected = get_settings().internal_service_token
    if not x_internal_token or x_internal_token != expected:
        logger.warning("内部通知事件接口鉴权失败：来源密钥不符")
        return error_response(-1, "无权访问")

    return notify_service.push_system_event(
        db,
        event_type=payload.event_type,
        content=payload.content,
        shop_pk=payload.shop_pk,
        operator_id=None,
    )
