# -*- coding: utf-8 -*-
"""
test_tiktok_connect —— TIK-014 TikTokChannel 装配 + routes/connections 联调
==========================================================================
本文件验证 TIK-014 交付点：
① 工厂 tiktok 分支返回 TikTokChannel 且 wiring 正确（queue 键一致、message_handler
   可调用、event_notifier 非 None、browser_session 被注入、consumer 的 parser 为
   TikTok 解析器、sender 为 TikTokSender）；
② monkeypatch BrowserSession.start/close 为异步桩后 start_channel(platform='tiktok')
   → start/stop/get_connection_status 正常、注册表登记正确；
③ connections 路由 platform 透传（mock start_channel 断言收到 platform='tiktok'；
   缺省为 'pdd'）。

不跑全量，仅本文件用例由 CI 选择性执行（pytest -k tiktok_connect）。
测试事件循环一律用 asyncio.run()（仓库约定，禁用 get_event_loop().run_until_complete）。

实现约束：日志禁 debug（38）、中文注释（37）、复用既有注入模式、不真实启动浏览器。
"""
from __future__ import annotations

import asyncio
import functools
import types
from typing import Any, Dict, Optional

import pytest

from channel_base import PLATFORM_PDD, PLATFORM_TIKTOK
from channel_pdd import connection_manager, connection_registry
from channel_pdd.connection_manager import (
    create_channel,
    start_channel,
    stop_channel,
)
from channel_tiktok.tiktok_channel import TikTokChannel
from channel_tiktok.tiktok_message import parse_tiktok_raw
from channel_tiktok.tiktok_sender import TikTokSender


# ----------------------------------------------------------------------
# 测试桩：假浏览器会话（仅供测试，不真实启动浏览器）
# ----------------------------------------------------------------------
class _FakePage:
    """最小假页面（提供 goto 与 url，满足 TikTokChannel.start 时序）。"""

    url: str = "https://seller-us.tiktok.com/chat"

    async def goto(self, *args: Any, **kwargs: Any) -> None:
        """模拟打开聊天页（无操作）。"""
        return None


class _FakeBrowserSession:
    """假浏览器会话：start/close 为异步桩，并注入假页面供通道时序使用。"""

    def __init__(self, shop_pk: int, **kwargs: Any) -> None:
        self.shop_pk = shop_pk
        self._page: Optional[_FakePage] = None

    @property
    def page(self) -> Optional[_FakePage]:
        """返回当前假页面（已开则复用）。"""
        return self._page

    async def start(self) -> None:
        """异步桩：注入假页面，不真实启动浏览器。"""
        self._page = _FakePage()

    async def close(self) -> None:
        """异步桩：释放假页面。"""
        self._page = None


def _install_fake_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """用假浏览器会话替换工厂内的 BrowserSession，避免真实启动浏览器。"""
    monkeypatch.setattr(
        connection_manager, "BrowserSession", _FakeBrowserSession
    )


# ----------------------------------------------------------------------
# ① 工厂 tiktok 分支装配正确性（含 consumer 的 parser/sender 验证）
# ----------------------------------------------------------------------
def test_factory_tiktok_returns_real_channel(
    monkeypatch: pytest.MonkeyPatch, tiktok_enabled
):
    """工厂 tiktok 分支返回 TikTokChannel，且关键 wiring 正确。"""
    # 捕获工厂内部构造的 MessageConsumer 关键字参数，验证解析器与发送器注入。
    captured: Dict[str, Any] = {}
    orig_consumer = connection_manager.MessageConsumer

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return orig_consumer(*args, **kwargs)

    monkeypatch.setattr(connection_manager, "MessageConsumer", _spy)

    async def _run() -> TikTokChannel:
        return create_channel("shop_tk", 2, 9, platform=PLATFORM_TIKTOK)

    channel = asyncio.run(_run())

    # 返回类型与基础属性。
    assert isinstance(channel, TikTokChannel)
    assert channel.shop_id == "shop_tk"
    assert channel.user_id == 9
    assert channel.shop_pk == 2

    # queue 键一致（"{user_id}:{shop_id}" 同 PDD 分支约定）。
    from channel_pdd.message_queue import message_queue_manager

    expected_queue = message_queue_manager.get_or_create("9:shop_tk")
    assert channel.message_queue is expected_queue

    # 消息消费回调可调用、告警通知器已注入。
    assert callable(channel._message_handler)
    assert channel._event_notifier is not None

    # 共享浏览器会话已被注入（避免自建第二份）。
    assert isinstance(channel._browser_session, _FakeBrowserSession) or (
        channel._browser_session is not None
    )

    # consumer 的 parser 为 TikTok 解析器（functools.partial 绑定 shop_id）。
    parser = captured.get("message_parser")
    assert parser is not None
    assert isinstance(parser, functools.partial)  # type: ignore[name-defined]
    assert parser.func is parse_tiktok_raw
    assert parser.keywords.get("shop_id") == "shop_tk"

    # consumer 的 sender 为 TikTokSender 实例。
    sender = captured.get("sender")
    assert isinstance(sender, TikTokSender)
    # 发送器共享同一浏览器会话，保证「发送与监控同页」。
    assert sender.browser_session is channel._browser_session


# ----------------------------------------------------------------------
# ② start_channel / stop / get_connection_status 端到端（假浏览器）
# ----------------------------------------------------------------------
def test_start_stop_tiktok_channel(monkeypatch: pytest.MonkeyPatch, tiktok_enabled):
    """monkeypatch 浏览器后，start_channel(tiktok) 启动/停止/状态查询/注册表均正常。"""
    _install_fake_browser(monkeypatch)

    async def _run() -> Dict[str, Any]:
        channel = await start_channel(
            "shop_tk", 2, 9, platform=PLATFORM_TIKTOK
        )
        status_after_start = channel.get_connection_status()
        # 注册表应已登记（按 shop_id + user_id 维度）。
        registered = connection_registry.get("shop_tk", 9)
        await stop_channel("shop_tk", 9)
        status_after_stop = channel.get_connection_status()
        # 停止后注册表注销。
        registered_after_stop = connection_registry.get("shop_tk", 9)
        return {
            "channel": channel,
            "status_after_start": status_after_start,
            "registered": registered,
            "status_after_stop": status_after_stop,
            "registered_after_stop": registered_after_stop,
        }

    result = asyncio.run(_run())

    assert isinstance(result["channel"], TikTokChannel)
    # 启动后状态非 None（connected / connecting / error，状态枚举值为小写）。
    assert result["status_after_start"] is not None
    assert result["status_after_start"]["state"] in ("connected", "connecting", "error")
    # 注册表登记正确。
    assert result["registered"] is result["channel"]
    # 停止后注册表注销。
    assert result["registered_after_stop"] is None


# ----------------------------------------------------------------------
# ③ connections 路由 platform 透传
# ----------------------------------------------------------------------
def test_connections_route_platform_passthrough(monkeypatch: pytest.MonkeyPatch):
    """connect_connection 路由应将 platform 透传给 start_channel（tiktok 与缺省 pdd）。"""
    captured: Dict[str, Any] = {}

    async def _fake_start_channel(
        shop_id: str,
        shop_pk: int,
        user_id: int,
        *,
        platform: str = PLATFORM_PDD,
        channel_name: str = "pinduoduo",
        enable_notify: bool = True,
        proxy_server: Optional[str] = None,
        browser_data_dir: Optional[str] = None,
    ) -> Any:
        captured["platform"] = platform
        captured["shop_id"] = shop_id
        captured["proxy_server"] = proxy_server
        captured["browser_data_dir"] = browser_data_dir

        # 注意：类体不可见外层函数局部变量，故用 SimpleNamespace 注入属性。
        return types.SimpleNamespace(shop_id=shop_id, user_id=user_id)

    monkeypatch.setattr(
        connection_manager, "start_channel", _fake_start_channel
    )

    # 隐式导入路由模块（其 connect_connection 调用 connection_manager.start_channel）。
    from routes import connections as connections_route

    # 登录态目录读取为数据库访问，测试环境注入桩（返回固定值并断言透传）。
    monkeypatch.setattr(
        connections_route, "_load_browser_data_dir", lambda shop_pk: "D:/browser_data/tiktok_2"
    )

    # 显式指定 platform='tiktok'（含店铺出口代理，Phase 2 前置透传）。
    async def _run_tiktok() -> None:
        resp = await connections_route.connect_connection(
            connections_route.ConnectRequest(
                shop_pk=2,
                shop_id="shop_tk",
                owner_user_id=9,
                platform=PLATFORM_TIKTOK,
                proxy_server="http://127.0.0.1:7890",
            )
        )
        assert resp.success is True

    asyncio.run(_run_tiktok())
    assert captured["platform"] == PLATFORM_TIKTOK
    assert captured["shop_id"] == "shop_tk"
    assert captured["proxy_server"] == "http://127.0.0.1:7890"
    # 登录态目录从库读取后透传给 start_channel（免二次登录）。
    assert captured["browser_data_dir"] == "D:/browser_data/tiktok_2"

    # 缺省 platform（不传）应为 'pdd'。
    captured.clear()

    async def _run_default() -> None:
        resp = await connections_route.connect_connection(
            connections_route.ConnectRequest(
                shop_pk=2, shop_id="shop_pdd", owner_user_id=9
            )
        )
        assert resp.success is True

    asyncio.run(_run_default())
    assert captured["platform"] == PLATFORM_PDD
    # 缺省请求体未带代理：透传 None（不走代理）。
    assert captured["proxy_server"] is None


__all__ = [
    "test_factory_tiktok_returns_real_channel",
    "test_start_stop_tiktok_channel",
    "test_connections_route_platform_passthrough",
]
