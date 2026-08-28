# -*- coding: utf-8 -*-
"""
test_routes_platform —— websocket 路由平台分派（TIK-016）单元测试
=================================================================
本文件用途：验证 login / messages / cookies 三个路由的 platform 分派逻辑：
- login：按 platform 分派到 login_tiktok / login_pdd，缺省 pdd 走原 PDD 路径；
- messages：TikTok 店铺复用活跃通道 TikTokSender 手动发送（无连接 → 失败「未连接」、
  有连接 → 发送且跳过最小随机间隔），缺省/查不到走原 PDD 路径；
- cookies：TikTok 店铺短路跳过刷新（success + skipped=True），不触发凭据加载/刷新；
- 缺省 platform='pdd' 时三路由 PDD 路径行为零变更、返回体结构不变。

测试不依赖真实数据库或网络：以 monkeypatch 替换平台分派相关依赖，路由函数
直接在 asyncio.run 内调用（构造 payload 实例）。告警链路（TIK-005）不在此测试范围。
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import contextmanager

import pytest

import websocket.routes.login as login_route
import websocket.routes.messages as messages_route
import websocket.routes.cookies as cookies_route
from websocket.routes.login import PasswordLoginRequest, login_by_password
from websocket.routes.messages import SendMessageRequest, send_message
from websocket.routes.cookies import RefreshCookieRequest, refresh_cookie


# ----------------------------------------------------------------------
# 通用假对象 / 夹具
# ----------------------------------------------------------------------
@contextmanager
def _fake_scope():
    """替身 session_scope：产出占位会话对象，避免触碰真实数据库。"""
    yield object()


class _FakeShop:
    """可配置 platform 的替身店铺对象。"""

    def __init__(self, platform: str):
        self.platform = platform


class _CallTracker:
    """记录被调用情况，便于断言分派正确性。"""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self

    def called(self) -> bool:
        return len(self.calls) > 0


async def _async_fake_info(*_a, **_k):
    """占位异步函数，返回非 None 信息（模拟登录成功）。"""
    return {"cookies": "x", "shop_id": "1", "shop_name": "s"}


async def _async_fake_none(*_a, **_k):
    """占位异步函数，返回 None（模拟登录失败占位）。"""
    return None


def _make_async_tracker(tracker, retval):
    """构造记录调用并返回固定值的异步替身。"""

    async def _inner(*args, **kwargs):
        tracker(*args, **kwargs)
        return retval

    return _inner


# ----------------------------------------------------------------------
# 1. login 分派
# ----------------------------------------------------------------------
def test_login_dispatch_tiktok(monkeypatch):
    """platform='tiktok' 时调用 login_tiktok，不调用 login_pdd。"""
    tiktok = _CallTracker()
    pdd = _CallTracker()
    monkeypatch.setattr(login_route, "login_tiktok",
                        _make_async_tracker(tiktok, {"cookies": "x", "shop_id": "1", "shop_name": "s"}))
    monkeypatch.setattr(login_route.pdd_login, "login_pdd",
                        _make_async_tracker(pdd, {"cookies": "x", "shop_id": "1", "shop_name": "s"}))

    resp = asyncio.run(login_by_password(
        PasswordLoginRequest(username="u", password="p", platform="tiktok")
    ))
    assert tiktok.called()
    assert not pdd.called()
    assert resp.success is True  # login_tiktok 被替身为返回信息


def test_login_dispatch_default_pdd(monkeypatch):
    """缺省 platform（'pdd'）时调用 login_pdd，不调用 login_tiktok。"""
    tiktok = _CallTracker()
    pdd = _CallTracker()
    monkeypatch.setattr(login_route, "login_tiktok",
                        _make_async_tracker(tiktok, None))
    monkeypatch.setattr(login_route.pdd_login, "login_pdd",
                        _make_async_tracker(pdd, {"cookies": "x", "shop_id": "1", "shop_name": "s"}))

    resp = asyncio.run(login_by_password(
        PasswordLoginRequest(username="u", password="p")
    ))
    assert pdd.called()
    assert not tiktok.called()
    assert resp.success is True


# ----------------------------------------------------------------------
# 2. messages TikTok 分派（Phase 2：复用活跃通道 TikTokSender 手动发送）
# ----------------------------------------------------------------------
def test_messages_tiktok_no_connection(monkeypatch):
    """platform='tiktok' 且店铺无活跃连接 → 失败「未连接」，不实例化 SendMessage。"""
    sender_tracker = _CallTracker()

    class _FakeSendMessage:
        def __init__(self, *a, **k):
            sender_tracker(*a, **k)
            self._k = k

        def send_text(self, *a, **k):
            return {"success": True}

    monkeypatch.setattr(messages_route, "session_scope", _fake_scope)
    monkeypatch.setattr(messages_route, "Repository",
                        lambda model, session: type("R", (), {
                            "get_by": lambda self, **kw: _FakeShop("tiktok")
                        })())
    monkeypatch.setattr(messages_route, "SendMessage", _FakeSendMessage)
    # 无活跃连接：connection_registry.get 返回 None。
    monkeypatch.setattr(messages_route, "get_connection", lambda *a, **k: None)

    resp = asyncio.run(send_message(SendMessageRequest(
        shop_pk=1, shop_id="s", owner_user_id=1, recipient_uid="c", content="hi"
    )))
    assert not resp.success
    assert "未连接" in resp.message
    assert not sender_tracker.called()  # 未实例化 SendMessage，零 PDD 网络/逻辑副作用


def test_messages_tiktok_sends_via_channel_sender(monkeypatch):
    """platform='tiktok' 且店铺有活跃连接 → 复用通道 sender 发送（跳过最小随机间隔）。"""
    send_tracker = _CallTracker()

    class _FakeSender:
        def send_text(self, *a, **k):
            send_tracker(*a, **k)
            return {"success": True}

    class _FakeChannel:
        sender = _FakeSender()

    monkeypatch.setattr(messages_route, "session_scope", _fake_scope)
    monkeypatch.setattr(messages_route, "Repository",
                        lambda model, session: type("R", (), {
                            "get_by": lambda self, **kw: _FakeShop("tiktok")
                        })())
    monkeypatch.setattr(messages_route, "get_connection",
                        lambda *a, **k: _FakeChannel())

    resp = asyncio.run(send_message(SendMessageRequest(
        shop_pk=1, shop_id="s", owner_user_id=1, recipient_uid="c", content="hi"
    )))
    assert resp.success is True
    assert send_tracker.called()
    # 手动发送：跳过发送最小随机间隔（enforce_interval=False），且未走 PDD SendMessage。
    _, kwargs = send_tracker.calls[0]
    assert kwargs.get("enforce_interval") is False


def test_messages_tiktok_channel_without_sender(monkeypatch):
    """platform='tiktok' 通道存在但无 sender（未装配）→ 失败提示未连接。"""
    class _FakeChannel:
        sender = None

    monkeypatch.setattr(messages_route, "session_scope", _fake_scope)
    monkeypatch.setattr(messages_route, "Repository",
                        lambda model, session: type("R", (), {
                            "get_by": lambda self, **kw: _FakeShop("tiktok")
                        })())
    monkeypatch.setattr(messages_route, "get_connection",
                        lambda *a, **k: _FakeChannel())

    resp = asyncio.run(send_message(SendMessageRequest(
        shop_pk=1, shop_id="s", owner_user_id=1, recipient_uid="c", content="hi"
    )))
    assert not resp.success
    assert "未连接" in resp.message


def test_messages_default_pdd_path(monkeypatch):
    """缺省 / 查不到平台时走原 PDD 路径（实例化 SendMessage 并发送）。"""
    sender_tracker = _CallTracker()

    class _FakeSendMessage:
        def __init__(self, *a, **k):
            sender_tracker(*a, **k)

        def send_text(self, *a, **k):
            return {"success": True}

    monkeypatch.setattr(messages_route, "session_scope", _fake_scope)
    # 缺省：get_by 返回 platform='pdd' 的店铺 → 走 PDD 路径
    monkeypatch.setattr(messages_route, "Repository",
                        lambda model, session: type("R", (), {
                            "get_by": lambda self, **kw: _FakeShop("pdd")
                        })())
    monkeypatch.setattr(messages_route, "SendMessage", _FakeSendMessage)

    resp = asyncio.run(send_message(SendMessageRequest(
        shop_pk=1, shop_id="s", owner_user_id=1, recipient_uid="c", content="hi"
    )))
    assert sender_tracker.called()  # 经 PDD 路径实例化发送器
    assert resp.success is True


def test_messages_unknown_shop_falls_back_pdd(monkeypatch):
    """查不到店铺（None）时记 warning 并回退 PDD 路径。"""
    sender_tracker = _CallTracker()

    class _FakeSendMessage:
        def __init__(self, *a, **k):
            sender_tracker(*a, **k)

        def send_text(self, *a, **k):
            return {"success": True}

    monkeypatch.setattr(messages_route, "session_scope", _fake_scope)
    monkeypatch.setattr(messages_route, "Repository",
                        lambda model, session: type("R", (), {
                            "get_by": lambda self, **kw: None
                        })())
    monkeypatch.setattr(messages_route, "SendMessage", _FakeSendMessage)

    resp = asyncio.run(send_message(SendMessageRequest(
        shop_pk=999, shop_id="s", owner_user_id=1, recipient_uid="c", content="hi"
    )))
    assert sender_tracker.called()  # 回退 PDD 路径


# ----------------------------------------------------------------------
# 3. cookies TikTok 跳过
# ----------------------------------------------------------------------
def test_cookies_tiktok_skipped(monkeypatch):
    """platform='tiktok' 时 success + skipped=True，且不调凭据加载/刷新。"""
    load_tracker = _CallTracker()
    refresh_tracker = _CallTracker()
    update_tracker = _CallTracker()

    monkeypatch.setattr(cookies_route, "load_account_credentials",
                        lambda *a, **k: (load_tracker(*a, **k) or ("u", "p")))
    monkeypatch.setattr(cookies_route.pdd_login, "refresh_pdd_cookies",
                        _make_async_tracker(refresh_tracker, {"cookies": "x"}))
    monkeypatch.setattr(cookies_route, "update_account_cookies",
                        lambda *a, **k: update_tracker(*a, **k))

    resp = asyncio.run(refresh_cookie(RefreshCookieRequest(
        shop_pk=1, shop_id="s", owner_user_id=1, platform="tiktok"
    )))
    assert resp.success is True
    assert resp.data.get("skipped") is True
    assert not load_tracker.called()
    assert not refresh_tracker.called()
    assert not update_tracker.called()


def test_cookies_default_pdd_path(monkeypatch):
    """缺省 platform（'pdd'）时按原路径执行凭据加载与刷新。"""
    load_tracker = _CallTracker()
    refresh_tracker = _CallTracker()

    monkeypatch.setattr(cookies_route, "load_account_credentials",
                        lambda *a, **k: (load_tracker(*a, **k), ("u", "p"))[1])
    # 刷新成功返回含 cookies 的字典
    async def _refresh(*_a, **_k):
        refresh_tracker(*_a, **_k)
        return {"cookies": "x"}

    monkeypatch.setattr(cookies_route.pdd_login, "refresh_pdd_cookies", _refresh)
    monkeypatch.setattr(cookies_route, "update_account_cookies",
                        lambda *a, **k: None)

    resp = asyncio.run(refresh_cookie(RefreshCookieRequest(
        shop_pk=1, shop_id="s", owner_user_id=1
    )))
    assert load_tracker.called()
    assert refresh_tracker.called()
    assert resp.success is True
    assert "skipped" not in resp.data  # 返回体结构保持原 PDD 形态


__all__ = [
    "test_login_dispatch_tiktok",
    "test_login_dispatch_default_pdd",
    "test_messages_tiktok_no_connection",
    "test_messages_tiktok_sends_via_channel_sender",
    "test_messages_tiktok_channel_without_sender",
    "test_messages_default_pdd_path",
    "test_messages_unknown_shop_falls_back_pdd",
    "test_cookies_tiktok_skipped",
    "test_cookies_default_pdd_path",
]
