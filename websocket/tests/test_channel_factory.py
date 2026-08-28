# -*- coding: utf-8 -*-
"""
test_channel_factory —— 工厂分派回归（TIK-014 更新）
====================================================
验证 connection_manager.create_channel 按 platform 分派：
- 'pdd' / 缺省 → PDDChannel（含 TIK-005 的 event_notifier 注入，行为不变）；
- 'tiktok' → TikTokChannel 真实现（TIK-014 联调落地，替代原占位桩）；
- 非法平台回退 PDD 路径。

不跑全量，仅本文件用例由 CI 选择性执行（pytest -k channel_factory）。

实现约束：日志禁 debug（38）、中文注释（37）、复用既有注入模式。
"""
from __future__ import annotations

import asyncio

import pytest

from channel_base import PLATFORM_PDD, PLATFORM_TIKTOK
from channel_pdd import connection_manager
from channel_pdd.connection_manager import (
    create_channel,
)
from channel_pdd.pdd_channel import PDDChannel
from channel_tiktok.tiktok_channel import TikTokChannel
from engine.alert_dedup import build_alert_notifier, get_alert_dedup


def _make_event_notifier(shop_pk: int):
    """构造一个可断言调用的事件通知器（捕获是否触发 connection_disconnected）。"""
    calls: list[tuple] = []

    def _notify(event_type: str, content: str) -> None:
        calls.append((event_type, content))

    return _notify, calls


def test_create_channel_default_is_pdd():
    """缺省 platform 应返回 PDDChannel（PDD 路径零行为变更）。"""
    channel = create_channel("shop_x", 1, 7)
    assert isinstance(channel, PDDChannel)
    assert channel.shop_id == "shop_x"
    assert channel.user_id == 7


def test_create_channel_explicit_pdd_is_pdd():
    """显式 platform='pdd' 应返回 PDDChannel。"""
    channel = create_channel("shop_x", 1, 7, platform=PLATFORM_PDD)
    assert isinstance(channel, PDDChannel)


def test_create_channel_pdd_injects_event_notifier():
    """PDD 分支必须注入 event_notifier（TIK-005 硬约束，不可破坏）。"""
    channel = create_channel("shop_x", 1, 7, platform=PLATFORM_PDD)
    # 构造器已注入 alert notifier（闭包绑定 shop_pk 维度去重单例）。
    assert channel._event_notifier is not None
    # 直接验证 build_alert_notifier 产物可被触发且不抛异常（告警链路可用）。
    notifier = build_alert_notifier(get_alert_dedup(), 1, send_cb=None)
    notifier("connection_disconnected", "测试")
    assert True


def test_create_channel_tiktok_is_real(tiktok_enabled):
    """platform='tiktok' 应返回 TikTokChannel 真实现（TIK-014 联调落地）。"""

    async def _run():
        return create_channel("shop_tk", 2, 9, platform=PLATFORM_TIKTOK)

    channel = asyncio.run(_run())
    assert isinstance(channel, TikTokChannel)
    assert channel.shop_id == "shop_tk"
    assert channel.user_id == 9
    assert channel.shop_pk == 2


def test_create_channel_tiktok_rejected_when_disabled(tiktok_disabled):
    """灰度开关关闭时，TikTok 连接请求被拒绝（TIK-017 灰度总开关）。

    经 tiktok_disabled fixture 显式注入关闭态（不读根目录 .env），保证无论
    本地/验收环境是否开启 TIKTOK_SHOP_ENABLED，本用例行为封闭稳定。
    """

    async def _run():
        return create_channel("shop_tk", 2, 9, platform=PLATFORM_TIKTOK)

    with pytest.raises(RuntimeError, match="TIKTOK_SHOP_ENABLED"):
        asyncio.run(_run())


def test_create_channel_tiktok_honors_config_values(tiktok_enabled):
    """工厂按配置注入 poll_interval / debounce_seconds（TIK-017）。

    send_timeout 同样经配置传入 TikTokSender（构造参数，见工厂装配），此处验证
    channel 侧可直接断言的两项配置接线。
    """
    tiktok_enabled.tiktok_poll_interval_seconds = 7.5
    tiktok_enabled.tiktok_debounce_seconds = 60.0

    async def _run():
        return create_channel("shop_tk", 2, 9, platform=PLATFORM_TIKTOK)

    channel = asyncio.run(_run())
    assert channel.poll_interval == 7.5
    assert channel._debouncer.silence_seconds == 60.0


def test_create_channel_invalid_platform_falls_back_pdd():
    """非法 platform 回退 PDD 路径（向后兼容）。"""
    channel = create_channel("shop_x", 1, 7, platform="unknown")
    assert isinstance(channel, PDDChannel)


def test_tiktok_factory_wiring_no_start(tiktok_enabled):
    """TikTok 工厂装配产物满足关键契约：队列键一致、回调可调用、通知器/会话已注入。"""

    async def _run():
        return create_channel("shop_tk", 2, 9, platform=PLATFORM_TIKTOK)

    channel = asyncio.run(_run())
    # 队列键与 PDD 分支同款约定（"{user_id}:{shop_id}"）。
    from channel_pdd.message_queue import message_queue_manager

    expected_queue = message_queue_manager.get_or_create("9:shop_tk")
    assert channel.message_queue is expected_queue
    # 消息消费回调可调用（handler(raw, shop_id, user_id)）。
    assert callable(channel._message_handler)
    # 告警通知器已注入（非 None）。
    assert channel._event_notifier is not None
    # 共享浏览器会话已注入（避免自建第二份）。
    assert channel._browser_session is not None


def test_create_channel_tiktok_passes_proxy_to_browser(tiktok_enabled, monkeypatch):
    """工厂 tiktok 分支把 proxy_server 注入 BrowserSession（Phase 2 前置）。"""
    captured: dict = {}

    class _SpyBrowserSession:
        """假浏览器会话：仅捕获构造参数，不真实启动浏览器。"""

        def __init__(self, shop_pk: int, **kwargs) -> None:
            captured["shop_pk"] = shop_pk
            captured["proxy_server"] = kwargs.get("proxy_server")

    monkeypatch.setattr(connection_manager, "BrowserSession", _SpyBrowserSession)

    async def _run():
        return create_channel(
            "shop_tk", 2, 9, platform=PLATFORM_TIKTOK,
            proxy_server="http://127.0.0.1:7890",
        )

    channel = asyncio.run(_run())
    assert isinstance(channel, TikTokChannel)
    assert captured["shop_pk"] == 2
    assert captured["proxy_server"] == "http://127.0.0.1:7890"


def test_create_channel_tiktok_default_proxy_none(tiktok_enabled, monkeypatch):
    """未传 proxy_server 时，BrowserSession 以 None 构造（不走代理，默认关闭）。"""
    captured: dict = {}

    class _SpyBrowserSession:
        def __init__(self, shop_pk: int, **kwargs) -> None:
            captured["proxy_server"] = kwargs.get("proxy_server")

    monkeypatch.setattr(connection_manager, "BrowserSession", _SpyBrowserSession)

    async def _run():
        return create_channel("shop_tk", 2, 9, platform=PLATFORM_TIKTOK)

    channel = asyncio.run(_run())
    assert isinstance(channel, TikTokChannel)
    assert captured["proxy_server"] is None


def test_pdd_channel_start_stop_noop_safe(monkeypatch):
    """PDD 分支在注入消费器场景下，create→（不真连）→事件通知器可用。"""
    # 仅验证 PDD 分支构造产物满足 ChannelAdapter 协议关键属性，避免真实建连。
    channel = create_channel("shop_x", 1, 7, platform=PLATFORM_PDD)
    assert hasattr(channel, "shop_id")
    assert hasattr(channel, "user_id")
    assert callable(getattr(channel, "start"))
    assert callable(getattr(channel, "stop"))
    assert callable(getattr(channel, "get_connection_status"))


__all__ = [
    "test_create_channel_default_is_pdd",
    "test_create_channel_explicit_pdd_is_pdd",
    "test_create_channel_pdd_injects_event_notifier",
    "test_create_channel_tiktok_is_real",
    "test_create_channel_tiktok_rejected_when_disabled",
    "test_create_channel_tiktok_honors_config_values",
    "test_create_channel_invalid_platform_falls_back_pdd",
    "test_tiktok_factory_wiring_no_start",
    "test_create_channel_tiktok_passes_proxy_to_browser",
    "test_create_channel_tiktok_default_proxy_none",
    "test_pdd_channel_start_stop_noop_safe",
]
