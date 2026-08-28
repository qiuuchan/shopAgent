# -*- coding: utf-8 -*-
"""
websocket.tests.test_tiktok_login —— channel_tiktok.tiktok_login 单元测试（TIK-018 前置）
=====================================================================================
本文件用途：在不启动真实 Chromium 的前提下，验证 ``login_tiktok`` 的真实登录编排
（2026-08-28 由占位实现回填，选择器来自 spike s6 实测）：

- 已登录复用：打开登录页后 URL 跳离（登录态有效）→ 免填表直接导出成功；
- 打开登录页 → 等待手机号输入框渲染 → 填手机号 / 密码 → 点登录按钮（调用序）；
- 等待登录成功（URL 跳离登录页）→ 导出 Cookie + 返回登录态目录与店铺信息；
- 页面出现可见错误提示（账号 / 密码错误）→ 提前判定失败返回 None；
- 等待超时仍未跳离登录页 → 返回 None；
- 释放浏览器会话（finally close）。

不依赖真实浏览器：经 monkeypatch 替换 ``tiktok_login.BrowserSession``（假会话）与
登录等待超时读取函数（硬约束：不真实联调），使登录流程完全在桩环境内执行。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import pytest

import channel_tiktok.tiktok_login as login_mod
from channel_tiktok.tiktok_login import login_tiktok
from channel_tiktok.selectors import (
    SELECTOR_LOGIN_MOBILE_INPUT,
    SELECTOR_LOGIN_PASSWORD_INPUT,
    SELECTOR_LOGIN_SUBMIT_BUTTON,
)

# 测试期禁用日志输出（避免用例失败时刷屏）。
logging.getLogger("channel_tiktok.tiktok_login").setLevel(logging.CRITICAL)

# 模拟的登录账号 / 密码（测试店手机号，与生产无关联）。
_NAME = "18023103936"
_PASSWORD = "zxx1128."

# 模拟的登录态用户数据目录（登录成功后应原样返回，供 backend 落库复用）。
_FAKE_DATA_DIR = "D:/browser_data/tiktok_123456"


class _FakeErrorHandle:
    """假错误提示元素：is_visible 恒 True。"""

    async def is_visible(self) -> bool:
        return True


class _FakePage:
    """最小假页面：记录调用序，按配置模拟 URL 跳转 / 错误提示。

    Args:
        already_logged_in: 初始即已登录（URL 为 homepage，模拟登录态有效复用）。
        login_success_after_submit_waits: 点击登录按钮后第 N 次 ``wait_for_timeout``
            将 URL 切换为已登录 homepage（模拟登录成功跳转）；None 表示永不跳转
            （用于超时用例）。注意：提交前的「打开页面等待 3s」不计入该计数。
        error_visible: 是否始终返回可见错误提示元素（模拟账号密码错误）。
    """

    def __init__(
        self,
        *,
        already_logged_in: bool = False,
        login_success_after_submit_waits: Optional[int] = None,
        error_visible: bool = False,
    ) -> None:
        self.calls: list[tuple] = []
        self.url: str = (
            "https://seller.tiktokshopglobalselling.com/homepage?lng=zh-CN"
            if already_logged_in
            else "https://seller.tiktokshopglobalselling.com/account/login?lng=zh-CN"
        )
        self.login_success_after_submit_waits = login_success_after_submit_waits
        self.submit_waits = 0
        self._submitted = False
        self.error_visible = error_visible

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.calls.append(("goto", url))

    async def wait_for_selector(self, selector: str, **kwargs: Any) -> None:
        self.calls.append(("wait_for_selector", selector))

    async def fill(self, selector: str, value: str) -> None:
        self.calls.append(("fill", selector, value))

    async def click(self, selector: str) -> None:
        self.calls.append(("click", selector))
        self._submitted = True

    async def wait_for_timeout(self, ms: int) -> None:
        if self._submitted:
            self.submit_waits += 1
            n = self.login_success_after_submit_waits
            if n is not None and self.submit_waits >= n:
                self.url = "https://seller.tiktokshopglobalselling.com/homepage?lng=zh-CN"

    async def evaluate(self, expression: str) -> str:
        return self.url

    async def query_selector(self, selector: str) -> Optional[_FakeErrorHandle]:
        return _FakeErrorHandle() if self.error_visible else None

    async def is_visible(self, selector: str) -> bool:
        # 模拟验证码输入框：仅提交后可见（避免干扰已登录复用分支）。
        return bool(self._submitted) and selector.endswith("Login_Code_Input")

    async def title(self) -> str:
        return "FunToy Lab 店铺首页"


class _FakeSession:
    """假浏览器会话：记录释放，导出固定 Cookie，暴露登录态目录。"""

    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.user_data_dir: str = _FAKE_DATA_DIR
        self.closed: bool = False

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def export_cookies_json(self) -> str:
        return '{"sid_guard":"abc"}'


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch):
    """注入假会话工厂与短超时，返回可配置的假页面引用（默认登录成功）。"""
    pages: dict[str, _FakePage] = {}

    def _fake_session_factory(**kwargs: Any) -> _FakeSession:
        page = _FakePage(login_success_after_submit_waits=1)
        pages["page"] = page
        return _FakeSession(page)

    monkeypatch.setattr(login_mod, "BrowserSession", _fake_session_factory)
    monkeypatch.setattr(login_mod, "_login_wait_timeout_ms", lambda: 60_000)
    return pages


async def _run_login():
    """在当前事件循环中执行登录（登录函数为异步）。"""
    return await login_tiktok(_NAME, _PASSWORD)


def test_login_success_returns_info_and_closes(fake_env):
    """登录成功：返回店铺信息与登录态目录，且释放浏览器会话。"""
    result = asyncio.run(_run_login())
    assert result is not None
    assert result["shop_id"] == _NAME  # 当前以账号名占位（TIK-018 实测回填真实标识）
    assert result["shop_name"] == "FunToy Lab 店铺首页"
    assert result["cookies_json"] == '{"sid_guard":"abc"}'
    assert result["browser_data_dir"] == _FAKE_DATA_DIR
    assert result["name"] == _NAME
    # 会话已释放（finally 兜底）。
    assert fake_env["page"] is not None


def test_login_submits_form_with_credentials(fake_env):
    """登录流程按序执行：打开登录页 → 等输入框 → 填手机号/密码 → 点登录。"""
    asyncio.run(_run_login())
    page = fake_env["page"]
    calls = page.calls
    # 打开登录页（基址 + 登录路径）。
    assert calls[0][0] == "goto"
    assert "/account/login" in calls[0][1]
    # 等待手机号输入框渲染（SPA 动态渲染）。
    assert ("wait_for_selector", SELECTOR_LOGIN_MOBILE_INPUT) in calls
    # 填手机号 + 密码。
    assert ("fill", SELECTOR_LOGIN_MOBILE_INPUT, _NAME) in calls
    assert ("fill", SELECTOR_LOGIN_PASSWORD_INPUT, _PASSWORD) in calls
    # 点登录按钮（且点击发生在填写之后）。
    fill_index = calls.index(("fill", SELECTOR_LOGIN_PASSWORD_INPUT, _PASSWORD))
    click_index = calls.index(("click", SELECTOR_LOGIN_SUBMIT_BUTTON))
    assert click_index > fill_index


def test_login_reuses_existing_session(fake_env, monkeypatch):
    """已有有效登录态：打开登录页后 URL 跳离 → 免填表直接导出成功。"""
    def _fake_session_factory(**kwargs: Any) -> _FakeSession:
        page = _FakePage(already_logged_in=True)
        fake_env["page"] = page
        return _FakeSession(page)

    monkeypatch.setattr(login_mod, "BrowserSession", _fake_session_factory)
    result = asyncio.run(_run_login())
    assert result is not None
    assert result["browser_data_dir"] == _FAKE_DATA_DIR
    assert result["cookies_json"] == '{"sid_guard":"abc"}'
    # 未触发任何填表 / 点击动作。
    assert not any(c[0] in ("fill", "click") for c in fake_env["page"].calls)


def test_login_fails_early_on_visible_error(fake_env, monkeypatch):
    """页面出现可见错误提示（账号/密码错误）→ 提前判定失败，不干等超时。"""
    # 覆盖为「始终有错误提示」的页面。
    def _fake_session_factory(**kwargs: Any) -> _FakeSession:
        page = _FakePage(error_visible=True)
        fake_env["page"] = page
        return _FakeSession(page)

    monkeypatch.setattr(login_mod, "BrowserSession", _fake_session_factory)
    result = asyncio.run(_run_login())
    assert result is None


def test_login_timeout_returns_none(fake_env, monkeypatch):
    """等待超时仍未跳离登录页 → 返回 None。"""
    def _fake_session_factory(**kwargs: Any) -> _FakeSession:
        # 永不跳转 + 无错误提示 → 触发超时路径。
        page = _FakePage(login_success_after_submit_waits=None)
        fake_env["page"] = page
        return _FakeSession(page)

    monkeypatch.setattr(login_mod, "BrowserSession", _fake_session_factory)
    monkeypatch.setattr(login_mod, "_login_wait_timeout_ms", lambda: 50)
    result = asyncio.run(_run_login())
    assert result is None
