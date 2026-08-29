# -*- coding: utf-8 -*-
"""
backend.app.api.routes.profile —— 个人设置接口路由
==================================================
本文件用途：提供 backend 服务的「个人设置」REST 接口，满足需求 22：

- ``GET /profile``            查询当前用户账户信息（用户名、角色只读展示，
  需求 22.1）。
- ``PUT /profile/password``   修改当前用户密码：校验当前密码（需求 22.2），
  当前密码错误返回 success=false、message「当前密码错误」（需求 22.3）；
  成功则哈希更新密码并使当前登录令牌失效（需求 22.5）。
- ``PUT /profile/contact``    保存个人联系方式（微信、QQ），按用户维度持久化
  与隔离（需求 22.6/22.7）。

接口约定（开发规范 1-3）：
- 所有接口 HTTP 恒返回 200，业务成败由统一响应体 {code, success, message,
  data} 表达。
- 个人设置无需额外资源权限：任意已登录用户均可管理「本人」账户；以用户维度
  隔离——所有读写均作用于鉴权得到的当前用户自身记录，不接受外部目标用户 ID
  （需求 22.7）。
- 业务逻辑委托 app.services.profile_service，路由层仅负责入参解析、依赖注入与
  令牌失效编排；数据库会话经 common.db.session.get_db 注入。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_token, get_current_user
from app.core import permission
from app.core.token_blacklist import get_token_blacklist
from app.services import profile_service
from common.db.session import get_db
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, success_response
from common.utils.security import decode_access_token
from common.utils.time_utils import utc_to_beijing

# 个人设置路由：标签「个人设置」便于 OpenAPI 文档分组；前缀由聚合层统一添加。
router = APIRouter(tags=["个人设置"])


class ChangePasswordRequest(BaseModel):
    """修改密码请求体：当前密码 + 新密码。"""

    current_password: str = Field(..., description="当前密码（明文）")
    new_password: str = Field(..., description="新密码（明文，长度不少于 6 位）")


class ContactRequest(BaseModel):
    """个人联系方式请求体：微信、QQ（均可选，None 表示不修改该字段）。"""

    wechat: Optional[str] = Field(None, description="微信号（空字符串表示清空）")
    qq: Optional[str] = Field(None, description="QQ 号（空字符串表示清空）")


@router.get("/profile", response_model=ApiResponse, summary="查询个人账户信息")
def get_profile(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询当前用户账户信息（需求 22.1）。用户名与角色由前端只读展示。"""
    return profile_service.get_profile(db, current_user)


@router.get(
    "/me/menu-resources",
    response_model=ApiResponse,
    summary="查询当前用户菜单授权资源",
)
def get_my_menu_resources(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """返回当前用户「是否管理员」与「被授予 view 的资源键列表」（需求 2.6）。

    前端据此按菜单所需资源键过滤可见菜单：管理员（is_admin=True）放行全部；普通
    用户仅渲染被授予 view 的资源对应菜单，使菜单可见性与接口级判权一致，避免出现
    「能看到菜单但点进去无权限」。任意已登录用户均可查询本人授权（无需额外权限）。
    """
    is_admin, resources = permission.granted_view_resources(current_user, session=db)
    return success_response(
        data={"is_admin": is_admin, "resources": resources},
        message="查询成功",
    )


@router.put("/profile/password", response_model=ApiResponse, summary="修改密码")
def change_password(
    payload: ChangePasswordRequest,
    token: str = Depends(get_current_token),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """修改当前用户密码（需求 22.2/22.3/22.5）。

    校验当前密码：错误返回「当前密码错误」（需求 22.3）；成功则哈希更新密码
    并使当前登录令牌失效（需求 22.5），前端据此引导用户重新登录。
    """
    result = profile_service.change_password(
        db,
        current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    # 仅在修改成功时使当前登录令牌失效（需求 22.5）。
    if result.success:
        _revoke_current_token(token)
    return result


@router.put("/profile/contact", response_model=ApiResponse, summary="保存个人联系方式")
def update_contact(
    payload: ContactRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """保存个人联系方式（微信、QQ），按用户维度持久化与隔离（需求 22.6/22.7）。"""
    return profile_service.update_contact(
        db,
        current_user,
        wechat=payload.wechat,
        qq=payload.qq,
    )


def _revoke_current_token(token: str) -> None:
    """将当前登录令牌记入失效登记表，使其立即失效（需求 22.5）。

    复用与登出一致的失效机制：解析令牌取得 ``jti`` 与过期时间，登记后该令牌
    再访问受保护接口将被判定为「未登录或登录已过期」。

    Args:
        token: 当前访问令牌。
    """
    if not token:
        return
    payload = decode_access_token(token)
    if not payload:
        return
    jti = payload.get("jti")
    if not jti:
        return
    # exp 为 Unix 时间戳（UTC），转换为北京时间登记，便于惰性清理。
    expire_at: Optional[datetime] = None
    exp_ts = payload.get("exp")
    if isinstance(exp_ts, (int, float)):
        expire_at = utc_to_beijing(
            datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        ).replace(tzinfo=None)
    get_token_blacklist().revoke(jti, expire_at)


__all__ = ["router"]
