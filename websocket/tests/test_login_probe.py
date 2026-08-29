# -*- coding: utf-8 -*-
"""
websocket.tests.test_login_probe —— TikTok 登录态周期巡检单元测试（TIK-023）
============================================================================
本文件用途：验证 ``channel_tiktok.login_probe`` 的只读巡检逻辑（TIK-023 登录态
过期周期观测的取数侧），全部通过注入桩回调完成，不真实操作浏览器：

- 判定优先级：无页面 → 页面死亡 → 主站过期 → IM 过期 → 正常；
- 探测异常记为 unknown（不误报为过期）；
- 中文说明映射（含未知取值原样返回）；
- ``TikTokChannel.probe_login_state`` 正确委派给纯逻辑函数；
- cookies 路由 TikTok 分支回传 login_probe 且恒返回 success。
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from channel_tiktok import login_probe
from channel_tiktok.login_probe import (
    LOGIN_PROBE_EXPIRED,
    LOGIN_PROBE_IM_EXPIRED,
    LOGIN_PROBE_NO_CHANNEL,
    LOGIN_PROBE_OK,
    LOGIN_PROBE_PAGE_DEAD,
    LOGIN_PROBE_UNKNOWN,
    describe,
    probe_login_state,
)


def _stubs(
    *,
    has_page=True,
    page_alive=True,
    login_expired=False,
    im_expired=False,
    raise_on_im=False,
):
    """构造四类回调桩（默认「页面正常且登录态有效」）。"""

    async def _page_alive():
        return page_alive

    async def _im_expired():
        if raise_on_im:
            raise RuntimeError("evaluate 失败")
        return im_expired

    return {
        "has_page": lambda: has_page,
        "page_alive": _page_alive,
        "is_login_expired": lambda: login_expired,
        "is_im_expired": _im_expired,
    }


def _run(**kwargs) -> str:
    """用桩回调跑一次巡检并返回结果取值。"""
    return asyncio.run(probe_login_state(**_stubs(**kwargs)))


# ----------------------------------------------------------------------
# 判定优先级
# ----------------------------------------------------------------------
def test_no_page_returns_channel_absent():
    """无活跃页面（未连接 / 营业时间窗外）记为 channel_absent，属正常期望态。"""
    assert _run(has_page=False) == LOGIN_PROBE_NO_CHANNEL


def test_no_page_short_circuits_before_alive_probe():
    """无页面时不再做存活探测（避免对空页面发起 evaluate）。"""
    calls = []

    async def _page_alive():
        calls.append(1)
        return True

    stubs = _stubs()
    stubs["has_page"] = lambda: False
    stubs["page_alive"] = _page_alive
    assert asyncio.run(probe_login_state(**stubs)) == LOGIN_PROBE_NO_CHANNEL
    assert calls == []


def test_page_dead_returns_page_dead():
    """页面存活探测失败（浏览器已退出）优先于登录态判定。"""
    assert _run(page_alive=False, login_expired=True) == LOGIN_PROBE_PAGE_DEAD


def test_login_page_redirect_returns_expired():
    """主站跳登录页记为 login_expired，且先于 IM 弹窗判定。"""
    assert _run(login_expired=True, im_expired=True) == LOGIN_PROBE_EXPIRED


def test_im_modal_returns_im_expired():
    """主站未跳转但出现 IM 会话过期弹窗时记为 im_expired。"""
    assert _run(im_expired=True) == LOGIN_PROBE_IM_EXPIRED


def test_all_healthy_returns_ok():
    """页面存活且两类过期判定均未命中 → ok。"""
    assert _run() == LOGIN_PROBE_OK


# ----------------------------------------------------------------------
# 异常兜底
# ----------------------------------------------------------------------
def test_probe_exception_returns_unknown():
    """探测过程异常记为 unknown，不误报为过期（巡检只观测，判定权留人工）。"""
    assert _run(raise_on_im=True) == LOGIN_PROBE_UNKNOWN


def test_page_alive_exception_returns_unknown():
    """存活探测本身抛异常同样记为 unknown。"""

    async def _boom():
        raise RuntimeError("浏览器已死")

    stubs = _stubs()
    stubs["page_alive"] = _boom
    assert asyncio.run(probe_login_state(**stubs)) == LOGIN_PROBE_UNKNOWN


# ----------------------------------------------------------------------
# 中文说明
# ----------------------------------------------------------------------
def test_describe_maps_all_registered_values():
    """各取值均有中文说明，且说明非空。"""
    for value in (
        LOGIN_PROBE_OK,
        LOGIN_PROBE_EXPIRED,
        LOGIN_PROBE_IM_EXPIRED,
        LOGIN_PROBE_PAGE_DEAD,
        LOGIN_PROBE_NO_CHANNEL,
        LOGIN_PROBE_UNKNOWN,
    ):
        assert describe(value) and describe(value) != value


def test_describe_unknown_value_passthrough():
    """未登记取值原样返回，便于排障时直接看到原始值。"""
    assert describe("some_future_value") == "some_future_value"


# ----------------------------------------------------------------------
# TikTokChannel 委派
# ----------------------------------------------------------------------
def test_channel_delegates_to_pure_function():
    """TikTokChannel.probe_login_state 把实例方法作为回调注入纯逻辑函数。"""
    from channel_tiktok.tiktok_channel import TikTokChannel

    channel = object.__new__(TikTokChannel)
    with mock.patch.object(
        login_probe, "probe_login_state", return_value=LOGIN_PROBE_OK
    ) as mocked:
        result = asyncio.run(channel.probe_login_state())

    assert result == LOGIN_PROBE_OK
    kwargs = mocked.call_args.kwargs
    # 四类回调均由通道实例方法提供（页面存在性为惰性 lambda）
    assert callable(kwargs["has_page"])
    assert kwargs["page_alive"] == channel._check_page_alive
    assert kwargs["is_login_expired"] == channel._is_login_expired
    assert kwargs["is_im_expired"] == channel._is_im_login_expired


def test_channel_has_page_false_without_browser_session():
    """无浏览器会话时 has_page 为 False（无活跃页面），不触碰 page 属性。"""
    from channel_tiktok.tiktok_channel import TikTokChannel

    channel = object.__new__(TikTokChannel)
    channel._browser_session = None
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return LOGIN_PROBE_NO_CHANNEL

    with mock.patch.object(login_probe, "probe_login_state", side_effect=_capture):
        asyncio.run(channel.probe_login_state())

    assert captured["has_page"]() is False


# ----------------------------------------------------------------------
# cookies 路由：TikTok 巡检打点
# ----------------------------------------------------------------------
def test_cookies_route_tiktok_returns_probe_and_skipped():
    """TikTok 分支回传 login_probe 且 skipped=True，恒 success 不改变任务成败。"""
    from routes.cookies import refresh_cookie, RefreshCookieRequest

    async def _fake_probe():
        return LOGIN_PROBE_IM_EXPIRED

    fake_channel = mock.Mock()
    fake_channel.probe_login_state = _fake_probe
    with mock.patch(
        "routes.cookies.connection_registry.get", return_value=fake_channel
    ) as get_mock:
        response = asyncio.run(
            refresh_cookie(
                RefreshCookieRequest(
                    shop_pk=1, shop_id="S1", owner_user_id=7, platform="tiktok"
                )
            )
        )

    assert response.success is True
    assert response.data["skipped"] is True
    assert response.data["login_probe"] == LOGIN_PROBE_IM_EXPIRED
    assert "IM 会话过期弹窗" in response.message
    # 按 (shop_id, owner_user_id) 定位活跃通道
    get_mock.assert_called_once_with("S1", 7)


def test_cookies_route_tiktok_no_channel_is_absent_not_failure():
    """无活跃通道（未连接 / 窗口外）记为 channel_absent，仍返回 success。"""
    from routes.cookies import refresh_cookie, RefreshCookieRequest

    with mock.patch("routes.cookies.connection_registry.get", return_value=None):
        response = asyncio.run(
            refresh_cookie(
                RefreshCookieRequest(
                    shop_pk=1, shop_id="S1", owner_user_id=7, platform="tiktok"
                )
            )
        )

    assert response.success is True
    assert response.data["login_probe"] == LOGIN_PROBE_NO_CHANNEL


def test_cookies_route_tiktok_probe_exception_still_success():
    """巡检抛异常不影响本路由：记 unknown 且仍返回 success。"""
    from routes.cookies import refresh_cookie, RefreshCookieRequest

    async def _boom():
        raise RuntimeError("page evaluate 失败")

    fake_channel = mock.Mock()
    fake_channel.probe_login_state = _boom
    with mock.patch("routes.cookies.connection_registry.get", return_value=fake_channel):
        response = asyncio.run(
            refresh_cookie(
                RefreshCookieRequest(
                    shop_pk=1, shop_id="S1", owner_user_id=7, platform="tiktok"
                )
            )
        )

    assert response.success is True
    assert response.data["login_probe"] == LOGIN_PROBE_UNKNOWN


def test_cookies_route_pdd_path_untouched():
    """PDD 路径零行为变更：缺少 owner_user_id 仍按原逻辑返回失败。"""
    from routes.cookies import refresh_cookie, RefreshCookieRequest

    response = asyncio.run(
        refresh_cookie(
            RefreshCookieRequest(
                shop_pk=1, shop_id="P1", owner_user_id=None, platform="pdd"
            )
        )
    )

    assert response.success is False
    assert "login_probe" not in (response.data or {})
