# -*- coding: utf-8 -*-
"""
backend.tests.test_notify_api —— 通知渠道与消息通知接口/服务单元测试
==================================================================
本文件用途：对 backend 通知渠道与消息通知（app.api.routes.notify 与
app.services.notify_service）进行单元测试，覆盖需求 18（通知渠道与消息通知）
核心验收场景：

- 配置通知渠道成功并持久化（需求 18.1）。
- 渠道类型 / 目标地址非法返回 success=false（入参校验）。
- 测试发送：渠道发送失败时仍记通知记录、HTTP 恒 200、不抛异常（需求 18.2/18.4）。
- 系统事件推送：经全部已启用渠道推送，单渠道失败不中断主流程（需求 18.3/18.4）。
- 通知记录后端分页（需求 18.5）。
- 通知渠道类型枚举字典查询（需求 18.x / 24.7）。
- 无权限访问被拒（需求 2.4）。

测试方案：pytest + FastAPI TestClient + 内存 SQLite（夹具见 conftest.py）。
本模块为 notify 资源补充权限点与授权用户，并预置渠道类型字典。所有接口 HTTP
恒返回 200，业务成败由统一响应体表达。渠道实际投递经打桩替换，避免真实网络 IO。
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from app.core.business_codes import CODE_FORBIDDEN, MSG_FORBIDDEN
from app.services import notify_service
from common.models.log_models import NotifyRecord
from common.models.setting_models import NotifyChannel, SysDict
from common.models.shop_models import Shop
from common.models.user_models import (
    SysPermission,
    SysRole,
    SysRolePermission,
    SysUser,
)
from common.utils.security import hash_password

API_PREFIX = "/api/v1"
LOGIN_URL = f"{API_PREFIX}/login"
CHANNELS_URL = f"{API_PREFIX}/notify/channels"
EVENTS_URL = f"{API_PREFIX}/notify/events"
RECORDS_URL = f"{API_PREFIX}/notify/records"
CHANNEL_TYPES_URL = f"{API_PREFIX}/notify/channel-types"


# ----------------------------------------------------------------------
# 夹具：权限点 + 授权 / 无权限用户 + 渠道类型字典
# ----------------------------------------------------------------------
@pytest.fixture()
def notify_env(db_session):
    """预置 notify + shop 资源权限、用户与渠道类型字典。

    构造：
    - nt 角色：被授予 notify 资源（记录/字典/事件）与 shop 资源（店铺级渠道配置）
      的 view/create/update 权限；nt_user 为其下授权用户（非管理员）；
    - 另一普通用户 other_user（无任何权限，用于无权限场景）；
    - 渠道类型字典 channel_type: email/webhook/wecom。
    """
    role_nt = SysRole(role_name="通知管理员", is_admin=False, status=1)
    role_other = SysRole(role_name="其它用户", is_admin=False, status=1)
    db_session.add_all([role_nt, role_other])
    db_session.flush()

    perm_ids = []
    # 通知记录/字典/事件用 notify 资源；店铺级通知渠道用 shop 资源（规范 42a）。
    for resource in ("notify", "shop"):
        for action in ("view", "create", "update"):
            perm = SysPermission(resource_key=resource, action=action)
            db_session.add(perm)
            db_session.flush()
            perm_ids.append(perm.id)
    for pid in perm_ids:
        db_session.add(SysRolePermission(role_id=role_nt.id, permission_id=pid))
    db_session.flush()

    nt_password = "nt-password-123"
    other_password = "other-password-123"
    nt_user = SysUser(
        username="nt_user",
        password_hash=hash_password(nt_password),
        role_id=role_nt.id,
        status=1,
    )
    other_user = SysUser(
        username="other_user",
        password_hash=hash_password(other_password),
        role_id=role_other.id,
        status=1,
    )
    db_session.add_all([nt_user, other_user])
    db_session.flush()

    # 预置一家归属 nt_user 的店铺，供店铺级通知渠道的数据范围隔离校验（需求 3.7）。
    shop = Shop(shop_id="pdd_nt", shop_name="通知店铺", owner_user_id=nt_user.id, status=1)
    db_session.add(shop)
    db_session.flush()

    for idx, (key, label) in enumerate(
        (("email", "邮件"), ("webhook", "Webhook"), ("wecom", "企业微信")),
        start=1,
    ):
        db_session.add(
            SysDict(
                dict_type="channel_type",
                dict_key=key,
                dict_label=label,
                order_no=idx,
                enabled=True,
            )
        )
    db_session.flush()
    db_session.commit()

    return {
        "nt_user": {"username": "nt_user", "password": nt_password, "id": nt_user.id},
        "other_user": {
            "username": "other_user",
            "password": other_password,
            "id": other_user.id,
        },
        "shop_pk": shop.id,
    }


def _login_token(client, username: str, password: str) -> str:
    """登录并返回访问令牌（断言登录成功）。"""
    resp = client.post(LOGIN_URL, json={"username": username, "password": password})
    body = resp.json()
    assert body["success"] is True, body
    return body["data"]["token"]


def _auth_header(token: str) -> dict:
    """构造 Bearer 鉴权请求头。"""
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------
# 接口测试
# ----------------------------------------------------------------------
def test_create_channel_success(client, notify_env):
    """配置通知渠道成功：合法入参保存并返回渠道信息（需求 18.1）。"""
    token = _login_token(
        client, notify_env["nt_user"]["username"], notify_env["nt_user"]["password"]
    )
    shop_pk = notify_env["shop_pk"]
    resp = client.post(
        CHANNELS_URL,
        headers=_auth_header(token),
        json={
            "shop_pk": shop_pk,
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["success"] is True
    assert body["data"]["channel_type"] == "webhook"
    assert body["data"]["target"] == "https://example.com/hook"
    assert body["data"]["shop_pk"] == shop_pk


def test_create_channel_denied_for_other_shop(client, notify_env, db_session):
    """数据范围隔离：非管理员不可为他人店铺配置通知渠道（需求 3.7）。"""
    # 预置一家归属他人的店铺。
    other_shop = Shop(
        shop_id="pdd_other", shop_name="他人店铺", owner_user_id=999999, status=1
    )
    db_session.add(other_shop)
    db_session.commit()

    token = _login_token(
        client, notify_env["nt_user"]["username"], notify_env["nt_user"]["password"]
    )
    resp = client.post(
        CHANNELS_URL,
        headers=_auth_header(token),
        json={
            "shop_pk": other_shop.id,
            "channel_type": "webhook",
            "target": "https://example.com/hook",
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["success"] is False
    assert body["code"] == CODE_FORBIDDEN


def test_create_channel_invalid_type_rejected(client, notify_env):
    """渠道类型非法返回 success=false（入参校验）。"""
    token = _login_token(
        client, notify_env["nt_user"]["username"], notify_env["nt_user"]["password"]
    )
    resp = client.post(
        CHANNELS_URL,
        headers=_auth_header(token),
        json={"shop_pk": 1, "channel_type": "sms", "target": "x"},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["success"] is False


def test_test_channel_failure_records_and_not_break(client, notify_env, monkeypatch):
    """测试发送失败仍记通知记录、HTTP 恒 200、不抛异常（需求 18.2/18.4）。"""
    token = _login_token(
        client, notify_env["nt_user"]["username"], notify_env["nt_user"]["password"]
    )
    created = client.post(
        CHANNELS_URL,
        headers=_auth_header(token),
        json={"shop_pk": notify_env["shop_pk"], "channel_type": "webhook", "target": "https://example.com/hook"},
    ).json()
    channel_id = created["data"]["id"]

    # 打桩渠道投递为失败，避免真实网络 IO（需求 18.4 失败不中断）。
    monkeypatch.setattr(
        notify_service, "send_via_channel", lambda *a, **k: (False, "模拟发送失败")
    )
    resp = client.post(
        f"{CHANNELS_URL}/{channel_id}/test",
        headers=_auth_header(token),
        json={"content": "测试一下"},
    )
    body = resp.json()
    # HTTP 恒 200，业务失败由统一响应体表达，不抛异常。
    assert resp.status_code == 200
    assert body["success"] is False
    # 通知记录已写入（记日志不中断）。
    records = client.get(RECORDS_URL, headers=_auth_header(token)).json()
    assert records["data"]["total"] >= 1


def test_push_event_single_channel_failure_not_break(
    db_session, notify_env, monkeypatch
):
    """系统事件推送：单渠道失败不中断主流程，逐条记录（需求 18.3/18.4）。"""
    # 预置两个已启用渠道。
    db_session.add_all(
        [
            NotifyChannel(channel_type="webhook", target="https://a.com", enabled=True),
            NotifyChannel(channel_type="wecom", target="https://b.com", enabled=True),
        ]
    )
    db_session.commit()

    # 打桩：一个渠道抛异常、一个成功，验证异常被兜底不中断。
    def _fake_send(channel_type, target, content):
        if channel_type == "webhook":
            raise RuntimeError("模拟渠道异常")
        return True, "发送成功"

    monkeypatch.setattr(notify_service, "send_via_channel", _fake_send)
    resp = notify_service.push_system_event(
        db_session,
        event_type=notify_service.EVENT_CONNECTION_DISCONNECTED,
        content="店铺连接断开",
    )
    assert resp.success is True
    assert resp.data["total"] == 2
    assert resp.data["success"] == 1
    assert resp.data["failed"] == 1
    # 两条通知记录均落库。
    assert db_session.query(NotifyRecord).count() == 2


def test_push_event_invalid_type_rejected(db_session, notify_env):
    """系统事件类型非法返回 success=false。"""
    resp = notify_service.push_system_event(
        db_session, event_type="unknown_event", content="x"
    )
    assert resp.success is False


def test_list_channel_types(client, notify_env):
    """通知渠道类型枚举字典查询：返回 channel_type 中文文案（需求 18.x）。"""
    token = _login_token(
        client, notify_env["nt_user"]["username"], notify_env["nt_user"]["password"]
    )
    resp = client.get(CHANNEL_TYPES_URL, headers=_auth_header(token))
    body = resp.json()
    assert resp.status_code == 200
    assert body["success"] is True
    labels = {item["key"]: item["label"] for item in body["data"]}
    assert labels["email"] == "邮件"
    assert labels["wecom"] == "企业微信"


def test_notify_access_denied_for_unauthorized(client, notify_env):
    """无权限访问被拒：未授权用户调用返回「无访问权限」（需求 2.4）。"""
    token = _login_token(
        client,
        notify_env["other_user"]["username"],
        notify_env["other_user"]["password"],
    )
    resp = client.get(CHANNEL_TYPES_URL, headers=_auth_header(token))
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == CODE_FORBIDDEN
    assert body["message"] == MSG_FORBIDDEN


# ----------------------------------------------------------------------
# 服务层测试
# ----------------------------------------------------------------------
def test_send_via_channel_wecom_uses_qyapi_payload(monkeypatch):
    """企微渠道按群机器人协议封装 msgtype/text（企微 webhook 要求）。

    2026-08-28 实测：企微群机器人仅接受
    ``{"msgtype":"text","text":{"content":...}}``，纯 ``{"content":...}`` 会被
    拒绝；通用 webhook 渠道仍保持纯文本格式不变。
    """
    captured: dict = {}

    class _FakeResp:
        status = 200

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return b'{"errcode":0,"errmsg":"ok"}'

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *exc) -> None:
            return None

    def _fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    ok, detail = notify_service.send_via_channel(
        "wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake", "你好"
    )
    assert ok is True, detail
    assert captured["url"].startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook")
    assert json.loads(captured["body"]) == {
        "msgtype": "text",
        "text": {"content": "你好"},
    }


def test_send_via_channel_webhook_keeps_plain_payload(monkeypatch):
    """通用 webhook 渠道保持纯文本载荷 {\"content\": ...} 不变。"""
    captured: dict = {}

    class _FakeResp:
        status = 200

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return b'{"errcode":0,"errmsg":"ok"}'

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *exc) -> None:
            return None

    def _fake_urlopen(request, timeout=None):
        captured["body"] = request.data.decode("utf-8")
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    ok, detail = notify_service.send_via_channel("webhook", "https://a.com/hook", "你好")
    assert ok is True, detail
    assert json.loads(captured["body"]) == {"content": "你好"}


def test_send_via_channel_wecom_business_error_detected(monkeypatch):
    """企微网关业务失败（key 失效/消息格式错）仍返回 2xx，须按 errcode 判失败。

    2026-08-28 实测：企微 webhook 对无效 key 返回 HTTP 200 但 body 带
    errcode!=0，仅看状态码会误报成功导致告警落库失真。
    """

    class _FakeResp:
        status = 200

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return b'{"errcode":93000,"errmsg":"invalid webhook url, hint: fake"}'

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *exc) -> None:
            return None

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: _FakeResp(),
    )
    ok, detail = notify_service.send_via_channel(
        "wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=bad", "你好"
    )
    assert ok is False
    assert "业务错误" in detail


def test_service_test_channel_records_failure(db_session, notify_env, monkeypatch):
    """服务层：测试发送失败写入失败结果的通知记录（需求 18.4）。"""
    channel = NotifyChannel(
        channel_type="webhook", target="https://a.com", enabled=True
    )
    db_session.add(channel)
    db_session.flush()

    monkeypatch.setattr(
        notify_service, "send_via_channel", lambda *a, **k: (False, "失败")
    )
    resp = notify_service.test_notify_channel(db_session, channel.id, content="x")
    assert resp.success is False
    record = db_session.query(NotifyRecord).first()
    assert record is not None
    assert record.send_result == notify_service.SEND_RESULT_FAILED
