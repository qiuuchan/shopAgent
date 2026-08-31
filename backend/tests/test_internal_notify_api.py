# -*- coding: utf-8 -*-
"""
backend.tests.test_internal_notify_api —— 系统事件通知内部接口单元测试（TIK-018）
==================================================================================
本文件用途：验证 ``/api/v1/internal/notify-events``（websocket 服务告警回调接收）：

- 密钥缺失 / 不符返回失败（X-Internal-Token 服务间鉴权，与 internal_chat 同款）；
- 密钥正确：转交 ``notify_service.push_system_event`` 按店铺已启用渠道推送并落
  ``pdd_notify_record``，渠道实际投递经打桩替换避免真实网络 IO（系统内部调用
  ``operator_id=None``，不做用户级数据隔离）。

测试方案：pytest + FastAPI TestClient + 内存 SQLite（夹具见 conftest.py）。
所有接口 HTTP 恒返回 200，业务成败由统一响应体表达。
"""
from __future__ import annotations

import pytest

from app.services import notify_service
from common.core.config import get_settings
from common.models.log_models import NotifyRecord
from common.models.setting_models import NotifyChannel

API_PREFIX = "/api/v1"
INTERNAL_EVENTS_URL = f"{API_PREFIX}/internal/notify-events"


def _headers(token: str | None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Internal-Token"] = token
    return headers


def test_internal_notify_rejects_bad_token(client):
    """密钥缺失 / 不符返回失败，不触发推送。"""
    for token in (None, "wrong-token"):
        resp = client.post(
            INTERNAL_EVENTS_URL,
            json={"event_type": "connection_disconnected", "content": "测试", "shop_pk": 1},
            headers=_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False


def test_internal_notify_pushes_via_shop_channels(client, db_session, monkeypatch):
    """密钥正确：经该店铺已启用渠道推送并落通知记录（operator_id=None 不隔离）。"""
    db_session.add_all(
        [
            NotifyChannel(
                channel_type="wecom",
                target="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
                enabled=True,
                shop_pk=1,
            ),
            NotifyChannel(channel_type="wecom", target="https://x.com", enabled=False, shop_pk=1),
        ]
    )
    db_session.commit()

    sent: list = []
    monkeypatch.setattr(
        notify_service,
        "send_via_channel",
        lambda channel_type, target, content: (sent.append((channel_type, target)), True, "发送成功")[1:],
    )

    token = get_settings().internal_service_token
    resp = client.post(
        INTERNAL_EVENTS_URL,
        json={
            "event_type": "connection_disconnected",
            "content": "店铺 TikTok 连接断开: 页面存活探测失败",
            "shop_pk": 1,
        },
        headers=_headers(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["total"] == 1
    assert body["data"]["success"] == 1
    # 仅启用渠道被投递，且记录落库。
    assert len(sent) == 1
    assert db_session.query(NotifyRecord).count() == 1


def test_internal_notify_invalid_event_type_rejected(client, db_session):
    """事件类型非法返回 success=false（不落记录）。"""
    token = get_settings().internal_service_token
    resp = client.post(
        INTERNAL_EVENTS_URL,
        json={"event_type": "unknown_event", "content": "测试", "shop_pk": 1},
        headers=_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert db_session.query(NotifyRecord).count() == 0


def test_internal_notify_reply_rate_event_accepted(client, db_session):
    """TIK-026 新事件类型 reply_rate_below_threshold 被白名单放行（无渠道也成功）。"""
    token = get_settings().internal_service_token
    resp = client.post(
        INTERNAL_EVENTS_URL,
        json={
            "event_type": "reply_rate_below_threshold",
            "content": "【回复率告警】店铺[测试店] 回复率 70.00%，低于阈值 85%",
            "shop_pk": 1,
        },
        headers=_headers(token),
    )
    assert resp.status_code == 200
    # 无渠道：推送动作完成（total=0），响应仍为成功（不拒绝事件类型）。
    assert resp.json()["success"] is True


def test_reply_rate_event_type_registered():
    """新事件类型已注册进 EVENT_TYPE_LABELS（含中文文案，供通知记录展示）。"""
    labels = notify_service.EVENT_TYPE_LABELS
    assert notify_service.EVENT_REPLY_RATE_BELOW_THRESHOLD == "reply_rate_below_threshold"
    assert labels["reply_rate_below_threshold"] == "回复率跌破阈值"
