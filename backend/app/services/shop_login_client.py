# -*- coding: utf-8 -*-
"""
backend.app.services.shop_login_client —— 店铺登录（经统一服务间客户端调用 websocket）
==================================================================================
本文件用途：在「添加店铺」（需求 4.1-4.4）时，由 **websocket 服务**经 Playwright
完成拼多多账号密码登录 / Cookie 导入校验，并抓取真实店铺信息（mallId / mallName /
mallLogo）与用户信息。按设计「多服务拆分架构」，backend 不直接维护浏览器自动化，
而是通过 **HTTP 调用** websocket 服务的登录接口完成，地址经环境变量
``WEBSOCKET_SERVICE_URL`` 配置，**禁止写死 localhost**（规范 21 / 需求 25.4）。

对外提供：
- ``login_by_password(username, password)``：账号密码登录。
- ``import_by_cookie(cookies)``：Cookie 文本导入校验。

二者均返回 ``ShopLoginResult``：
- ``ok``：是否成功获取到店铺信息。
- ``info``：成功时为含 shop_id / shop_name / shop_logo / user_id / username /
  password / cookies 的字典（供 backend 加密入库）；失败时为 None。
- ``message``：失败原因（中文）；成功时为空字符串。

实现要点：
- 复用 common 统一服务间 HTTP 客户端 ``common.services.service_client``（规范 36/52）。
- 登录为耗时操作（Playwright 非无头 + 可能的人工验证），超时给予较长有界值。
- 网络不可达 / 超时 / 非 2xx / 业务失败一律规整为 ok=False，不抛异常打断 backend
  主流程（健壮性兜底，需求 26）。
- Cookie 等敏感信息仅经内网服务间传输并随后由 backend 加密入库，不外泄前端（需求 3.6）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from common.services import service_client

logger = logging.getLogger(__name__)

# 登录请求超时（秒）：Playwright 登录含人工验证等待，给予较长有界超时。
# 与 websocket 侧登录等待超时（默认 120s 量级）匹配，并留出网络余量。
_LOGIN_TIMEOUT_SECONDS: float = 180.0

# websocket 服务登录接口的相对路径（与 websocket 路由约定一致）。
_PASSWORD_LOGIN_PATH: str = "/api/v1/login/password"
_COOKIE_IMPORT_PATH: str = "/api/v1/login/cookie"


@dataclass
class ShopLoginResult:
    """店铺登录结果（对 websocket 服务响应的统一规整）。

    Attributes:
        ok: 是否成功获取到店铺信息。
        info: 成功时的店铺与账号信息字典；失败时为 None。
        message: 失败原因（中文）；成功时为空字符串。
    """

    ok: bool = False
    info: Optional[Dict[str, Any]] = field(default=None)
    message: str = ""


def _invoke(path: str, payload: Dict[str, Any], *, action: str) -> ShopLoginResult:
    """调用 websocket 登录接口并将响应规整为 ``ShopLoginResult``（健壮性兜底）。

    Args:
        path: websocket 登录接口相对路径。
        payload: 请求体。
        action: 动作描述（用于日志，如「账号密码登录」）。

    Returns:
        规整后的 ``ShopLoginResult``。
    """
    response = service_client.post_json(
        service_client.websocket_base_url(),
        path,
        payload,
        timeout=_LOGIN_TIMEOUT_SECONDS,
    )

    # 传输层失败（网络不可达 / 超时 / 非 2xx / 解析失败）。
    if not response.ok or response.body is None:
        logger.warning(
            "调用 websocket %s 失败：err=%s", action, response.message or response.error
        )
        return ShopLoginResult(ok=False, message="登录服务暂不可用，请稍后重试")

    # 业务层失败（登录失败 / Cookie 无效等），透传 websocket 的中文原因。
    if not response.success:
        message = response.message or f"{action}失败"
        logger.warning("%s失败：%s", action, message)
        return ShopLoginResult(ok=False, message=str(message))

    info = response.data
    if not info or not info.get("shop_id"):
        logger.warning("%s成功但未返回店铺信息", action)
        return ShopLoginResult(ok=False, message="登录成功但未获取到店铺信息")

    return ShopLoginResult(ok=True, info=info)


def login_by_password(
    username: str, password: str, platform: str = "pdd"
) -> ShopLoginResult:
    """经 websocket 账号密码登录拼多多并获取店铺信息（需求 4.1 / 4.2）。

    Args:
        username: 拼多多商家后台登录账号。
        password: 拼多多商家后台登录密码（明文，仅内网传输）。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。

    Returns:
        规整后的 ``ShopLoginResult``；成功时 info 含 shop_id / shop_name /
        shop_logo / user_id / username / password / cookies。
    """
    return _invoke(
        _PASSWORD_LOGIN_PATH,
        {"username": username, "password": password, "platform": platform},
        action="账号密码登录",
    )


def import_by_cookie(cookies: str, platform: str = "pdd") -> ShopLoginResult:
    """经 websocket 校验 Cookie 文本并获取店铺信息（需求 4.3 / 4.4）。

    Args:
        cookies: 用户粘贴的 Cookie 文本。
        platform: 店铺所属平台（'pdd' / 'tiktok'，默认 'pdd'，TIK-003 透传）。

    Returns:
        规整后的 ``ShopLoginResult``；成功时 info 含 shop_id / shop_name /
        shop_logo / user_id / cookies。
    """
    return _invoke(
        _COOKIE_IMPORT_PATH,
        {"cookies": cookies, "platform": platform},
        action="Cookie 导入",
    )


__all__ = ["ShopLoginResult", "login_by_password", "import_by_cookie"]
