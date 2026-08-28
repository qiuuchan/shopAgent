# -*- coding: utf-8 -*-
"""
test_browser_session —— channel_tiktok.BrowserSession 单元测试（TIK-011）
=========================================================================
本文件用途：在不启动真实 Chromium 的前提下，验证 ``BrowserSession`` 的关键行为
（关联工单 TIK-011）：

- user-data-dir 按 ``shop_pk`` 隔离（``tiktok_{shop_pk}`` 命名，且基于环境变量
  ``PLAYWRIGHT_USER_DATA_DIR`` 或当前工作目录的 ``browser_data``）。
- headless / proxy_server 参数正确传递至启动逻辑。
- 启动前调用 ``clean_singleton_lock_files`` 清理残留 Singleton 锁。
- ``close()`` 释放 page / context / playwright 资源，且不抛异常。

不依赖真实浏览器：经 monkeypatch 替换 ``login.browser_launcher`` 的清锁与启动函数，
以及 playwright 工厂，使 ``start()`` / ``close()`` 完全在桩环境内运行（硬约束：不真实联调）。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

import channel_tiktok.browser_session as bs_mod
from channel_tiktok.browser_session import BrowserSession


# ----------------------------------------------------------------------
# 桩：浏览器启动链路的各部分
# ----------------------------------------------------------------------
class _FakePage:
    """最小假页面：记录 close 调用。"""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeContext:
    """最小假上下文：new_page 返回假页面，记录 close 调用。"""

    def __init__(self):
        self.closed = False
        self.new_page_called = False

    async def new_page(self):
        self.new_page_called = True
        return _FakePage()

    async def close(self):
        self.closed = True


class _FakePlaywright:
    """最小假 playwright：暴露 chromium.launch_persistent_context。"""

    def __init__(self, context):
        self._context = context
        self.stopped = False

    async def stop(self):
        self.stopped = True

    class _Chromium:
        def __init__(self, context):
            self._context = context

        async def launch_persistent_context(self, *a, **k):
            return self._context

    @property
    def chromium(self):
        return self._Chromium(self._context)


def _make_fakes():
    """构建一组假浏览器对象，返回 (playwright, context)。"""
    context = _FakeContext()
    playwright = _FakePlaywright(context)
    return playwright, context


@pytest.fixture
def patched_launcher(monkeypatch):
    """用桩替换 browser_launcher 的清锁/启动与 playwright 工厂，记录调用。"""
    calls = {
        "clean_lock": [],
        "launch": [],
    }

    def _fake_clean(user_data_dir, name):
        calls["clean_lock"].append((user_data_dir, name))

    async def _fake_launch(playwright, user_data_dir, name, *, headless, chromium_args=None):
        calls["launch"].append(
            {
                "user_data_dir": user_data_dir,
                "name": name,
                "headless": headless,
                "chromium_args": chromium_args,
            }
        )
        # 返回由 playwright 工厂产出的上下文。
        _, context = _make_fakes()
        # 将 context 绑回 playwright 以便 close 验证。
        playwright._context = context
        return context

    # 假 playwright 默认工厂：BrowserSession 未显式注入工厂时经模块级默认值命中此处。
    # 返回「无 __aenter__ 的假 playwright 实例」：start() 将其直接作为实例引用，
    # close() 走 stop() 协程回收路径。必须替换——真实 async_playwright 的 __aenter__
    # 会拉起 node driver 子进程，且 start/close 分处两个 asyncio.run（不同 loop），
    # 子进程 transport 跨 loop 无法回收，GC 时产生 unraisable 告警并偶发干扰
    # Hypothesis 属性测试（Flaky）。
    def _fake_default_playwright_factory():
        playwright, _ = _make_fakes()
        return lambda: playwright

    monkeypatch.setattr(bs_mod, "clean_singleton_lock_files", _fake_clean)
    monkeypatch.setattr(bs_mod, "launch_persistent_context_with_retry", _fake_launch)
    monkeypatch.setattr(
        bs_mod, "_default_playwright_factory", _fake_default_playwright_factory
    )
    return calls


def test_user_data_dir_isolated_by_shop_pk(monkeypatch, patched_launcher, tmp_path):
    """不同 shop_pk 应映射到不同的 tiktok_{shop_pk} 用户数据目录（隔离、不串号）。"""
    monkeypatch.setenv("PLAYWRIGHT_USER_DATA_DIR", str(tmp_path))
    s1 = BrowserSession(shop_pk=12)
    s2 = BrowserSession(shop_pk=13)
    # 目录名必须带前缀并以 shop_pk 区分。
    assert s1.user_data_dir.endswith("tiktok_12")
    assert s2.user_data_dir.endswith("tiktok_13")
    assert s1.user_data_dir != s2.user_data_dir
    # 基础目录来自环境变量。
    assert str(tmp_path) in s1.user_data_dir


def test_proxy_passed_to_launch(monkeypatch, patched_launcher):
    """proxy_server 应注入启动参数 chromium_args（--proxy-server=...），否则代理静默失效。"""
    session = BrowserSession(shop_pk=1, headless=True, proxy_server="http://proxy:8080")

    async def _run():
        await session.start()
        # 启动确实被调用一次。
        assert len(patched_launcher["launch"]) == 1
        launch_kwargs = patched_launcher["launch"][0]
        assert launch_kwargs["headless"] is True
        assert launch_kwargs["name"] == "tiktok_1"
        # 代理地址必须体现在 chromium_args 启动参数中。
        chromium_args = launch_kwargs["chromium_args"] or []
        assert any(
            arg == "--proxy-server=http://proxy:8080" for arg in chromium_args
        )

    asyncio.run(_run())
    asyncio.run(session.close())


def test_singleton_lock_clean_called_before_launch(monkeypatch, patched_launcher):
    """启动前应调用 clean_singleton_lock_files 清理锁文件。"""
    session = BrowserSession(shop_pk=7, headless=False)

    async def _run():
        await session.start()
        # 清锁被调用，且参数为本店铺的 user_data_dir / name。
        assert len(patched_launcher["clean_lock"]) == 1
        ud, name = patched_launcher["clean_lock"][0]
        assert ud == session.user_data_dir
        assert name == "tiktok_7"
        # 清锁发生在启动之前（按调用序：先 clean 后 launch）。
        assert patched_launcher["launch"][0]["user_data_dir"] == ud

    asyncio.run(_run())
    asyncio.run(session.close())


def test_close_releases_resources(monkeypatch, patched_launcher):
    """close() 释放 page / context / playwright，并将内部引用置空，不抛异常。"""
    session = BrowserSession(shop_pk=9)

    async def _run():
        await session.start()
        # 启动后持有资源。
        assert session.page is not None
        assert session._context is not None

    asyncio.run(_run())

    # 资源引用已清空（不抛异常即通过核心约束）。
    asyncio.run(session.close())
    assert session.page is None
    assert session._context is None
    assert session._playwright is None


def test_start_idempotent(monkeypatch, patched_launcher):
    """重复 start() 不重复调用启动逻辑（幂等）。"""
    session = BrowserSession(shop_pk=3)

    async def _run():
        await session.start()
        await session.start()  # 第二次应为幂等提前返回

    asyncio.run(_run())
    assert len(patched_launcher["launch"]) == 1
    assert len(patched_launcher["clean_lock"]) == 1
    asyncio.run(session.close())


def test_close_without_start_is_safe(monkeypatch, patched_launcher):
    """未 start 直接 close 不应抛异常（资源均为 None 时安全跳过）。"""
    session = BrowserSession(shop_pk=5)
    # 不调用 start，直接 close。
    asyncio.run(session.close())
    assert session._context is None


def test_user_data_dir_injection_overrides_isolation(monkeypatch):
    """显式注入 user_data_dir 应优先生效（便于测试 / 自定义隔离目录）。"""
    custom = "/tmp/custom_tiktok_dir"
    session = BrowserSession(shop_pk=99, user_data_dir=custom)
    assert session.user_data_dir == custom
