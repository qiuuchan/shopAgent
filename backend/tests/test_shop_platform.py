# -*- coding: utf-8 -*-
"""
backend.tests.test_shop_platform —— 店铺平台（platform）全链路透传测试（TIK-003）
===============================================================================
本文件用途：验证 backend 侧「平台分派链」按 TIK-003 落地，覆盖：

- 平台枚举校验（common 约定 + 默认值）：默认 'pdd' 向后兼容；非法枚举抛
  ``ValueError`` / 路由层转 ``BusinessError``（归 CODE_PARAM_ERROR，不抛 4xx/5xx）。
- shops 路由 platform 字段：默认值（缺省 'pdd'）、显式传入（'tiktok' 入库）、
  非法值（'wechat'）被拒（HTTP 仍 200，success=false）。
- 登录分派透传：账号密码登录 / Cookie 导入两种入口，platform 透传至
  shop_login_client 的 websocket 调用参数。
- chat_send_client 平台无感（Phase 2）：TikTok 不再拦截，全部经 websocket 侧按
  Shop.platform 分派；websocket 返回失败原因原样透传。

测试方案：pytest + 内存 SQLite（夹具见 conftest.py）；对服务间 HTTP 调用
（shop_login_client / chat_send_client 内部 service_client.post_json）做 monkeypatch，
避免真实网络；路由测试经 FastAPI TestClient，所有接口 HTTP 恒返回 200。
"""
from __future__ import annotations

import pytest

import common.models.shop_models  # noqa: F401  确保 Shop 表登记进 Base.metadata
from app.core.business_codes import CODE_PARAM_ERROR
from app.services import account_service, chat_send_client, shop_login_client
from app.services.account_service import (
    DEFAULT_PLATFORM,
    VALID_PLATFORMS,
    validate_platform,
    validate_proxy_server,
)
from common.db.repository import Repository
from common.models.shop_models import Shop
from common.models.user_models import (
    SysPermission,
    SysRole,
    SysRolePermission,
    SysUser,
)


# ----------------------------------------------------------------------
# 1) 平台枚举校验（默认值 + 合法性）
# ----------------------------------------------------------------------
def test_validate_platform_default_is_pdd():
    """空 / None 归一为默认平台 'pdd'（向后兼容存量店铺）。"""
    assert validate_platform(None) == DEFAULT_PLATFORM
    assert validate_platform("") == DEFAULT_PLATFORM
    assert validate_platform("  ") == DEFAULT_PLATFORM


def test_validate_platform_valid_values():
    """合法枚举（pdd / tiktok）原样返回（含空白容忍）。"""
    assert validate_platform("pdd") == "pdd"
    assert validate_platform("tiktok") == "tiktok"
    assert validate_platform("  tiktok  ") == "tiktok"
    assert VALID_PLATFORMS == ("pdd", "tiktok")


def test_validate_platform_invalid_raises():
    """非法枚举抛 ValueError（供路由层转 BusinessError）。"""
    import pytest as _pytest

    for bad in ("wechat", "taobao", "PDD", "1"):
        with _pytest.raises(ValueError):
            validate_platform(bad)


# ----------------------------------------------------------------------
# 2) account_service 层 platform 透传（持久化）
# ----------------------------------------------------------------------
def test_upsert_shop_persists_platform(db_session, seed_permissions, monkeypatch):
    """upsert_shop 将 platform 写入 Shop.platform；缺省为 'pdd'。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    user = _make_owner(db_session, seed_permissions, "owner_p1")

    resp = account_service.upsert_shop(
        db_session, shop_id="SHOP_P1", owner_user_id=user.id, operator_id=user.id
    )
    assert resp.success is True
    shop = Repository(Shop, db_session).get_by(owner_user_id=user.id, shop_id="SHOP_P1")
    assert shop.platform == DEFAULT_PLATFORM
    # 序列化输出也带回 platform 字段。
    assert resp.data["platform"] == DEFAULT_PLATFORM


def test_upsert_shop_persists_tiktok_platform(db_session, seed_permissions, monkeypatch):
    """upsert_shop 显式 platform='tiktok' 入库。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    user = _make_owner(db_session, seed_permissions, "owner_p2")

    resp = account_service.upsert_shop(
        db_session,
        shop_id="SHOP_P2",
        owner_user_id=user.id,
        platform="tiktok",
        operator_id=user.id,
    )
    assert resp.success is True
    shop = Repository(Shop, db_session).get_by(owner_user_id=user.id, shop_id="SHOP_P2")
    assert shop.platform == "tiktok"


def test_upsert_shop_invalid_platform_rejected(db_session, seed_permissions, monkeypatch):
    """upsert_shop 非法 platform 返回 CODE_PARAM_ERROR 失败响应（不抛异常）。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    user = _make_owner(db_session, seed_permissions, "owner_p3")

    resp = account_service.upsert_shop(
        db_session,
        shop_id="SHOP_P3",
        owner_user_id=user.id,
        platform="wechat",
        operator_id=user.id,
    )
    assert resp.success is False
    assert resp.code == CODE_PARAM_ERROR


def test_login_shop_by_password_passes_platform(db_session, seed_permissions, monkeypatch):
    """login_shop_by_password 将 platform 透传至 shop_login_client.login_by_password。"""
    captured = {}

    def fake_login(username, password, platform="pdd"):
        captured["platform"] = platform
        return shop_login_client.ShopLoginResult(
            ok=True,
            info={"shop_id": "LOGIN_P", "shop_name": "登录店", "cookies": "c"},
        )

    # account_service 经模块级别名 login_with_password 调用，需打该别名。
    monkeypatch.setattr(account_service, "login_with_password", fake_login)
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    user = _make_owner(db_session, seed_permissions, "owner_p4")

    resp = account_service.login_shop_by_password(
        db_session,
        username="u",
        password="p",
        owner_user_id=user.id,
        platform="tiktok",
        operator_id=user.id,
    )
    assert resp.success is True
    assert captured["platform"] == "tiktok"
    shop = Repository(Shop, db_session).get_by(owner_user_id=user.id, shop_id="LOGIN_P")
    assert shop.platform == "tiktok"


def test_import_shop_by_cookie_passes_platform(db_session, seed_permissions, monkeypatch):
    """import_shop_by_cookie 将 platform 透传至 shop_login_client.import_by_cookie。"""
    captured = {}

    def fake_import(cookies, platform="pdd"):
        captured["platform"] = platform
        return shop_login_client.ShopLoginResult(
            ok=True,
            info={"shop_id": "COOKIE_P", "shop_name": "Cookie店", "cookies": "c"},
        )

    # account_service 经模块级别名 login_import_by_cookie 调用，需打该别名。
    monkeypatch.setattr(account_service, "login_import_by_cookie", fake_import)
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    user = _make_owner(db_session, seed_permissions, "owner_p5")

    resp = account_service.import_shop_by_cookie(
        db_session,
        cookies="ck",
        owner_user_id=user.id,
        platform="tiktok",
        operator_id=user.id,
    )
    assert resp.success is True
    assert captured["platform"] == "tiktok"


# ----------------------------------------------------------------------
# 3) connection_notify 平台透传（默认 'pdd'）
# ----------------------------------------------------------------------
def test_notify_connect_passes_default_platform(monkeypatch):
    """notify_connect 默认将 platform='pdd' 透传至 websocket 请求体。"""
    captured = {}

    def fake_post(base_url, path, payload, *, timeout):
        captured["payload"] = payload
        return _fake_ok_response()

    monkeypatch.setattr("common.services.service_client.post_json", fake_post)
    account_service.notify_connect(1, "S1", 10)
    assert captured["payload"]["platform"] == "pdd"


def test_notify_disconnect_passes_explicit_platform(monkeypatch):
    """notify_disconnect 显式 platform 透传至 websocket 请求体。"""
    captured = {}

    def fake_post(base_url, path, payload, *, timeout):
        captured["payload"] = payload
        return _fake_ok_response()

    monkeypatch.setattr("common.services.service_client.post_json", fake_post)
    account_service.notify_disconnect(1, "S1", 10, platform="tiktok")
    assert captured["payload"]["platform"] == "tiktok"


# ----------------------------------------------------------------------
# 4) shops 路由 platform 字段校验（默认值 / 非法值）
# ----------------------------------------------------------------------
def test_upsert_shop_route_default_platform(
    client, db_session, seed_permissions, monkeypatch
):
    """路由 POST /shops 缺省 platform 时，落库为 'pdd'。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    token = _auth_token_for_shop(client, db_session, seed_permissions, "ruser_d")

    resp = client.post(
        "/api/v1/shops",
        headers=_auth(token),
        json={"shop_id": "ROUTE_D"},
    ).json()
    assert resp["success"] is True
    assert resp["data"]["platform"] == "pdd"
    shop = Repository(Shop, db_session).get_by(shop_id="ROUTE_D")
    assert shop.platform == "pdd"


def test_upsert_shop_route_tiktok_platform(
    client, db_session, seed_permissions, monkeypatch
):
    """路由 POST /shops 显式 platform='tiktok' 落库。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    token = _auth_token_for_shop(client, db_session, seed_permissions, "ruser_t")

    resp = client.post(
        "/api/v1/shops",
        headers=_auth(token),
        json={"shop_id": "ROUTE_T", "platform": "tiktok"},
    ).json()
    assert resp["success"] is True
    assert resp["data"]["platform"] == "tiktok"


def test_upsert_shop_route_invalid_platform_rejected(
    client, db_session, seed_permissions, monkeypatch
):
    """路由 POST /shops 非法 platform 被拒：HTTP 仍 200，success=false，code=40000。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    token = _auth_token_for_shop(client, db_session, seed_permissions, "ruser_x")

    resp = client.post(
        "/api/v1/shops",
        headers=_auth(token),
        json={"shop_id": "ROUTE_X", "platform": "wechat"},
    ).json()
    # 校验失败抛 BusinessError，统一处理器兜底为 HTTP 200 + 失败响应体（不抛 4xx/5xx）。
    assert resp["success"] is False
    assert resp["code"] == CODE_PARAM_ERROR


# ----------------------------------------------------------------------
# 5) chat_send_client 平台无感（Phase 2：TikTok 不再拦截，全部走 websocket 分派）
# ----------------------------------------------------------------------
def test_chat_send_tiktok_calls_websocket(monkeypatch):
    """TikTok 店铺照常调用 websocket（平台分派由 websocket 侧完成，Phase 2）。"""
    called = {"n": 0}

    def fake_post(base_url, path, payload, *, timeout):
        called["n"] += 1
        return _fake_ok_response()

    monkeypatch.setattr("common.services.service_client.post_json", fake_post)
    result = chat_send_client.send_manual_message(1, "S1", 10, "C1", "你好")
    assert result.ok is True
    assert called["n"] == 1  # 调用 websocket（websocket 侧按 Shop.platform 分派）


def test_chat_send_pdd_calls_websocket(monkeypatch):
    """PDD 路径照常调用 websocket（与现状一致）。"""
    called = {"n": 0}

    def fake_post(base_url, path, payload, *, timeout):
        called["n"] += 1
        return _fake_ok_response()

    monkeypatch.setattr("common.services.service_client.post_json", fake_post)
    result = chat_send_client.send_manual_message(1, "S1", 10, "C1", "你好")
    assert result.ok is True
    assert called["n"] == 1


def test_chat_send_failure_propagates_websocket_message(monkeypatch):
    """websocket 返回失败时，失败原因原样透传给调用方。"""
    def fake_post(base_url, path, payload, *, timeout):
        return type("R", (), {"ok": True, "body": {}, "success": False,
                              "message": "TikTok 店铺未连接，请先建立连接", "error": None})()

    monkeypatch.setattr("common.services.service_client.post_json", fake_post)
    result = chat_send_client.send_manual_message(1, "S1", 10, "C1", "你好")
    assert result.ok is False
    assert "未连接" in result.message


# ----------------------------------------------------------------------
# 5) 店铺出口代理（proxy_server）字段（Phase 2 前置）
# ----------------------------------------------------------------------
def test_validate_proxy_server_normalization():
    """空 / None 归一为 None（不走代理）；合法地址原样返回。"""
    assert validate_proxy_server(None) is None
    assert validate_proxy_server("") is None
    assert validate_proxy_server("  ") is None
    assert validate_proxy_server("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert validate_proxy_server("  socks5://proxy.example.com:1080  ") == "socks5://proxy.example.com:1080"
    assert validate_proxy_server("https://user:pass@host:3128") == "https://user:pass@host:3128"


def test_validate_proxy_server_invalid_raises():
    """非支持的协议前缀或缺 host:port 抛 ValueError。"""
    for bad in ("ftp://host:21", "host:8080", "http://", "://host:1", "1.2.3.4"):
        with pytest.raises(ValueError):
            validate_proxy_server(bad)


def test_upsert_shop_persists_proxy_server(db_session, seed_permissions, monkeypatch):
    """upsert_shop 将 proxy_server 写入 Shop.proxy_server 并随序列化返回。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    user = _make_owner(db_session, seed_permissions, "owner_px1")

    resp = account_service.upsert_shop(
        db_session,
        shop_id="SHOP_PX1",
        owner_user_id=user.id,
        platform="tiktok",
        proxy_server="http://127.0.0.1:7890",
        operator_id=user.id,
    )
    assert resp.success is True
    assert resp.data["proxy_server"] == "http://127.0.0.1:7890"
    shop = Repository(Shop, db_session).get_by(shop_id="SHOP_PX1")
    assert shop.proxy_server == "http://127.0.0.1:7890"


def test_upsert_shop_empty_proxy_clears(db_session, seed_permissions, monkeypatch):
    """upsert 显式传空串：清空已存代理（归一为 None，不走代理）。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    user = _make_owner(db_session, seed_permissions, "owner_px2")

    account_service.upsert_shop(
        db_session,
        shop_id="SHOP_PX2",
        owner_user_id=user.id,
        platform="tiktok",
        proxy_server="http://127.0.0.1:7890",
        operator_id=user.id,
    )
    resp = account_service.upsert_shop(
        db_session,
        shop_id="SHOP_PX2",
        owner_user_id=user.id,
        platform="tiktok",
        proxy_server="",
        operator_id=user.id,
    )
    assert resp.success is True
    shop = Repository(Shop, db_session).get_by(shop_id="SHOP_PX2")
    assert shop.proxy_server is None


def test_upsert_shop_invalid_proxy_rejected(db_session, seed_permissions, monkeypatch):
    """upsert 传入非法代理地址：拒绝保存（CODE_PARAM_ERROR）。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    user = _make_owner(db_session, seed_permissions, "owner_px3")

    resp = account_service.upsert_shop(
        db_session,
        shop_id="SHOP_PX3",
        owner_user_id=user.id,
        proxy_server="ftp://host:21",
        operator_id=user.id,
    )
    assert resp.success is False
    assert resp.code == CODE_PARAM_ERROR


def test_notify_connect_passes_proxy_server(monkeypatch):
    """notify_connect 将 proxy_server 透传至 websocket 请求体（缺省 None）。"""
    captured = {}

    def fake_post(base_url, path, payload, *, timeout):
        captured["payload"] = payload
        return _fake_ok_response()

    monkeypatch.setattr("common.services.service_client.post_json", fake_post)

    account_service.notify_connect(
        1, "S1", 10, platform="tiktok", proxy_server="http://127.0.0.1:7890"
    )
    assert captured["payload"]["proxy_server"] == "http://127.0.0.1:7890"

    captured.clear()
    account_service.notify_connect(1, "S1", 10)
    assert captured["payload"]["proxy_server"] is None


def test_upsert_shop_route_proxy_server(
    client, db_session, seed_permissions, monkeypatch
):
    """路由 POST /shops 携带 proxy_server 落库并返回。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    token = _auth_token_for_shop(client, db_session, seed_permissions, "ruser_px")

    resp = client.post(
        "/api/v1/shops",
        headers=_auth(token),
        json={
            "shop_id": "ROUTE_PX",
            "platform": "tiktok",
            "proxy_server": "socks5://proxy.example.com:1080",
        },
    ).json()
    assert resp["success"] is True
    assert resp["data"]["proxy_server"] == "socks5://proxy.example.com:1080"
    shop = Repository(Shop, db_session).get_by(shop_id="ROUTE_PX")
    assert shop.proxy_server == "socks5://proxy.example.com:1080"


def test_upsert_shop_route_invalid_proxy_rejected(
    client, db_session, seed_permissions, monkeypatch
):
    """路由 POST /shops 非法代理地址：HTTP 200、success=false。"""
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    token = _auth_token_for_shop(client, db_session, seed_permissions, "ruser_px2")

    resp = client.post(
        "/api/v1/shops",
        headers=_auth(token),
        json={"shop_id": "ROUTE_PX_BAD", "proxy_server": "no-scheme:8080"},
    ).json()
    assert resp["success"] is False


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------
def _make_owner(db_session, seed_permissions, username: str):
    """创建一个普通用户并返回（与 test_account_service 同款）。"""
    user = SysUser(
        username=username,
        password_hash="x",
        role_id=seed_permissions["role_manager_id"],
        status=1,
    )
    db_session.add(user)
    db_session.flush()
    db_session.commit()
    return user


def _auth_token_for_shop(client, db_session, seed_permissions, username: str) -> str:
    """创建具备 shop:create / shop:view 权限的用户并登录，返回令牌。"""
    # 专门角色 + 权限点（与普通 manager 角色隔离，避免影响其它用例）。
    role = SysRole(role_name=f"shop_role_{username}", is_admin=False, status=1)
    db_session.add(role)
    db_session.flush()
    perms = []
    for action in ("create", "view"):
        perm = SysPermission(resource_key="shop", action=action)
        db_session.add(perm)
        db_session.flush()
        db_session.add(SysRolePermission(role_id=role.id, permission_id=perm.id))
        perms.append(perm)
    password = "pw-123456"
    user = SysUser(username=username, password_hash=_hash(password), role_id=role.id, status=1)
    db_session.add(user)
    db_session.flush()
    db_session.commit()

    resp = client.post("/api/v1/login", json={"username": username, "password": password})
    body = resp.json()
    assert body["success"] is True, body
    return body["data"]["token"]


def _auth(token: str) -> dict:
    """构造 Bearer 鉴权头。"""
    return {"Authorization": f"Bearer {token}"}


def _hash(password: str) -> str:
    """bcrypt 哈希（复用项目工具）。"""
    from common.utils.security import hash_password

    return hash_password(password)


def _fake_ok_response():
    """构造一个业务成功的 service_client 响应桩。"""

    class _Body:
        ok = True
        body = "{}"
        success = True
        message = ""
        error = ""
        data = {"connected": True}

    return _Body()


__all__ = [
    "test_validate_platform_default_is_pdd",
    "test_validate_platform_valid_values",
    "test_validate_platform_invalid_raises",
    "test_upsert_shop_persists_platform",
    "test_upsert_shop_persists_tiktok_platform",
    "test_upsert_shop_invalid_platform_rejected",
    "test_login_shop_by_password_passes_platform",
    "test_import_shop_by_cookie_passes_platform",
    "test_notify_connect_passes_default_platform",
    "test_notify_disconnect_passes_explicit_platform",
    "test_upsert_shop_route_default_platform",
    "test_upsert_shop_route_tiktok_platform",
    "test_upsert_shop_route_invalid_platform_rejected",
    "test_chat_send_tiktok_blocked_without_websocket_call",
    "test_chat_send_pdd_calls_websocket",
    "test_chat_send_resolves_platform_from_db",
    "test_validate_proxy_server_normalization",
    "test_validate_proxy_server_invalid_raises",
    "test_upsert_shop_persists_proxy_server",
    "test_upsert_shop_empty_proxy_clears",
    "test_upsert_shop_invalid_proxy_rejected",
    "test_notify_connect_passes_proxy_server",
    "test_upsert_shop_route_proxy_server",
    "test_upsert_shop_route_invalid_proxy_rejected",
]
