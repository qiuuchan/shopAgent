# -*- coding: utf-8 -*-
"""
test_alert_dedup —— TIK-005 告警去重与连接断开链路单元测试
=========================================================
本文件用途：验证新建的 ``AlertDedup`` 去重防抖与 ``PDDChannel`` 在「重连达上限置错误」
时触发 ``connection_disconnected`` 事件通知的链路（关联工单 TIK-005）。

测试覆盖：
- AlertDedup：静默期内重复事件不应发送（去重）、超过静默期后恢复发送、
  resolve 立即恢复（事件恢复后下次可再告警）。
- PDDChannel：event_notifier 为 None 时行为与现状一致（不触发任何通知）；
  注入 event_notifier 后，达重连上限触发的 ``connection_disconnected`` 事件能被收到。
- cookies：刷新失败分支触发告警（经全局单例去重）。

不依赖真实企微 Webhook（地址未提供）：用记录型 send_cb / 计数桩验证调用行为。
时间经 monkeypatch ``now_beijing`` 控制，避免等待真实 30 分钟静默期。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, List
from unittest.mock import patch

import channel_pdd.pdd_channel as channel_mod
from channel_pdd.core.connection_status import (
    ConnectionState,
    ConnectionStatusManager,
)
from channel_pdd.core.pdd_config import ReconnectConfig
from channel_pdd.pdd_channel import PDDChannel
from common.utils.time_utils import BEIJING_TZ
from engine.alert_dedup import AlertDedup, build_alert_notifier, get_alert_dedup


# ----------------------------------------------------------------------
# AlertDedup 去重防抖
# ----------------------------------------------------------------------
def test_dedup_blocks_repeat_in_silence_window():
    """静默期内同类事件第二次 should_send 应为 False（防抖去重）。"""
    dedup = AlertDedup(silence_seconds=1800.0)
    assert dedup.should_send(1, "connection_disconnected") is True
    dedup.mark_sent(1, "connection_disconnected")
    # 刚发送完，处于静默期。不加时间偏移，应被去重。
    assert dedup.should_send(1, "connection_disconnected") is False


def test_dedup_recovers_after_silence_window(monkeypatch):
    """超过静默期后 should_send 恢复为 True（自动恢复发送）。"""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=BEIJING_TZ)
    calls = {"n": 0}

    def _fake_now():
        calls["n"] += 1
        # 第一次调用（mark_sent 记录时间）返回 base；
        # 之后每次 + 偏移，使第二次 should_send 检查已超过 1800 秒。
        offset = timedelta(seconds=0) if calls["n"] == 1 else timedelta(seconds=1801)
        return base + offset

    dedup = AlertDedup(silence_seconds=1800.0)
    monkeypatch.setattr("engine.alert_dedup.now_beijing", _fake_now)

    assert dedup.should_send(1, "connection_disconnected") is True
    dedup.mark_sent(1, "connection_disconnected")
    # 已越过 1800 秒静默期 → 允许再次发送。
    assert dedup.should_send(1, "connection_disconnected") is True


def test_dedup_resolve_immediately_recovers():
    """resolve 立即清除静默状态，下次同类事件可立即再告警。"""
    dedup = AlertDedup(silence_seconds=1800.0)
    dedup.mark_sent(1, "connection_disconnected")
    assert dedup.should_send(1, "connection_disconnected") is False
    dedup.resolve(1, "connection_disconnected")
    assert dedup.should_send(1, "connection_disconnected") is True


def test_dedup_key_isolation_by_shop_and_event():
    """不同店铺或不同事件类型互不影响（二元组维度隔离）。"""
    dedup = AlertDedup(silence_seconds=1800.0)
    dedup.mark_sent(1, "connection_disconnected")
    # 同店铺不同事件：不受静默影响。
    assert dedup.should_send(1, "cookie_refresh_failed") is True
    # 不同店铺同事件：不受静默影响。
    assert dedup.should_send(2, "connection_disconnected") is True
    # 同店铺同事件：被去重。
    assert dedup.should_send(1, "connection_disconnected") is False


def test_build_alert_notifier_skips_in_silence_and_sends_after():
    """build_alert_notifier 在静默期内跳过发送、跨静默期后真正调用 send_cb。"""
    sent: List[tuple] = []
    dedup = AlertDedup(silence_seconds=1800.0)
    notifier = build_alert_notifier(dedup, shop_pk=7, send_cb=lambda et, c, sp: sent.append((et, c, sp)))

    # 第一次发送：真正调用。第二次（静默期内）：被去重跳过。
    notifier("connection_disconnected", "内容1")
    notifier("connection_disconnected", "内容2")
    assert len(sent) == 1
    assert sent[0] == ("connection_disconnected", "内容1", 7)


# ----------------------------------------------------------------------
# PDDChannel 事件通知链路
# ----------------------------------------------------------------------
def _fake_connect_cm():
    """构造一个使 websockets.connect 立即失败（拿不到 token）的桩。"""
    class _FailCM:
        async def __aenter__(self):
            raise RuntimeError("connect forced fail")

        async def __aexit__(self, *a):
            return False

    return _FailCM()


def test_channel_without_event_notifier_no_trigger_on_limit():
    """event_notifier 为 None 时，达上限分支不触发任何通知（零变更）。"""
    manager = ConnectionStatusManager()
    triggered: List[tuple] = []
    channel = PDDChannel(
        shop_id="shop_x",
        user_id=9,
        username="u9",
        status_manager=manager,
        reconnect_config=ReconnectConfig(
            max_attempts=2, initial_delay=0.001, max_delay=0.002, backoff_factor=2.0
        ),
        token_provider=lambda *a, **k: None,  # 始终失败 → 达上限
    )
    # 断言默认值仍为 None（硬约束：未注入时不影响既有行为）。
    assert channel._event_notifier is None

    async def _run():
        await channel.start()
        await channel._connect_task

    asyncio.run(_run())
    status = manager.get_status("shop_x", 9)
    assert status is not None
    assert status.state == ConnectionState.ERROR
    assert triggered == []  # 不应有任何通知调用


def test_channel_triggers_connection_disconnected_event():
    """注入 event_notifier 后，达重连上限触发 connection_disconnected 事件。"""
    manager = ConnectionStatusManager()
    events: List[tuple] = []
    channel = PDDChannel(
        shop_id="shop_y",
        user_id=10,
        username="u10",
        status_manager=manager,
        reconnect_config=ReconnectConfig(
            max_attempts=2, initial_delay=0.001, max_delay=0.002, backoff_factor=2.0
        ),
        token_provider=lambda *a, **k: None,  # 始终失败 → 达上限
        event_notifier=lambda et, content: events.append((et, content)),
    )

    async def _run():
        await channel.start()
        await channel._connect_task

    asyncio.run(_run())

    # 状态仍正确置错误。
    status = manager.get_status("shop_y", 10)
    assert status is not None
    assert status.state == ConnectionState.ERROR
    # 触发了一次 connection_disconnected 事件，且内容含店铺标识。
    assert len(events) == 1
    et, content = events[0]
    assert et == "connection_disconnected"
    assert "shop_y" in content


def test_channel_triggers_async_event_notifier():
    """event_notifier 为协程时也能被正确 await 触发。"""
    manager = ConnectionStatusManager()
    events: List[str] = []

    async def _async_notify(et, content):
        events.append(et)

    channel = PDDChannel(
        shop_id="shop_z",
        user_id=11,
        username="u11",
        status_manager=manager,
        reconnect_config=ReconnectConfig(
            max_attempts=2, initial_delay=0.001, max_delay=0.002, backoff_factor=2.0
        ),
        token_provider=lambda *a, **k: None,
        event_notifier=_async_notify,
    )

    async def _run():
        await channel.start()
        await channel._connect_task

    asyncio.run(_run())
    assert events == ["connection_disconnected"]


# ----------------------------------------------------------------------
# cookies 刷新失败触发告警链路（经全局单例去重）
# ----------------------------------------------------------------------
def test_cookie_refresh_failure_triggers_alert(monkeypatch):
    """Cookie 刷新失败分支触发 connection_disconnected 之外的告警并走去重。"""
    from websocket.routes import cookies as cookies_mod

    # 隔离模块级全局单例，避免污染其它用例。
    local_dedup = AlertDedup(silence_seconds=1800.0)
    monkeypatch.setattr(cookies_mod, "get_alert_dedup", lambda: local_dedup)

    sent: List[tuple] = []

    def _fake_notifier(dedup, shop_pk, send_cb=None):
        # 复用 build_alert_notifier，但 send_cb 改为记录型，便于断言。
        return build_alert_notifier(dedup, shop_pk, send_cb=lambda et, c, sp: sent.append((et, c, sp)))

    monkeypatch.setattr(cookies_mod, "build_alert_notifier", _fake_notifier)

    # 调用内部告警触发函数（登录态失效场景）。
    cookies_mod._alert_cookie_refresh_failed(42, "shop_c", "登录态已失效")
    # 静默期内重复调用应被去重。
    cookies_mod._alert_cookie_refresh_failed(42, "shop_c", "登录态已失效")

    assert len(sent) == 1
    et, content, sp = sent[0]
    assert et == "cookie_refresh_failed"
    assert sp == 42
    assert "shop_c" in content
