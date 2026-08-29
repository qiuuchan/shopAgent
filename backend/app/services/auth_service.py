# -*- coding: utf-8 -*-
"""
backend.app.services.auth_service —— 认证业务服务
=================================================
本文件用途：实现 backend 服务的认证业务逻辑，供 auth 路由与鉴权依赖复用，
满足需求 1（用户认证与会话）：

- ``login(username, password)``：校验用户名与密码，成功签发 JWT 并返回
  {token, user}（脱敏）统一响应体；用户名或密码错误返回 success=false、
  message「用户名或密码错误」（需求 1.1/1.2）。
- ``logout(token)``：解析当前令牌并将其 ``jti`` 记入失效登记表，使该令牌
  失效（需求 1.5）。
- ``serialize_user(user)``：将用户模型脱敏序列化为对外可返回的字典
  （不含 password_hash 等敏感字段，需求 1.6）。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3）。
- 所有数据访问经 common.db.repository 的参数化查询，禁止拼接 SQL（规范 16）。
- 密码哈希校验与 JWT 签发 / 校验复用 common.utils.security（规范 36/52）。
- 时间统一北京时间（规范 17 / 需求 24.8）。
- 被停用用户（status=0，逻辑删除）不允许登录（需求 2.7/2.8）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.business_codes import (
    CODE_LOGIN_FAILED,
    CODE_PARAM_ERROR,
    MSG_ACCOUNT_DISABLED,
    MSG_LOGIN_FAILED,
)
from app.core.token_blacklist import get_token_blacklist
from app.services import captcha_service, email_code_service, setting_service
from common.db.repository import Repository
from common.models.user_models import SysRole, SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.schemas.sanitize import sanitize_sensitive
from common.utils.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from common.utils.time_utils import utc_to_beijing

# 用户启用状态值（与 SysUser.status 约定一致：1=启用，0=停用）。
_USER_STATUS_ENABLED: int = 1

# 注册密码最小长度（与前端校验口径一致）。
_MIN_PASSWORD_LENGTH: int = 6


def serialize_user(user: SysUser, role: Optional[SysRole] = None) -> Dict[str, Any]:
    """将用户模型脱敏序列化为对外可返回的字典（需求 1.6）。

    仅输出展示所需字段（用户名、角色、状态、联系方式等），并经
    sanitize_sensitive 二次脱敏，确保不外泄 password_hash 等敏感字段。

    Args:
        user: 用户模型实例。
        role: 该用户所属角色（可选），用于附带角色名与是否管理员。

    Returns:
        脱敏后的用户信息字典。
    """
    info: Dict[str, Any] = {
        "id": user.id,
        "username": user.username,
        "role_id": user.role_id,
        "status": user.status,
        "wechat": user.wechat,
        "qq": user.qq,
    }
    if role is not None:
        info["role_name"] = role.role_name
        info["is_admin"] = bool(role.is_admin)
    # 二次脱敏：即便上方未显式包含敏感字段，也统一过滤，避免遗漏。
    return sanitize_sensitive(info)


def login(
    session: Session,
    username: str,
    password: str,
    captcha_ticket: Optional[str] = None,
) -> ApiResponse:
    """校验用户名与密码并签发登录令牌（需求 1.1/1.2）。

    成功：返回 success=true、data={token, user}（user 已脱敏）。
    失败（用户不存在 / 密码错误）：返回 success=false、message「用户名或密码
    错误」，业务码 40001；HTTP 由路由层恒返回 200。被停用用户拒绝登录。

    若系统设置开启「登录验证码」（需求 21.6），先校验滑块验证票据
    ``captcha_ticket``：缺失 / 失效返回中文提示，校验通过后票据立即作废（防重放）。

    Args:
        session: 数据库会话。
        username: 登录用户名。
        password: 登录明文密码。
        captcha_ticket: 滑块验证通过后签发的一次性票据（开启验证码时必填）。

    Returns:
        统一响应体 ApiResponse。
    """
    # 开启登录验证码时：先校验并消费滑块验证票据（防止绕过滑块直接登录）。
    if setting_service.is_login_captcha_enabled(session):
        if not captcha_service.consume_ticket(captcha_ticket):
            return error_response(CODE_PARAM_ERROR, "请先完成滑块验证")

    # 入参兜底：空用户名 / 空密码一律视为登录失败（不泄露具体原因）。
    if not username or not password:
        return error_response(CODE_LOGIN_FAILED, MSG_LOGIN_FAILED)

    user_repo = Repository(SysUser, session)
    user = user_repo.get_by(username=username)

    # 用户不存在：返回统一的「用户名或密码错误」，不区分是哪一项错误，避免
    # 暴露「用户名是否存在」信息。
    if user is None:
        return error_response(CODE_LOGIN_FAILED, MSG_LOGIN_FAILED)

    # 密码校验：明文与存储哈希比对（不返回任何哈希 / 明文）。
    if not verify_password(password, user.password_hash):
        return error_response(CODE_LOGIN_FAILED, MSG_LOGIN_FAILED)

    # 停用用户拒绝登录（逻辑删除 status=0，需求 2.7/2.8）。
    if user.status != _USER_STATUS_ENABLED:
        return error_response(CODE_LOGIN_FAILED, MSG_ACCOUNT_DISABLED)

    # 读取角色信息（用于令牌声明与返回展示）。
    role: Optional[SysRole] = None
    if user.role_id is not None:
        role = Repository(SysRole, session).get(user.role_id)

    # 签发 JWT：sub 为用户 ID，附带用户名与角色作为自定义声明，便于鉴权后
    # 直接获取身份上下文；过期时间取配置默认值。
    extra_claims: Dict[str, Any] = {"username": user.username}
    if user.role_id is not None:
        extra_claims["role_id"] = user.role_id
    if role is not None:
        extra_claims["is_admin"] = bool(role.is_admin)

    token = create_access_token(subject=user.id, extra_claims=extra_claims)

    return success_response(
        data={"token": token, "user": serialize_user(user, role)},
        message="登录成功",
    )


def register(
    session: Session,
    username: str,
    password: str,
    email: Optional[str] = None,
    verification_code: Optional[str] = None,
    session_id: Optional[str] = None,
) -> ApiResponse:
    """用户自助注册（需求 2 / 规范 41，参照 xianyu-auto-reply-wangpan 注册流程）。

    完整校验链（任一不通过即返回中文原因，HTTP 恒 200）：
    1. 「允许用户注册」开关关闭时直接拒绝（需求 21.6）；
    2. 用户名 / 密码非空，密码长度不少于 6 位，用户名全局唯一；
    3. 邮箱格式合法且全局唯一（库层最终校验，防并发）；
    4. 邮箱验证码正确且场景匹配（register），校验通过后即作废（防重放）；
    5. 分配「注册默认角色」（is_default，非管理员），绝不分配管理员角色。

    注册成功后不自动登录，返回 success=true、message「注册成功，请登录」，由前端
    跳转登录页（与参照项目一致）。

    Args:
        session: 数据库会话。
        username: 注册用户名。
        password: 注册明文密码（仅用于哈希，不落库明文）。
        email: 注册邮箱（邮箱验证码归属校验，全局唯一）。
        verification_code: 邮箱验证码。
        session_id: 前端会话标识（图形验证码流程使用，注册接口不强依赖）。

    Returns:
        统一响应体：成功返回 success=true（data=None）；失败返回中文原因。
    """
    # 1) 注册开关：关闭时一律拒绝（前端亦会拦截，此处为后端兜底）。
    if not setting_service.is_register_allowed(session):
        return error_response(CODE_PARAM_ERROR, "注册功能已关闭，请联系管理员")

    # 2) 用户名 / 密码基础校验。
    if not username or not username.strip():
        return error_response(CODE_PARAM_ERROR, "用户名不能为空")
    if not password:
        return error_response(CODE_PARAM_ERROR, "密码不能为空")
    if len(password) < _MIN_PASSWORD_LENGTH:
        return error_response(CODE_PARAM_ERROR, f"密码长度不能少于 {_MIN_PASSWORD_LENGTH} 位")

    # 3) 邮箱格式校验。
    email = (email or "").strip()
    if not email_code_service.is_valid_email(email):
        return error_response(CODE_PARAM_ERROR, "请输入正确的邮箱地址")

    # 4) 邮箱验证码存在性校验（实际校验放在最后，避免其它校验失败时白白消耗验证码）。
    if not verification_code or not verification_code.strip():
        return error_response(CODE_PARAM_ERROR, "请输入邮箱验证码")

    username = username.strip()
    user_repo = Repository(SysUser, session)
    # 用户名全局唯一。
    if user_repo.get_by(username=username) is not None:
        return error_response(CODE_PARAM_ERROR, "用户名已存在")
    # 邮箱全局唯一（库层最终校验，防并发；规范 10 由代码控制唯一性）。
    if user_repo.get_by(email=email) is not None:
        return error_response(CODE_PARAM_ERROR, "该邮箱已被注册")

    # 5) 分配注册默认角色（非管理员）；默认角色须启用且存在，否则拒绝注册。
    role_repo = Repository(SysRole, session)
    default_role = role_repo.get_by(is_default=True, status=_USER_STATUS_ENABLED)
    if default_role is None:
        return error_response(CODE_PARAM_ERROR, "系统尚未配置默认注册角色，请联系管理员")

    # 6) 邮箱验证码校验（场景 register），通过后即作废（最后一步，校验通过即创建）。
    code_ok, code_msg = email_code_service.verify_code(
        email, verification_code, "register"
    )
    if not code_ok:
        return error_response(CODE_PARAM_ERROR, code_msg)

    user_repo.create(
        username=username,
        password_hash=hash_password(password),
        email=email,
        role_id=default_role.id,
        status=_USER_STATUS_ENABLED,
    )

    # 注册成功不自动登录，提示前端跳转登录页（与参照项目一致）。
    return success_response(data=None, message="注册成功，请登录")


def logout(token: str) -> ApiResponse:
    """使当前登录令牌失效（需求 1.5）。

    解析令牌取得其 ``jti`` 与过期时间，记入进程内失效登记表；此后携带该令牌
    的请求将被鉴权依赖判定为「未登录或登录已过期」。无论令牌是否有效，均返回
    成功（登出语义具幂等性，避免泄露令牌状态）。

    Args:
        token: 当前访问令牌（可能为空或已失效）。

    Returns:
        统一响应体 ApiResponse（恒 success=true）。
    """
    if token:
        payload = decode_access_token(token)
        if payload:
            jti = payload.get("jti")
            if jti:
                # exp 为 Unix 时间戳（UTC），转换为北京时间登记，便于惰性清理。
                expire_at: Optional[datetime] = None
                exp_ts = payload.get("exp")
                if isinstance(exp_ts, (int, float)):
                    expire_at = utc_to_beijing(
                        datetime.fromtimestamp(exp_ts, tz=timezone.utc)
                    ).replace(tzinfo=None)
                get_token_blacklist().revoke(jti, expire_at)
    return success_response(data=None, message="已登出")


__all__ = [
    "serialize_user",
    "login",
    "register",
    "logout",
]
