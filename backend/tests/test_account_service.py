# -*- coding: utf-8 -*-
"""
backend.tests.test_account_service —— 店铺与账号管理服务单元测试
==============================================================
本文件用途：对 backend 店铺与账号管理业务服务（app.services.account_service）进行
单元测试，覆盖需求 3 的核心验收场景（service 层直接以内存 SQLite 会话测试）：

- upsert 幂等（需求 3.1 / 3.2）：同一用户相同 shop_id 多次提交仅 1 条记录，内容
  更新为最后一次写入。
- Cookie 加密存储与脱敏（需求 3.6）：account 表存密文（非明文），列表 / 详情响应
  不返回 Cookie 明文。
- 列表北京时间倒序 + 后端分页（需求 3.3）：返回 {list, total, page, page_size}，
  新店铺在前。
- 数据范围隔离（需求 3.7）：非管理员仅见本人店铺；管理员可见全部。
- 停用逻辑删除 + 断连通知（需求 3.5）：status 置 0、记录保留、断连通知被调用。

测试方案：pytest + 内存 SQLite（夹具见 conftest.py），断连通知经 monkeypatch 打桩
避免真实网络调用。
"""
from __future__ import annotations

# 导入店铺模型模块，确保其表登记进 Base.metadata（conftest 仅默认导入用户模型）。
import common.models.shop_models  # noqa: F401
import pytest
from app.services import account_service
from common.db.repository import Repository
from common.models.shop_models import Account, Shop
from common.models.user_models import SysUser
from common.utils.crypto import decrypt_text


@pytest.fixture(autouse=True)
def _mock_connection_notify(monkeypatch):
    """自动打桩连接启停通知，避免单元测试触发真实服务间 HTTP 调用（及其超时等待）。

    店铺新增 / 启用会调用 ``notify_connect``、停用会调用 ``notify_disconnect``，
    二者真实实现会向 websocket 服务发起 HTTP 请求；测试环境无该服务，若不打桩将
    每次走满超时（拖慢测试）。此处统一桩为成功返回，个别用例可再行覆盖以断言调用。
    """
    monkeypatch.setattr(account_service, "notify_connect", lambda **kw: True)
    monkeypatch.setattr(account_service, "notify_disconnect", lambda **kw: True)


def _make_user(db_session, seed_permissions, *, username: str, is_admin: bool):
    """在内存库创建一个用户并返回，便于按归属做隔离测试。"""
    role_id = (
        seed_permissions["role_admin_id"]
        if is_admin
        else seed_permissions["role_manager_id"]
    )
    user = SysUser(username=username, password_hash="x", role_id=role_id, status=1)
    db_session.add(user)
    db_session.flush()
    db_session.commit()
    return user


def test_upsert_shop_is_idempotent_for_same_business_key(db_session, seed_permissions):
    """upsert 幂等：同一用户相同 shop_id 多次提交仅 1 条记录（需求 3.2）。"""
    user = _make_user(db_session, seed_permissions, username="owner1", is_admin=False)

    r1 = account_service.upsert_shop(
        db_session,
        shop_id="SHOP_A",
        owner_user_id=user.id,
        shop_name="店铺A",
        operator_id=user.id,
    )
    assert r1.success is True

    # 再次以相同业务键提交，更新名称。
    r2 = account_service.upsert_shop(
        db_session,
        shop_id="SHOP_A",
        owner_user_id=user.id,
        shop_name="店铺A改名",
        operator_id=user.id,
    )
    assert r2.success is True

    # 仅 1 条记录，内容为最后一次写入。
    repo = Repository(Shop, db_session)
    assert repo.count(filters={"owner_user_id": user.id, "shop_id": "SHOP_A"}) == 1
    shop = repo.get_by(owner_user_id=user.id, shop_id="SHOP_A")
    assert shop.shop_name == "店铺A改名"


def test_upsert_shop_encrypts_cookie_and_hides_plaintext(db_session, seed_permissions):
    """Cookie 加密存储且响应不返回明文（需求 3.6）。"""
    user = _make_user(db_session, seed_permissions, username="owner2", is_admin=False)
    plain_cookie = "PDD_SESS=abc123; foo=bar"

    resp = account_service.upsert_shop(
        db_session,
        shop_id="SHOP_B",
        owner_user_id=user.id,
        cookies=plain_cookie,
        password="secret-pwd",
        operator_id=user.id,
    )
    assert resp.success is True
    # 响应中不含 Cookie / 密码明文（脱敏）。
    assert plain_cookie not in str(resp.data)
    assert "cookies" not in resp.data
    assert "password" not in resp.data

    # account 表存的是密文（非明文），但可解密还原。
    shop = Repository(Shop, db_session).get_by(owner_user_id=user.id, shop_id="SHOP_B")
    account = Repository(Account, db_session).get_by(shop_pk=shop.id, user_id=user.id)
    assert account is not None
    assert account.cookies_enc is not None
    assert account.cookies_enc != plain_cookie
    assert decrypt_text(account.cookies_enc) == plain_cookie


def test_list_shops_orders_by_beijing_time_desc_with_pagination(
    db_session, seed_permissions
):
    """列表按北京时间倒序 + 后端分页结构（需求 3.3）。"""
    user = _make_user(db_session, seed_permissions, username="owner3", is_admin=False)
    for i in range(3):
        account_service.upsert_shop(
            db_session,
            shop_id=f"SHOP_C{i}",
            owner_user_id=user.id,
            shop_name=f"店铺C{i}",
            operator_id=user.id,
        )

    resp = account_service.list_shops(
        db_session, current_user=user, page=1, page_size=20
    )
    assert resp.success is True
    data = resp.data
    assert {"list", "total", "page", "page_size"}.issubset(data.keys())
    assert data["total"] == 3
    # 倒序：最后创建的 SHOP_C2 在最前。
    assert data["list"][0]["shop_id"] == "SHOP_C2"
    # 列表脱敏：不含 Cookie 明文字段。
    for item in data["list"]:
        assert "cookies" not in item


def test_list_shops_data_scope_isolation_for_non_admin(db_session, seed_permissions):
    """数据范围隔离：非管理员仅见本人店铺，管理员可见全部（需求 3.7）。"""
    user_a = _make_user(db_session, seed_permissions, username="ua", is_admin=False)
    user_b = _make_user(db_session, seed_permissions, username="ub", is_admin=False)
    admin = _make_user(db_session, seed_permissions, username="adm", is_admin=True)

    account_service.upsert_shop(
        db_session, shop_id="A1", owner_user_id=user_a.id, operator_id=user_a.id
    )
    account_service.upsert_shop(
        db_session, shop_id="B1", owner_user_id=user_b.id, operator_id=user_b.id
    )

    # 非管理员 A 仅见本人店铺。
    resp_a = account_service.list_shops(db_session, current_user=user_a)
    assert resp_a.data["total"] == 1
    assert resp_a.data["list"][0]["shop_id"] == "A1"

    # 管理员可见全部。
    resp_admin = account_service.list_shops(db_session, current_user=admin)
    assert resp_admin.data["total"] == 2


def test_disable_shop_soft_deletes_and_notifies_disconnect(
    db_session, seed_permissions, monkeypatch
):
    """停用：逻辑删除（status=0、记录保留）并通知断连（需求 3.5）。"""
    user = _make_user(db_session, seed_permissions, username="owner4", is_admin=False)
    account_service.upsert_shop(
        db_session, shop_id="SHOP_D", owner_user_id=user.id, operator_id=user.id
    )
    shop = Repository(Shop, db_session).get_by(owner_user_id=user.id, shop_id="SHOP_D")

    # 打桩断连通知，记录是否被调用，避免真实网络请求。
    called = {}

    def _fake_notify(shop_pk, shop_id, owner_user_id, **kwargs):
        called["args"] = (shop_pk, shop_id, owner_user_id)
        return True

    monkeypatch.setattr(account_service, "notify_disconnect", _fake_notify)

    total_before = Repository(Shop, db_session).count()
    resp = account_service.disable_shop(db_session, shop.id, current_user=user)
    assert resp.success is True
    assert resp.data["status"] == account_service.SHOP_STATUS_DISABLED

    # 记录仍在库（逻辑删除），总数不变。
    assert Repository(Shop, db_session).count() == total_before
    assert Repository(Shop, db_session).get(shop.id).status == 0
    # 断连通知被调用。
    assert called["args"] == (shop.id, "SHOP_D", user.id)


def test_non_owner_cannot_disable_others_shop(db_session, seed_permissions, monkeypatch):
    """数据范围隔离：非管理员不可停用他人店铺（需求 3.7）。"""
    owner = _make_user(db_session, seed_permissions, username="o5", is_admin=False)
    other = _make_user(db_session, seed_permissions, username="x5", is_admin=False)
    account_service.upsert_shop(
        db_session, shop_id="SHOP_E", owner_user_id=owner.id, operator_id=owner.id
    )
    shop = Repository(Shop, db_session).get_by(owner_user_id=owner.id, shop_id="SHOP_E")

    monkeypatch.setattr(account_service, "notify_disconnect", lambda **kw: True)

    resp = account_service.disable_shop(db_session, shop.id, current_user=other)
    assert resp.success is False
    # 他人店铺仍为启用状态（未被越权停用）。
    assert Repository(Shop, db_session).get(shop.id).status == 1


def test_upsert_shop_notifies_connect_when_enabled(
    db_session, seed_permissions, monkeypatch
):
    """新增启用店铺：保存成功后通知 websocket 启动连接（需求 5.1）。"""
    user = _make_user(db_session, seed_permissions, username="owner6", is_admin=False)

    # 打桩启动连接通知，记录调用参数（覆盖 autouse 桩以断言）。
    called = {}

    def _fake_connect(shop_pk, shop_id, owner_user_id, **kwargs):
        called["args"] = (shop_pk, shop_id, owner_user_id)
        return True

    monkeypatch.setattr(account_service, "notify_connect", _fake_connect)

    resp = account_service.upsert_shop(
        db_session, shop_id="SHOP_F", owner_user_id=user.id, operator_id=user.id
    )
    assert resp.success is True

    shop = Repository(Shop, db_session).get_by(owner_user_id=user.id, shop_id="SHOP_F")
    # 启用店铺新增后触发启动连接通知。
    assert called["args"] == (shop.id, "SHOP_F", user.id)


def test_update_shop_enable_notifies_connect(
    db_session, seed_permissions, monkeypatch
):
    """重新启用店铺：通知 websocket 启动连接（需求 3.4 / 5.1）。"""
    user = _make_user(db_session, seed_permissions, username="owner7", is_admin=False)
    account_service.upsert_shop(
        db_session, shop_id="SHOP_G", owner_user_id=user.id, operator_id=user.id
    )
    shop = Repository(Shop, db_session).get_by(owner_user_id=user.id, shop_id="SHOP_G")
    # 先停用，再重新启用。
    account_service.disable_shop(db_session, shop.id, current_user=user)

    called = {}

    def _fake_connect(shop_pk, shop_id, owner_user_id, **kwargs):
        called["args"] = (shop_pk, shop_id, owner_user_id)
        return True

    monkeypatch.setattr(account_service, "notify_connect", _fake_connect)

    resp = account_service.update_shop(
        db_session, shop.id, current_user=user, enabled=True
    )
    assert resp.success is True
    assert Repository(Shop, db_session).get(shop.id).status == 1
    # 重新启用触发启动连接通知。
    assert called["args"] == (shop.id, "SHOP_G", user.id)
