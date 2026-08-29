# -*- coding: utf-8 -*-
"""
backend.app.api.routes.dashboard —— 仪表盘与数据分析接口路由
============================================================
本文件用途：提供 backend 服务的「仪表盘与数据分析」REST 接口，满足需求 20
（仪表盘与数据分析）：

- ``GET /dashboard/overview`` 仪表盘关键指标（需求 20.1）：在线店铺数、今日消息数、
  今日自动回复数、AI 回复数、风控触发数（北京时间口径，需求 20.3）。
- ``GET /dashboard/trend``    数据分析趋势（需求 20.2）：指定时间范围内按天聚合的
  消息量与回复量趋势（北京时间口径，需求 20.3）。
- ``GET /dashboard/first-response`` 首响时长统计（TIK-025）：按平台 / 店铺 / 日期
  范围统计首响时长分布、超 5 分钟占比与回复率，口径与 TIK-018 验收对账工具一致。

权限控制（需求 2.4）：所有接口先经统一权限模块 ``permission.check`` 判断当前用户
对资源 ``dashboard`` 的 ``view`` 操作是否被授权；未授权返回 success=false、
message「无访问权限」的统一响应体（HTTP 恒 200）。数据范围隔离在服务层统一处理
（非管理员仅统计本人 / 被授权店铺数据，需求 3.7）。

接口约定（开发规范 1-3）：所有接口 HTTP 恒返回 200，业务成败由统一响应体
{code, success, message, data} 表达；业务逻辑委托 app.services.dashboard_service
与 app.services.first_response_service，路由层仅负责入参解析、鉴权依赖与权限
判断；数据库会话经 common.db.session.get_db 注入。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import permission
from app.core.business_codes import CODE_FORBIDDEN, MSG_FORBIDDEN
from app.services import dashboard_service, first_response_service
from common.db.session import get_db
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response

# 仪表盘路由：标签「仪表盘与数据分析」便于 OpenAPI 文档分组；前缀由聚合层添加。
router = APIRouter(tags=["仪表盘与数据分析"])

# 受保护资源键约定（与 sys_permission.resource_key 对齐）。
RESOURCE_DASHBOARD: str = "dashboard"


def _ensure_permission(
    user: SysUser, action: str, session: Session
) -> Optional[ApiResponse]:
    """统一权限校验：未授权时返回「无访问权限」响应，授权时返回 None。

    依据需求 2.4，集中经 ``permission.check`` 判断对 ``dashboard`` 资源的操作权限。

    Args:
        user: 当前登录用户。
        action: 操作（view）。
        session: 数据库会话。

    Returns:
        未授权时返回失败响应体；已授权返回 None。
    """
    if permission.check(user, RESOURCE_DASHBOARD, action, session=session):
        return None
    return error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)


@router.get(
    "/dashboard/overview",
    response_model=ApiResponse,
    summary="仪表盘关键指标",
)
def get_overview(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """返回仪表盘关键指标（需求 20.1，按北京时间口径，需求 20.3）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return dashboard_service.get_overview(db, current_user)


@router.get(
    "/dashboard/trend",
    response_model=ApiResponse,
    summary="数据分析趋势（按天聚合）",
)
def get_trend(
    start_date: Optional[str] = Query(
        None, description="起始日期（YYYY-MM-DD），缺省为最近 7 天起"
    ),
    end_date: Optional[str] = Query(
        None, description="结束日期（YYYY-MM-DD），缺省为今日"
    ),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """返回时间范围内按天聚合的消息量与回复量趋势（需求 20.2，北京时间口径 20.3）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return dashboard_service.get_trend(
        db, current_user, start_date=start_date, end_date=end_date
    )


@router.get(
    "/dashboard/first-response",
    response_model=ApiResponse,
    summary="首响时长统计（分布 / 超 5 分钟占比 / 回复率）",
)
def get_first_response(
    start_date: Optional[str] = Query(
        None, description="起始日期（YYYY-MM-DD），缺省为最近 7 天起"
    ),
    end_date: Optional[str] = Query(
        None, description="结束日期（YYYY-MM-DD），缺省为今日"
    ),
    platform: Optional[str] = Query(
        None, description="平台筛选：pdd=拼多多 / tiktok=TikTok Shop，缺省全部平台"
    ),
    shop_pk: Optional[int] = Query(
        None, description="店铺主键筛选，缺省为数据范围内全部店铺"
    ),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """返回首响时长分布、超 5 分钟占比与回复率（TIK-025，北京时间口径）。

    口径与 TIK-018 验收对账工具（tools/tiktok_acceptance/reconcile.py）一致：
    首响时长 = 本店首次回复时间 - 该段第一条买家消息时间，>300 秒计超时；窗口结束
    仍无回复的计「待回复」，超时与待回复均计入回复率分母并计为未达标。
    """
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return first_response_service.get_first_response_stats(
        db,
        current_user,
        start_date=start_date,
        end_date=end_date,
        platform=platform,
        shop_pk=shop_pk,
    )


__all__ = ["router"]
