# -*- coding: utf-8 -*-
"""
backend.app.api.routes.business_hours —— 营业时间配置接口路由
============================================================
本文件用途：提供 backend 服务的「营业时间配置」REST 接口，满足需求 11.1
（营业时间控制 —— 配置起止时刻并持久化）：

- ``PUT /shops/{shop_pk}/business-hours``  配置 / 更新店铺营业时间起止时刻
  （按店铺 upsert，幂等，需求 11.1）。
- ``GET /shops/{shop_pk}/business-hours``  查询店铺营业时间配置；未配置返回
  data=null（业务侧默认全天，需求 11.4）。

权限控制（需求 2.4）：所有接口先经统一权限模块 ``permission.check`` 判断当前
用户对资源 ``business_hours`` 的对应操作（view / update）是否被授权；未授权时
返回 ``success=false``、``message``「无访问权限」的统一响应体（HTTP 恒 200）。

接口约定（开发规范 1-3）：
- 所有接口 HTTP 恒返回 200，业务成败由统一响应体 {code, success, message,
  data} 表达。
- 业务逻辑委托 app.services.business_hours_service，路由层仅负责入参解析、
  鉴权依赖与权限判断；数据库会话经 common.db.session.get_db 注入。
- 时间字段按北京时间口径处理（规范 17 / 需求 24.8）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import permission
from app.core.business_codes import CODE_FORBIDDEN, MSG_FORBIDDEN
from app.services import business_hours_service
from common.db.session import get_db
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response

# 营业时间配置路由：标签「营业时间」便于 OpenAPI 文档分组；前缀由聚合层添加。
router = APIRouter(tags=["营业时间"])

# 受保护资源键：营业时间为「店铺级设置」，统一归属店铺管理（shop）资源判权——
# 入口收敛到店铺管理页后，有店铺管理权限即可操作店铺级设置（与前端入口一致）。
RESOURCE_BUSINESS_HOURS: str = "shop"


def _ensure_permission(
    user: SysUser,
    action: str,
    session: Session,
) -> Optional[ApiResponse]:
    """统一权限校验：未授权时返回「无访问权限」响应，授权时返回 None。

    依据需求 2.4，集中经 ``permission.check`` 判断对 ``business_hours`` 资源的
    操作权限；未授权返回 success=false、message「无访问权限」的统一响应体。

    Args:
        user: 当前登录用户。
        action: 操作（view / update）。
        session: 数据库会话。

    Returns:
        未授权时返回失败响应体；已授权返回 None。
    """
    if permission.check(user, RESOURCE_BUSINESS_HOURS, action, session=session):
        return None
    return error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)


class BusinessHoursRequest(BaseModel):
    """营业时间配置请求体。

    起止时刻按「HH:MM」或「HH:MM:SS」字符串提交（北京时间口径），允许为空
    表示不设置该时刻；均为空表示未配置（业务侧默认全天，需求 11.4）。
    """

    start_time: Optional[str] = Field(
        None, description="营业开始时刻（HH:MM 或 HH:MM:SS，北京时间；空表示不设置）"
    )
    end_time: Optional[str] = Field(
        None, description="营业结束时刻（HH:MM 或 HH:MM:SS，北京时间；空表示不设置，可跨午夜）"
    )
    weekdays: Optional[str] = Field(
        None, description="营业星期维度（逗号分隔 1~7，如 '1,2,3,4,5' 表示周一至周五；空表示每天）"
    )
    enabled: bool = Field(True, description="是否启用该营业时间配置")


@router.put(
    "/shops/{shop_pk}/business-hours",
    response_model=ApiResponse,
    summary="配置 / 更新店铺营业时间",
)
def configure_business_hours(
    shop_pk: int,
    payload: BusinessHoursRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """配置并持久化店铺营业时间起止时刻（需求 11.1）。

    按店铺主键 upsert，同一店铺仅一条配置，重复配置覆盖更新（幂等）。
    """
    denied = _ensure_permission(current_user, "update", db)
    if denied is not None:
        return denied
    return business_hours_service.configure_business_hours(
        db,
        shop_pk=shop_pk,
        start_time=payload.start_time,
        end_time=payload.end_time,
        weekdays=payload.weekdays,
        enabled=payload.enabled,
        operator_id=current_user.id,
    )


@router.get(
    "/shops/{shop_pk}/business-hours",
    response_model=ApiResponse,
    summary="查询店铺营业时间",
)
def get_business_hours(
    shop_pk: int,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询店铺营业时间配置（需求 11.1 配套）。未配置返回 data=null。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return business_hours_service.get_business_hours(
        db, shop_pk=shop_pk, operator_id=current_user.id
    )


__all__ = ["router"]
