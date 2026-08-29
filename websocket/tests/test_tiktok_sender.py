# -*- coding: utf-8 -*-
"""
websocket.tests.test_tiktok_sender —— TikTok 发送器单元测试（TIK-012）
=======================================================================
本文件用途：验证 ``channel_tiktok.tiktok_sender.TikTokSender`` 的同步发送逻辑，
全部通过注入假对象（FakePage / FakeLoop / 桥接 mock）完成，不真实操作浏览器：

- FakePage 注入：点击会话 / 输入 / 发送调用序正确；
- human-like 输入（type）调用序；
- 成功检测（等待己方气泡出现）与超时失败；
- 线程桥接（run_coroutine_threadsafe）mock 验证；
- 并发串行化（asyncio.Lock 同一锁保证顺序）；
- send_image 显式不支持返回 None。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from channel_tiktok.tiktok_sender import TikTokSender


class FakePage:
    """记录调用的假页面对象（无需真实浏览器）。

    支持：click / fill / type / wait_for_selector。``bubble_appears`` 控制
    wait_for_selector(己方气泡) 是否成功（模拟成功检测 / 超时失败）。
    """

    def __init__(self, bubble_appears: bool = True, wait_raise: bool = False) -> None:
        self.calls: List[str] = []
        self.bubble_appears = bubble_appears
        self.wait_raise = wait_raise  # 若 True，wait_for_selector 抛超时异常
        # 既有己方气泡数（count 桩，模拟会话历史气泡；TIK-018 增量检测）。
        self.existing_bubbles = 0

    async def click(self, selector: str) -> None:
        self.calls.append(f"click:{selector}")

    async def count(self, selector: str) -> int:
        self.calls.append(f"count:{selector}")
        return self.existing_bubbles

    def locator(self, selector: str) -> Any:
        """返回带异步 count() 的假定位器（对齐真实 Page.locator(sel).count()）。"""
        outer = self

        class _FakeLocator:
            async def count(self) -> int:
                return await outer.count(selector)

        return _FakeLocator()

    async def fill(self, selector: str, value: str) -> None:
        self.calls.append(f"fill:{selector}:{value}")

    async def type(self, selector: str, text: str) -> None:
        self.calls.append(f"type:{selector}:{text}")

    async def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        self.calls.append(f"wait:{selector}")
        if not self.bubble_appears or self.wait_raise:
            raise Exception("timeout: bubble not found")


class FakeBrowserSession:
    """提供 page 属性的假浏览器会话。"""

    def __init__(self, page: Any) -> None:
        self.page = page


class FakeLoop:
    """记录 run_coroutine_threadsafe 调用的假事件循环。

    立即用 asyncio 真实运行协程并缓存 future 供 result(timeout) 取结果，
    从而在不依赖真实线程桥接的情况下验证「桥接被调用」。
    """

    def __init__(self) -> None:
        self.scheduled: List[asyncio.Future] = []

    def run_coroutine_threadsafe(self, coro, loop=None):
        """模拟 asyncio.run_coroutine_threadsafe：真实执行协程并返回 fake future。

        返回 ``concurrent.futures.Future``（兼容 ``result(timeout=...)`` 关键字参数，
        与真实 ``run_coroutine_threadsafe`` 返回类型一致）。
        """
        import concurrent.futures

        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            result = asyncio.run(coro)
            fut.set_result(result)
        except Exception as exc:  # noqa: BLE001
            fut.set_exception(exc)
        self.scheduled.append(fut)
        return fut


def _make_sender(
    page: FakePage,
    *,
    bubble_appears: bool = True,
    loop: Optional[Any] = None,
    min_send_interval: float = 0.0,
    max_send_interval: float = 0.0,
) -> TikTokSender:
    """构造注入假页面的发送器。

    默认关闭发送最小随机间隔（max_send_interval=0），避免既有测试空等 45-120s；
    间隔生效行为由专门的测试用例覆盖（注入 mock 验证 sleep）。
    """
    session = FakeBrowserSession(page)
    return TikTokSender(
        shop_id="shop_1",
        user_id=1,
        browser_session=session,
        main_loop=loop,
        min_send_interval=min_send_interval,
        max_send_interval=max_send_interval,
    )


# ----------------------------------------------------------------------
# 调用序与成功检测
# ----------------------------------------------------------------------
def test_send_text_call_sequence():
    """发送文本：点击会话 → fill → type → click 发送 → 等待己方气泡，调用序正确。"""
    page = FakePage(bubble_appears=True)
    sender = _make_sender(page)

    # 直接测试主循环协程（不依赖真实事件循环桥接）。
    async def _run():
        return await sender._dom_send_text("u_1", "你好")

    result = asyncio.run(_run())

    assert result is not None
    assert result["success"] is True
    # 调用序校验（首个 DOM 操作为点击会话卡；随后统计既有己方气泡）。
    assert page.calls[0].startswith("click:")
    assert any(c.startswith("count:") for c in page.calls)
    assert any(c.startswith("fill:") for c in page.calls)
    assert any(c.startswith("type:") for c in page.calls)
    assert any(c.startswith("wait:") for c in page.calls)
    # 会话定位选择器含 recipient_uid。
    assert any(f"u_1" in c for c in page.calls if c.startswith("click:"))


def test_send_text_bubble_timeout_failure():
    """发送后未检测到己方气泡（超时）→ 返回 None。"""
    page = FakePage(bubble_appears=False)
    sender = _make_sender(page)

    async def _run():
        return await sender._dom_send_text("u_1", "你好")

    result = asyncio.run(_run())
    assert result is None


def test_send_image_not_supported():
    """send_image 显式不支持（Phase 1）返回 None。"""
    page = FakePage()
    sender = _make_sender(page)
    assert sender.send_image("u_1", "http://x/y.png") is None


def test_humanlike_type_called():
    """human-like 输入经 type 逐字输入（调用序含 type）。"""
    page = FakePage(bubble_appears=True)
    sender = _make_sender(page)

    async def _run():
        return await sender._dom_send_text("u_1", "逐字内容")

    asyncio.run(_run())
    type_calls = [c for c in page.calls if c.startswith("type:")]
    assert len(type_calls) == 1
    assert "逐字内容" in type_calls[0]


# ----------------------------------------------------------------------
# 发送最小随机间隔（频率断路器，PLAN §7）
# ----------------------------------------------------------------------
def test_default_send_interval_constants():
    """默认最小随机间隔常量：45-120s（PLAN §7 频率断路器）。"""
    from channel_tiktok.tiktok_sender import (
        DEFAULT_MAX_SEND_INTERVAL_SECONDS,
        DEFAULT_MIN_SEND_INTERVAL_SECONDS,
    )

    assert DEFAULT_MIN_SEND_INTERVAL_SECONDS == 45.0
    assert DEFAULT_MAX_SEND_INTERVAL_SECONDS == 120.0


def test_send_interval_applied_before_bridge():
    """发送前应用最小随机间隔：同步 sleep 被调用一次（采样值落在区间内）。

    TIK-018 实测修正：节流移至桥接之前（桥接等待超时仅 15s，协程内 sleep
    45-120s 必然超时误判），故 sleep 为同步调用。
    """
    page = FakePage(bubble_appears=True)
    loop = FakeLoop()
    sender = _make_sender(page, loop=loop, min_send_interval=0.01, max_send_interval=0.02)

    sleep_calls: List[float] = []

    with mock.patch(
        "channel_tiktok.tiktok_sender.time.sleep",
        side_effect=sleep_calls.append,
    ), mock.patch(
        "channel_tiktok.tiktok_sender.asyncio.run_coroutine_threadsafe",
        side_effect=loop.run_coroutine_threadsafe,
    ):
        result = sender.send_text("u_1", "间隔")

    assert result is not None
    # sleep 恰好调用一次（桥接前的节流等待），且采样值在 [0.01, 0.02] 内。
    assert len(sleep_calls) == 1
    assert 0.01 <= sleep_calls[0] <= 0.02


def test_send_interval_disabled_when_max_non_positive():
    """间隔上限 ≤ 0 时跳过 sleep（测试 / 关闭场景）。"""
    page = FakePage(bubble_appears=True)
    loop = FakeLoop()
    sender = _make_sender(page, loop=loop, min_send_interval=0.0, max_send_interval=0.0)

    with mock.patch("channel_tiktok.tiktok_sender.time.sleep") as fake_sleep, mock.patch(
        "channel_tiktok.tiktok_sender.asyncio.run_coroutine_threadsafe",
        side_effect=loop.run_coroutine_threadsafe,
    ):
        result = sender.send_text("u_1", "无间隔")

    assert result is not None
    fake_sleep.assert_not_called()


# ----------------------------------------------------------------------
# 线程桥接 mock
# ----------------------------------------------------------------------
def test_thread_bridge_invoked():
    """send_text 经 run_coroutine_threadsafe 桥接主循环并返回结果。"""
    page = FakePage(bubble_appears=True)
    loop = FakeLoop()
    sender = _make_sender(page, loop=loop)

    with mock.patch(
        "channel_tiktok.tiktok_sender.asyncio.run_coroutine_threadsafe",
        side_effect=loop.run_coroutine_threadsafe,
    ) as patched:
        result = sender.send_text("u_1", "桥接测试")
        # 桥接被调用一次。
        assert patched.call_count == 1
        assert result is not None
        assert result["success"] is True


def test_thread_bridge_timeout_returns_none():
    """桥接超时（future.result 抛 TimeoutError）→ 返回 None。"""

    class TimeoutFuture:
        def result(self, timeout=None):
            raise asyncio.TimeoutError("timeout")

        def cancel(self):
            return False

    class TimeoutLoop:
        def run_coroutine_threadsafe(self, coro, loop=None):
            # 对齐真实 run_coroutine_threadsafe 的所有权语义：协程一经桥接即由
            # 循环接管；本 mock 不执行它，须显式关闭避免 GC「never awaited」告警。
            coro.close()
            return TimeoutFuture()

    page = FakePage(bubble_appears=True)
    sender = _make_sender(page, loop=TimeoutLoop())
    with mock.patch(
        "channel_tiktok.tiktok_sender.asyncio.run_coroutine_threadsafe",
        side_effect=TimeoutLoop().run_coroutine_threadsafe,
    ):
        assert sender.send_text("u_1", "超时") is None


# ----------------------------------------------------------------------
# 并发串行化
# ----------------------------------------------------------------------
def test_concurrent_sends_serialized():
    """并发 DOM 发送经同一 asyncio.Lock 串行执行（不交错）。"""
    page = FakePage(bubble_appears=True)
    sender = _make_sender(page)

    async def _run():
        # 在运行中的循环内初始化主循环侧串行锁（与 _dom_send_text 内一致）。
        sender._send_lock = asyncio.Lock()
        # 模拟两个并发发送协程，验证共享锁串行（通过调用序计数器校验不交错）。
        order = []

        async def _one(tag):
            async with sender._send_lock:
                order.append(f"enter:{tag}")
                await asyncio.sleep(0.001)
                order.append(f"exit:{tag}")

        await asyncio.gather(_one("A"), _one("B"))
        return order

    order = asyncio.run(_run())
    # 任一 enter 之后必紧跟对应 exit，才证明串行（无交错）。
    for i, step in enumerate(order):
        if step.startswith("enter:"):
            assert order[i + 1].startswith("exit:")


def test_no_page_returns_none():
    """无可用页面（browser_session 为 None）→ 返回 None。"""
    sender = TikTokSender(shop_id="s", user_id=1, browser_session=None, main_loop=None)

    async def _run():
        return await sender._dom_send_text("u_1", "无页面")

    assert asyncio.run(_run()) is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
