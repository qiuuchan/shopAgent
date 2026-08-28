# -*- coding: utf-8 -*-
"""
backend.app.services.account_service —— 店铺与账号管理业务服务
==============================================================
本文件用途：实现 backend 服务的「拼多多账号与店铺管理」业务逻辑，供 shops 路由
复用，满足需求 3（拼多多账号与店铺管理）：

- ``upsert_shop(...)``：新增 / 更新店铺（需求 3.1 / 3.2）。按业务键
  （owner_user_id + shop_id）upsert 幂等：已存在则更新，不新建重复记录；同时
  以可逆加密存储 Cookie 凭据与账号密码（需求 3.6）。
- ``update_shop(...)``：修改店铺备注 / 启用状态 / 关联配置（需求 3.4）。
- ``disable_shop(...)``：停用店铺（需求 3.5）。状态字段逻辑删除（禁止物理删除），
  并经 HTTP 通知 websocket 服务断开其拼多多连接。
- ``list_shops(...)``：店铺列表（需求 3.3 / 3.7）。按北京时间（created_at）倒序、
  后端分页（默认 20，可选 10/20/50/100），并做数据范围隔离（非管理员仅见本人 /
  被授权店铺）；列表不返回 Cookie 明文（需求 3.6）。
- ``get_shop(...)``：查询单个店铺详情（同样脱敏、受数据范围隔离约束）。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3）。
- 所有数据访问经 common.db.repository 参数化查询，禁止拼接 SQL（规范 16）。
- Cookie / 密码以 common.utils.crypto 可逆加密存储；对外响应经
  common.schemas.sanitize 脱敏，列表 / 详情绝不返回明文（需求 3.6）。
- 数据范围隔离统一经 app.core.data_scope（规范 42 集中判权）。
- 禁止物理删除业务数据，停用经状态字段实现（规范 11 / 需求 3.5 / 24.6）。
- 时间统一北京时间（规范 17 / 需求 24.8）；导入置顶（规范 51）；中文注释（规范 37）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.business_codes import CODE_NOT_FOUND, CODE_PARAM_ERROR
from app.core.data_scope import (
    DataScope,
    build_data_scope,
    is_in_scope,
)
from app.services.connection_notify import notify_connect, notify_disconnect
from app.services.shop_login_client import (
    import_by_cookie as login_import_by_cookie,
)
from app.services.shop_login_client import (
    login_by_password as login_with_password,
)
from common.db.repository import Repository
from common.models.shop_models import Account, Shop
from common.schemas.common import ApiResponse, error_response, success_response
from common.schemas.sanitize import sanitize_sensitive
from common.utils.crypto import encrypt_text, try_decrypt_text
from common.utils.time_utils import safe_isoformat

# 店铺所属平台的合法枚举（与 sys_dict 的 platform 字典、Shop.platform 列一致）。
# 默认 'pdd'，向后兼容存量店铺（TIK-002 已将存量店铺归 'pdd'，TIK-003 全链路透传）。
VALID_PLATFORMS: tuple[str, ...] = ("pdd", "tiktok")
DEFAULT_PLATFORM: str = "pdd"

# 店铺启停用状态值（与 Shop.status 约定一致：1=启用，0=停用）。
SHOP_STATUS_ENABLED: int = 1
SHOP_STATUS_DISABLED: int = 0


def validate_platform(platform: Optional[str]) -> str:
    """校验并返回合法的店铺所属平台枚举。

    空 / None 归一为默认平台 ``'pdd'``（向后兼容）；非合法枚举抛 ``ValueError``，
    由路由层捕获后转 ``BusinessError``（归 ``CODE_PARAM_ERROR``，不抛 4xx/5xx）。

    Args:
        platform: 平台标识（'pdd' / 'tiktok'），或空 / None 表示沿用默认。

    Returns:
        合法平台字符串；空输入返回 ``DEFAULT_PLATFORM``。

    Raises:
        ValueError: 当 platform 为非空但不在合法枚举内时抛出。
    """
    if platform is None or not str(platform).strip():
        return DEFAULT_PLATFORM
    value = str(platform).strip()
    if value not in VALID_PLATFORMS:
        raise ValueError(f"非法的平台标识：{value!r}（仅允许 {VALID_PLATFORMS}）")
    return value


# 店铺出口代理允许的协议前缀（Phase 2 前置，店铺级代理字段校验）。
# 仅支持浏览器可直接消费的代理协议：http(s) 正向代理与 socks5(5h)；
# 地址须含 host:port（Playwright --proxy-server 参数语义）。
_PROXY_ALLOWED_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "socks5://",
    "socks5h://",
)


def validate_proxy_server(proxy_server: Optional[str]) -> Optional[str]:
    """校验并返回合法的店铺出口代理服务器地址。

    空 / None 归一为 None（不走代理，默认关闭）；非空时须以支持的协议前缀
    （http(s):// / socks5(5h)://）开头且带 host:port，否则抛 ``ValueError``，
    由路由层捕获后转 ``BusinessError``（归 ``CODE_PARAM_ERROR``，不抛 4xx/5xx）。

    Args:
        proxy_server: 代理服务器地址（如 ``http://127.0.0.1:7890``），或空 / None。

    Returns:
        合法代理地址字符串；空输入返回 None（不走代理）。

    Raises:
        ValueError: 当代理地址非空但协议前缀非法或缺少 host:port 时抛出。
    """
    if proxy_server is None or not str(proxy_server).strip():
        return None
    value = str(proxy_server).strip()
    lowered = value.lower()
    matched_prefix = next(
        (p for p in _PROXY_ALLOWED_PREFIXES if lowered.startswith(p)), None
    )
    if matched_prefix is None:
        raise ValueError(
            f"代理地址协议不支持：{value!r}（仅允许 http(s):// 与 socks5(5h)://）"
        )
    # host:port 校验：协议前缀之后须非空且含端口分隔（host 或 host:port 均可由
    # Playwright 解析，但为尽早暴露配置错误，要求至少形如 host:port）。
    endpoint = value[len(matched_prefix):]
    if not endpoint or ":" not in endpoint:
        raise ValueError(f"代理地址须包含 host:port：{value!r}")
    return value


# ----------------------------------------------------------------------
# 序列化（脱敏，列表 / 详情不返回 Cookie 明文 —— 需求 3.6）
# ----------------------------------------------------------------------
def serialize_shop(shop: Shop) -> Dict[str, Any]:
    """将店铺模型脱敏序列化为对外字典（不含任何凭据明文，需求 3.6）。

    仅输出展示所需字段（店铺标识、名称、Logo、归属、备注、状态与北京时间审计
    字段），并经 ``sanitize_sensitive`` 二次过滤，确保即便未来新增字段也不会
    意外外泄 Cookie / 密码等敏感信息。

    Args:
        shop: 店铺模型实例。

    Returns:
        脱敏后的店铺信息字典。
    """
    info: Dict[str, Any] = {
        "id": shop.id,
        "channel_id": shop.channel_id,
        "shop_id": shop.shop_id,
        "shop_name": shop.shop_name,
        "shop_logo": shop.shop_logo,
        "owner_user_id": shop.owner_user_id,
        "remark": shop.remark,
        "status": shop.status,
        "platform": shop.platform or DEFAULT_PLATFORM,
        "proxy_server": shop.proxy_server,
        "created_at": safe_isoformat(shop.created_at),
        "updated_at": safe_isoformat(shop.updated_at),
    }
    # 二次脱敏：统一过滤敏感字段，防御性兜底（需求 3.6）。
    return sanitize_sensitive(info)


def _get_shop_credentials(
    session: Session, shop_pk: int, owner_user_id: Optional[int]
) -> Dict[str, Any]:
    """读取并解密某店铺的账号凭据，供「详情反显 + 编辑」使用。

    应用户要求，店铺管理详情需反显账号、密码与 Cookie 明文（前端以隐藏查看
    展示）。本函数按业务键（shop_pk + user_id）取账号记录并容错解密；无记录或
    解密失败时返回空串，绝不抛异常中断详情查询。

    Args:
        session: 数据库会话。
        shop_pk: 关联店铺主键（shop.id）。
        owner_user_id: 归属用户 ID（账号业务键的一部分）。

    Returns:
        含明文 ``username`` / ``password`` / ``cookies`` 的字典（无则为空串）。
    """
    account = Repository(Account, session).get_by(
        shop_pk=shop_pk, user_id=owner_user_id
    )
    if account is None:
        return {"username": "", "password": "", "cookies": ""}
    return {
        "username": account.username or "",
        "password": (try_decrypt_text(account.password_enc) or "")
        if account.password_enc
        else "",
        "cookies": (try_decrypt_text(account.cookies_enc) or "")
        if account.cookies_enc
        else "",
    }


# ----------------------------------------------------------------------
# 新增 / 更新店铺（upsert 幂等 + Cookie 加密存储 —— 需求 3.1 / 3.2 / 3.6）
# ----------------------------------------------------------------------
def upsert_shop(
    session: Session,
    *,
    shop_id: str,
    owner_user_id: int,
    shop_name: Optional[str] = None,
    shop_logo: Optional[str] = None,
    channel_id: Optional[int] = None,
    remark: Optional[str] = None,
    cookies: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    platform: str = DEFAULT_PLATFORM,
    proxy_server: Optional[str] = None,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """新增或更新店铺（需求 3.1 / 3.2 / 3.6）。

    按业务键（owner_user_id + shop_id）upsert：同一用户已存在相同 shop_id 时
    更新该记录而非新建重复记录（幂等，需求 3.2）；不存在则新建（需求 3.1）。
    Cookie 凭据与账号密码以可逆加密存储于 account 表，店铺基础信息存于 shop 表，
    对外响应不返回任何凭据明文（需求 3.6）。

    Args:
        session: 数据库会话。
        shop_id: 拼多多店铺业务标识（业务键的一部分）。
        owner_user_id: 归属用户 ID（业务键的一部分，用于数据范围隔离）。
        shop_name: 店铺名称。
        shop_logo: 店铺 Logo URL。
        channel_id: 所属渠道 ID。
        remark: 备注。
        cookies: 登录 Cookie 明文（将加密存储，不落库明文）。
        username: 拼多多登录账号。
        password: 账号密码明文（将加密存储，不落库明文）。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。
        proxy_server: 店铺出口代理服务器地址（Phase 2 前置，仅 TikTok 通道消费；
            None=不改，空串=清空不走代理，非空须经 ``validate_proxy_server`` 校验）。
        operator_id: 操作人用户 ID（创建人审计字段）。

    Returns:
        统一响应体：成功返回脱敏后的店铺信息。
    """
    # 入参校验：店铺标识与归属用户均不可为空（业务键）。
    if not shop_id or not str(shop_id).strip():
        return error_response(CODE_PARAM_ERROR, "店铺标识不能为空")
    if owner_user_id is None:
        return error_response(CODE_PARAM_ERROR, "店铺归属用户不能为空")

    # 平台标识校验：非法枚举归 CODE_PARAM_ERROR，不抛 HTTP 4xx/5xx。
    try:
        platform = validate_platform(platform)
    except ValueError as exc:
        return error_response(CODE_PARAM_ERROR, str(exc))

    # 出口代理校验：显式传入的非空值须合法（空串 / None 归一为不走代理）。
    # 归一前记录「是否显式传入」，用于区分 None（不改）与空串（清空）。
    proxy_provided = proxy_server is not None
    try:
        proxy_server = validate_proxy_server(proxy_server)
    except ValueError as exc:
        return error_response(CODE_PARAM_ERROR, str(exc))

    shop_id = str(shop_id).strip()
    shop_repo = Repository(Shop, session)

    # 按业务键 upsert：保证相同 (owner_user_id, shop_id) 记录数恒为 1（需求 3.2）。
    biz_keys = {"owner_user_id": owner_user_id, "shop_id": shop_id}
    # 仅对显式传入的字段做更新，避免覆盖已有值为 None。
    shop_values: Dict[str, Any] = {}
    if shop_name is not None:
        shop_values["shop_name"] = shop_name
    if shop_logo is not None:
        shop_values["shop_logo"] = shop_logo
    if channel_id is not None:
        shop_values["channel_id"] = channel_id
    if remark is not None:
        shop_values["remark"] = remark
    # 平台标识：TIK-003 透传，缺省 'pdd' 与存量行为一致。
    shop_values["platform"] = platform
    # 出口代理：仅显式传入（含空串清空为 None）才更新，None 表示不改动（upsert 语义）。
    if proxy_provided:
        shop_values["proxy_server"] = proxy_server

    # 判定是否为新建（用于初始化新建记录的默认字段，如启用状态与创建人）。
    existing = shop_repo.get_by(**biz_keys)
    if existing is None:
        # 新建：默认启用，并记录创建人审计字段。
        shop_values.setdefault("status", SHOP_STATUS_ENABLED)
        shop_values["created_by"] = operator_id

    shop = shop_repo.upsert(biz_keys=biz_keys, values=shop_values)

    # 凭据加密存储到 account 表（按 shop_pk + user_id upsert 幂等，需求 3.6）。
    _upsert_account_credentials(
        session,
        shop_pk=shop.id,
        owner_user_id=owner_user_id,
        cookies=cookies,
        username=username,
        password=password,
    )

    # 店铺启用时通知 websocket 服务启动连接（参照 Customer-Agent「启动账号」，需求 5.1）。
    # 关键时序：websocket 启动连接会读库取 Cookie，故须先提交本次写入再通知，
    # 否则连接侧读不到刚保存的凭据。notify_connect 不抛异常、失败不影响保存结果。
    if shop.status == SHOP_STATUS_ENABLED:
        session.commit()
        notify_connect(
            shop_pk=shop.id,
            shop_id=shop.shop_id,
            owner_user_id=owner_user_id,
            platform=platform,
            # 以落库后的实际代理为准（未显式修改时沿用存量值，保证连接与配置一致）。
            proxy_server=shop.proxy_server,
        )

    return success_response(data=serialize_shop(shop), message="保存成功")


def _upsert_account_credentials(
    session: Session,
    *,
    shop_pk: int,
    owner_user_id: int,
    cookies: Optional[str],
    username: Optional[str],
    password: Optional[str],
) -> None:
    """加密并 upsert 店铺账号凭据到 account 表（需求 3.6）。

    Cookie 与密码以 ``common.utils.crypto`` 可逆加密后存入密文字段，绝不落库
    明文。仅当传入了任一凭据字段时才写入，避免无谓创建空账号记录。

    Args:
        session: 数据库会话。
        shop_pk: 关联店铺主键（shop.id）。
        owner_user_id: 归属用户 ID。
        cookies: Cookie 明文（None 表示不更新）。
        username: 登录账号（None 表示不更新）。
        password: 密码明文（None 表示不更新）。
    """
    # 三者皆未提供时无需维护账号凭据记录。
    if cookies is None and username is None and password is None:
        return

    account_repo = Repository(Account, session)
    values: Dict[str, Any] = {}
    if username is not None:
        values["username"] = username
    if cookies is not None:
        # Cookie 可逆加密存储（需求 3.6），列表 / 详情不返回明文。
        values["cookies_enc"] = encrypt_text(cookies)
    if password is not None:
        # 密码可逆加密存储（需求 3.6）。
        values["password_enc"] = encrypt_text(password)

    account_repo.upsert(
        biz_keys={"shop_pk": shop_pk, "user_id": owner_user_id},
        values=values,
    )


# ----------------------------------------------------------------------
# 账号密码登录 / Cookie 导入新增店铺（经 websocket 自动获取店铺信息 —— 需求 4.1-4.4）
# ----------------------------------------------------------------------
def login_shop_by_password(
    session: Session,
    *,
    username: str,
    password: str,
    owner_user_id: int,
    remark: Optional[str] = None,
    platform: str = DEFAULT_PLATFORM,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """账号密码登录并自动获取店铺信息后新增 / 更新店铺（需求 4.1 / 4.2）。

    流程：先经 websocket 服务用 Playwright 登录拼多多并抓取真实店铺信息
    （shop_id=mallId / shop_name=mallName / shop_logo=mallLogo）与登录 Cookie，
    再按业务键（owner_user_id + shop_id）upsert 落库，Cookie 与密码加密存储。
    **用户无需手填 shop_id**，由登录结果自动获取。

    Args:
        session: 数据库会话。
        username: 拼多多商家后台登录账号。
        password: 账号密码明文（将加密存储，不落库明文）。
        owner_user_id: 归属用户 ID（数据范围隔离）。
        remark: 备注。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。
        operator_id: 操作人用户 ID（创建人审计字段）。

    Returns:
        统一响应体：成功返回脱敏后的店铺信息；登录失败返回中文原因。
    """
    if not username or not str(username).strip():
        return error_response(CODE_PARAM_ERROR, "登录账号不能为空")
    if not password:
        return error_response(CODE_PARAM_ERROR, "登录密码不能为空")

    # 平台标识校验：非法枚举归 CODE_PARAM_ERROR，不抛 HTTP 4xx/5xx。
    try:
        platform = validate_platform(platform)
    except ValueError as exc:
        return error_response(CODE_PARAM_ERROR, str(exc))

    # 经 websocket 登录并获取店铺信息（耗时操作，失败已规整为中文原因）。
    result = login_with_password(str(username).strip(), password, platform=platform)
    if not result.ok or not result.info:
        return error_response(CODE_PARAM_ERROR, result.message or "账号密码登录失败")

    return _persist_logged_in_shop(
        session,
        info=result.info,
        owner_user_id=owner_user_id,
        username=str(username).strip(),
        password=password,
        remark=remark,
        platform=platform,
        operator_id=operator_id,
    )


def import_shop_by_cookie(
    session: Session,
    *,
    cookies: str,
    owner_user_id: int,
    remark: Optional[str] = None,
    platform: str = DEFAULT_PLATFORM,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """校验 Cookie 文本并自动获取店铺信息后新增 / 更新店铺（需求 4.3 / 4.4）。

    流程：先经 websocket 服务校验 Cookie 有效性并抓取真实店铺信息，再按业务键
    upsert 落库，Cookie 加密存储。**用户无需手填 shop_id**。

    Args:
        session: 数据库会话。
        cookies: 用户粘贴的 Cookie 文本。
        owner_user_id: 归属用户 ID（数据范围隔离）。
        remark: 备注。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。
        operator_id: 操作人用户 ID（创建人审计字段）。

    Returns:
        统一响应体：成功返回脱敏后的店铺信息；Cookie 无效返回中文原因。
    """
    if not cookies or not str(cookies).strip():
        return error_response(CODE_PARAM_ERROR, "Cookie 文本不能为空")

    # 平台标识校验：非法枚举归 CODE_PARAM_ERROR，不抛 HTTP 4xx/5xx。
    try:
        platform = validate_platform(platform)
    except ValueError as exc:
        return error_response(CODE_PARAM_ERROR, str(exc))

    result = login_import_by_cookie(str(cookies).strip(), platform=platform)
    if not result.ok or not result.info:
        return error_response(CODE_PARAM_ERROR, result.message or "Cookie 导入失败")

    # Cookie 导入：凭据为登录结果中回传的 Cookie（无账号密码）；账号名优先取
    # 登录结果的 username，回退取拼多多用户信息的 user_name（用于展示与后续重登）。
    cookie_username = result.info.get("username") or result.info.get("user_name")
    return _persist_logged_in_shop(
        session,
        info=result.info,
        owner_user_id=owner_user_id,
        username=cookie_username,
        password=None,
        remark=remark,
        platform=platform,
        operator_id=operator_id,
    )


def _persist_logged_in_shop(
    session: Session,
    *,
    info: Dict[str, Any],
    owner_user_id: int,
    username: Optional[str],
    password: Optional[str],
    remark: Optional[str],
    platform: str = DEFAULT_PLATFORM,
    operator_id: Optional[int],
) -> ApiResponse:
    """将登录 / 导入获取到的店铺信息与凭据落库（复用 upsert，需求 4.1-4.4 / 3.6）。

    店铺标识 / 名称 / Logo 均取自登录结果（拼多多 mallId / mallName / mallLogo），
    Cookie 取自登录结果并加密存储；账号密码（如有）一并加密存储。

    Args:
        session: 数据库会话。
        info: websocket 登录结果（含 shop_id / shop_name / shop_logo / cookies 等）。
        owner_user_id: 归属用户 ID。
        username: 登录账号（账号密码登录时为输入账号；Cookie 导入时取登录态用户名）。
        password: 账号密码明文（仅账号密码登录时有值，将加密存储）。
        remark: 备注。
        platform: 店铺所属平台（透传，默认 'pdd'）。
        operator_id: 操作人用户 ID。

    Returns:
        统一响应体：成功返回脱敏后的店铺信息。
    """
    shop_id = info.get("shop_id")
    if not shop_id:
        return error_response(CODE_PARAM_ERROR, "登录成功但未获取到店铺标识")

    # Cookie 统一序列化为字符串落库（与凭据存储口径一致）。
    cookies_value = info.get("cookies")
    if isinstance(cookies_value, (dict, list)):
        cookies_text: Optional[str] = json.dumps(cookies_value, ensure_ascii=False)
    elif cookies_value is None:
        cookies_text = None
    else:
        cookies_text = str(cookies_value)

    return upsert_shop(
        session,
        shop_id=str(shop_id),
        owner_user_id=owner_user_id,
        shop_name=info.get("shop_name"),
        shop_logo=info.get("shop_logo"),
        remark=remark,
        cookies=cookies_text,
        username=username,
        password=password,
        platform=platform,
        operator_id=operator_id,
    )


# ----------------------------------------------------------------------
# 修改店铺（备注 / 启用状态 / 关联配置 —— 需求 3.4）
# ----------------------------------------------------------------------
def update_shop(
    session: Session,
    shop_pk: int,
    *,
    current_user: Any,
    remark: Optional[str] = None,
    shop_name: Optional[str] = None,
    shop_logo: Optional[str] = None,
    channel_id: Optional[int] = None,
    proxy_server: Optional[str] = None,
    enabled: Optional[bool] = None,
    username: Optional[str] = None,
    cookies: Optional[str] = None,
    password: Optional[str] = None,
) -> ApiResponse:
    """修改店铺的备注、启用状态、关联配置或账号凭据（需求 3.4 / 3.6）。

    先经数据范围隔离校验当前用户对该店铺的可见性（非管理员仅可操作本人 / 被授权
    店铺，需求 3.7）；仅更新显式传入的字段。若 ``enabled`` 置为 False，等同停用
    （会触发断连，建议改用 ``disable_shop``，此处保留以兼容统一更新入口）。

    账号凭据（``username`` / ``cookies`` / ``password``）支持反显后编辑保存：传入
    非 None 即覆盖更新（Cookie / 密码加密存储）；若店铺处于启用态且凭据有变更，
    更新后重新通知 websocket 以新 Cookie 重连（先提交确保连接侧读到最新凭据）。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        current_user: 当前登录用户（用于数据范围隔离）。
        remark: 新备注。
        shop_name: 新店铺名称。
        shop_logo: 新 Logo URL。
        channel_id: 新渠道 ID。
        proxy_server: 新出口代理服务器地址（None=不改；空串=清空不走代理；非空
            须经 ``validate_proxy_server`` 校验）。
        enabled: 启用状态；True=启用，False=停用（停用将断连）。
        username: 新登录账号（None=不改）。
        cookies: 新 Cookie 明文（None=不改，将加密存储）。
        password: 新密码明文（None=不改，将加密存储）。

    Returns:
        统一响应体：成功返回更新后的脱敏店铺信息（含反显凭据）。
    """
    shop_repo = Repository(Shop, session)
    shop = shop_repo.get(shop_pk)
    if shop is None:
        return error_response(CODE_NOT_FOUND, "目标店铺不存在")

    # 数据范围隔离：非管理员仅可操作本人 / 被授权店铺（需求 3.7）。
    scope = _build_scope(session, current_user)
    if not is_in_scope(scope, shop.owner_user_id):
        return error_response(CODE_NOT_FOUND, "目标店铺不存在")

    values: Dict[str, Any] = {}
    if remark is not None:
        values["remark"] = remark
    if shop_name is not None:
        values["shop_name"] = shop_name
    if shop_logo is not None:
        values["shop_logo"] = shop_logo
    if channel_id is not None:
        values["channel_id"] = channel_id

    # 出口代理：显式传入（含空串清空为 None）才更新，None 表示不改动。
    if proxy_server is not None:
        try:
            values["proxy_server"] = validate_proxy_server(proxy_server)
        except ValueError as exc:
            return error_response(CODE_PARAM_ERROR, str(exc))

    if values:
        shop_repo.update(shop_pk, **values)

    # 账号凭据编辑：仅当传入任一凭据字段时覆盖更新（加密存储，需求 3.6）。
    credentials_changed = (
        username is not None or cookies is not None or password is not None
    )
    if credentials_changed:
        _upsert_account_credentials(
            session,
            shop_pk=shop.id,
            owner_user_id=shop.owner_user_id,
            cookies=cookies,
            username=username,
            password=password,
        )

    # 更新后的实际出口代理（未显式修改时沿用存量值，保证连接与配置一致）。
    effective_proxy = values["proxy_server"] if "proxy_server" in values else shop.proxy_server

    # 启用状态变更：停用走停用流程（断连），启用直接置回启用。
    if enabled is not None:
        if enabled:
            shop_repo.update(shop_pk, status=SHOP_STATUS_ENABLED)
            # 重新启用：通知 websocket 启动连接（先提交，使连接侧能读到凭据，需求 5.1）。
            session.commit()
            notify_connect(
                shop_pk=shop.id,
                shop_id=shop.shop_id,
                owner_user_id=shop.owner_user_id,
                platform=shop.platform or DEFAULT_PLATFORM,
                proxy_server=effective_proxy,
            )
        else:
            return disable_shop(session, shop_pk, current_user=current_user)
    elif credentials_changed and shop.status == SHOP_STATUS_ENABLED:
        # 凭据变更且店铺在线：提交后通知 websocket 以最新 Cookie 重连（尽力而为）。
        session.commit()
        notify_connect(
            shop_pk=shop.id,
            shop_id=shop.shop_id,
            owner_user_id=shop.owner_user_id,
            platform=shop.platform or DEFAULT_PLATFORM,
            proxy_server=effective_proxy,
        )

    # 返回最新详情（含反显凭据），便于前端保存后即时刷新展示。
    detail = serialize_shop(shop)
    detail.update(_get_shop_credentials(session, shop.id, shop.owner_user_id))
    return success_response(data=detail, message="更新成功")


# ----------------------------------------------------------------------
# 停用店铺（逻辑删除 + 断开连接 —— 需求 3.5）
# ----------------------------------------------------------------------
def disable_shop(
    session: Session,
    shop_pk: int,
    *,
    current_user: Any,
) -> ApiResponse:
    """停用店铺并断开其拼多多连接（需求 3.5）。

    经状态字段逻辑删除（status=0），禁止物理删除（规范 11 / 需求 24.6）；停用后
    经 HTTP 通知 websocket 服务断开该店铺的 WebSocket 长连接（断连为尽力而为，
    失败不影响停用落库）。受数据范围隔离约束（需求 3.7）。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        current_user: 当前登录用户（用于数据范围隔离）。

    Returns:
        统一响应体：成功返回停用后的脱敏店铺信息。
    """
    shop_repo = Repository(Shop, session)
    shop = shop_repo.get(shop_pk)
    if shop is None:
        return error_response(CODE_NOT_FOUND, "目标店铺不存在")

    # 数据范围隔离：非管理员仅可停用本人 / 被授权店铺（需求 3.7）。
    scope = _build_scope(session, current_user)
    if not is_in_scope(scope, shop.owner_user_id):
        return error_response(CODE_NOT_FOUND, "目标店铺不存在")

    # 逻辑删除：状态置停用，记录保留（需求 3.5 / 24.6）。
    shop_repo.update(shop_pk, status=SHOP_STATUS_DISABLED)

    # 经 HTTP 通知 websocket 服务断开连接（尽力而为，失败仅记日志不影响停用）。
    # 事务提交统一由 get_db 依赖在请求正常结束时完成；notify_disconnect 不抛异常，
    # 故停用状态必然随请求结束落库。
    notify_disconnect(
        shop_pk=shop.id,
        shop_id=shop.shop_id,
        owner_user_id=shop.owner_user_id,
        platform=shop.platform or DEFAULT_PLATFORM,
    )

    return success_response(data=serialize_shop(shop), message="已停用并断开连接")


# ----------------------------------------------------------------------
# 店铺列表（北京时间倒序 + 后端分页 + 数据范围隔离 —— 需求 3.3 / 3.7 / 3.6）
# ----------------------------------------------------------------------
def list_shops(
    session: Session,
    *,
    current_user: Any,
    page: Any = 1,
    page_size: Any = 20,
    status: Optional[int] = None,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """分页查询店铺列表（需求 3.3 / 3.6 / 3.7）。

    按北京时间（created_at）倒序、后端分页（默认 20，可选 10/20/50/100）返回当前
    用户有权查看的店铺。数据范围隔离：管理员可见全部；非管理员仅见本人创建或被
    显式授权的店铺（需求 3.7）。列表中每条均经脱敏，不返回 Cookie 明文（需求 3.6）。

    Args:
        session: 数据库会话。
        current_user: 当前登录用户。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        status: 按状态筛选（1=启用，0=停用）；None 表示不筛选。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表（非管理员适用）。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    scope = _build_scope(session, current_user, authorized_owner_ids)
    shop_repo = Repository(Shop, session)

    # 基础过滤条件（状态筛选）。
    base_filters: Dict[str, Any] = {}
    if status is not None:
        base_filters["status"] = status

    allowed = scope.allowed_owner_ids()
    if allowed is None:
        # 管理员：无归属限制，直接分页（默认按 created_at 北京时间倒序）。
        page_result = shop_repo.paginate(
            page=page, page_size=page_size, filters=base_filters or None
        )
        serialized = [serialize_shop(shop) for shop in page_result.items]
        return _build_paged_response(page_result, serialized)

    if not allowed:
        # 非管理员且无任何可见归属：返回空分页（不可见任何数据，需求 3.7）。
        from common.utils.pagination import normalize_pagination

        norm_page, norm_size = normalize_pagination(page, page_size)
        data = {"list": [], "total": 0, "page": norm_page, "page_size": norm_size}
        return success_response(data=data, message="查询成功")

    if len(allowed) == 1:
        # 单一归属：等值条件下推数据库分页，最高效。
        (only_owner,) = tuple(allowed)
        filters = {**base_filters, "owner_user_id": only_owner}
        page_result = shop_repo.paginate(
            page=page, page_size=page_size, filters=filters or None
        )
        serialized = [serialize_shop(shop) for shop in page_result.items]
        return _build_paged_response(page_result, serialized)

    # 多归属（本人 + 被授权对象）：以 IN 条件下推查询并分页。
    return _list_shops_multi_owner(
        session, scope, base_filters, page=page, page_size=page_size
    )


def _list_shops_multi_owner(
    session: Session,
    scope: DataScope,
    base_filters: Dict[str, Any],
    *,
    page: Any,
    page_size: Any,
) -> ApiResponse:
    """非管理员多归属场景的店铺分页查询（IN 条件下推 + 北京时间倒序）。

    等值过滤无法表达「owner_user_id IN 多值」，故此处用 SQLAlchemy 表达式以
    ``owner_user_id.in_(...)`` 下推隔离条件，并复用分页规整与北京时间倒序排序。

    Args:
        session: 数据库会话。
        scope: 用户数据范围。
        base_filters: 基础等值过滤（如状态）。
        page: 页码。
        page_size: 每页条数。

    Returns:
        统一响应体：分页结构。
    """
    from sqlalchemy import func, select

    from common.utils.pagination import (
        build_page_result,
        calc_offset,
        normalize_pagination,
    )

    norm_page, norm_size = normalize_pagination(page, page_size)
    allowed_ids = sorted(scope.allowed_owner_ids() or frozenset())

    # 组装 where 条件：归属 IN + 基础等值过滤（参数化绑定）。
    conditions = [Shop.owner_user_id.in_(allowed_ids)]
    for field_name, value in base_filters.items():
        conditions.append(getattr(Shop, field_name) == value)

    # 统计总数。
    count_stmt = select(func.count()).select_from(Shop).where(*conditions)
    total = int(session.execute(count_stmt).scalar_one())

    # 取当前页：按 created_at 北京时间倒序，id 倒序作为稳定二级排序键。
    offset = calc_offset(norm_page, norm_size)
    list_stmt = (
        select(Shop)
        .where(*conditions)
        .order_by(Shop.created_at.desc(), Shop.id.desc())
        .offset(offset)
        .limit(norm_size)
    )
    shops = list(session.execute(list_stmt).scalars().all())

    page_result = build_page_result(
        items=shops, total=total, page=norm_page, page_size=norm_size
    )
    serialized = [serialize_shop(shop) for shop in shops]
    return _build_paged_response(page_result, serialized)


# ----------------------------------------------------------------------
# 查询单个店铺详情（脱敏 + 数据范围隔离）
# ----------------------------------------------------------------------
def get_shop(
    session: Session,
    shop_pk: int,
    *,
    current_user: Any,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """查询单个店铺详情（脱敏、受数据范围隔离约束，需求 3.6 / 3.7）。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        current_user: 当前登录用户。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：成功返回脱敏店铺信息；不存在 / 无权限统一返回「不存在」。
    """
    shop = Repository(Shop, session).get(shop_pk)
    if shop is None:
        return error_response(CODE_NOT_FOUND, "目标店铺不存在")

    scope = _build_scope(session, current_user, authorized_owner_ids)
    # 越权访问与不存在统一返回「不存在」，避免泄露其它用户店铺的存在性。
    if not is_in_scope(scope, shop.owner_user_id):
        return error_response(CODE_NOT_FOUND, "目标店铺不存在")

    # 详情反显账号 / 密码 / Cookie 明文（应用户要求支持回显 + 编辑，前端以隐藏
    # 查看展示）。凭据字段在脱敏之后再附加，避免被 sanitize_sensitive 过滤。
    detail = serialize_shop(shop)
    detail.update(_get_shop_credentials(session, shop.id, shop.owner_user_id))
    return success_response(data=detail, message="查询成功")


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------
def _build_scope(
    session: Session,
    current_user: Any,
    authorized_owner_ids: Optional[List[int]] = None,
) -> DataScope:
    """据当前用户装配数据范围（集中经 data_scope，规范 42）。

    Args:
        session: 数据库会话。
        current_user: 当前登录用户。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        装配完成的 ``DataScope``。
    """
    return build_data_scope(
        current_user,
        session=session,
        authorized_owner_ids=authorized_owner_ids,
    )


def _build_paged_response(page_result: Any, serialized: List[Dict[str, Any]]) -> ApiResponse:
    """以脱敏后的列表替换分页结果 items，构造统一分页响应体。

    Args:
        page_result: 仓储层返回的分页结果（含 total/page/page_size）。
        serialized: 已脱敏的店铺信息列表。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    data = {
        "list": serialized,
        "total": page_result.total,
        "page": page_result.page,
        "page_size": page_result.page_size,
    }
    return success_response(data=data, message="查询成功")


__all__ = [
    "SHOP_STATUS_ENABLED",
    "SHOP_STATUS_DISABLED",
    "VALID_PLATFORMS",
    "DEFAULT_PLATFORM",
    "validate_platform",
    "serialize_shop",
    "upsert_shop",
    "login_shop_by_password",
    "import_shop_by_cookie",
    "update_shop",
    "disable_shop",
    "list_shops",
    "get_shop",
]
