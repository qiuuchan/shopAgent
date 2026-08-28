# -*- coding: utf-8 -*-
"""
websocket.tests.test_login_recovery —— 登录失效恢复探测单元测试（Phase 2 保活）
===============================================================================
本文件用途：验证 ``channel_tiktok.login_recovery.wait_login_recovery`` 的恢复探测
闭环（PLAN §7 登录态保活策略），全部通过注入桩回调完成，不真实操作浏览器：

- 登录态恢复（人工重登）→ 重开聊天页并返回 True；
- 持续失效达上限 → 返回 False（退出监控）；
- 探测期间收到停止信号 → 立即返回 False；
- 重开聊天页异常 → 继续探测（不中断）。
"""
from __future__ import annotations

import asyncio
from typing import List
from unittest import mock

from channel_tiktok.login_recovery import wait_login_recovery


def _sleep_counter(waits: List[float]) -> "asyncio.coroutines":
    """构造记录睡眠时长的桩 sleep（立即返回，不真等）。"""

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    return _sleep


def _run(coro) -> bool:
    """在真实事件循环中运行协程并返回结果。"""
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 恢复 / 放弃 / 停止信号
# ----------------------------------------------------------------------
def test_recovery_after_expiry_returns_true():
    """失效后第二轮探测登录态恢复 → 重开聊天页并返回 True。"""
    expired_flags = [True, False]  # 首轮仍失效，第二轮恢复
    reopen_calls = []

    async def _reopen():
        reopen_calls.append(1)

    waits: List[float] = []

    async def _run_coro():
        return await wait_login_recovery(
            is_expired=lambda: expired_flags.pop(0),
            reopen_chat=_reopen,
            sleep=_sleep_counter(waits),
            is_stopped=lambda: False,
            interval=5.0,
            max_tries=10,
        )

    assert _run(_run_coro()) is True
    assert len(reopen_calls) == 1  # 恢复后重开聊天页一次
    # 探测两轮（首轮仍失效、次轮恢复），每轮先 sleep 再检查。
    assert waits == [5.0, 5.0]


def test_expiry_persists_returns_false():
    """持续失效达 max_tries 上限 → 返回 False（退出监控待人工重连）。"""
    waits: List[float] = []

    async def _run_coro():
        return await wait_login_recovery(
            is_expired=lambda: True,  # 恒失效
            reopen_chat=lambda: asyncio.sleep(0),  # 不应被调用
            sleep=_sleep_counter(waits),
            is_stopped=lambda: False,
            interval=3.0,
            max_tries=4,
        )

    assert _run(_run_coro()) is False
    assert len(waits) == 4  # 探测满 4 轮


def test_stop_signal_aborts_probe():
    """探测期间收到停止信号 → 立即返回 False（不阻塞 stop()）。"""
    waits: List[float] = []

    async def _run_coro():
        return await wait_login_recovery(
            is_expired=lambda: True,
            reopen_chat=lambda: asyncio.sleep(0),
            sleep=_sleep_counter(waits),
            is_stopped=lambda: True,  # 首轮即收到停止信号
            interval=3.0,
            max_tries=10,
        )

    assert _run(_run_coro()) is False
    assert len(waits) == 1  # 仅探测一轮即因停止信号退出


def test_reopen_chat_failure_continues_probe():
    """登录恢复但重开聊天页异常 → 继续探测（不中断，下一轮恢复成功）。"""
    expired_flags = [False, False]  # 首轮恢复但 reopen 失败，次轮成功
    reopen_calls = {"n": 0}

    async def _reopen():
        reopen_calls["n"] += 1
        if reopen_calls["n"] == 1:
            raise RuntimeError("DOM 失配")

    waits: List[float] = []

    async def _run_coro():
        return await wait_login_recovery(
            is_expired=lambda: expired_flags.pop(0),
            reopen_chat=_reopen,
            sleep=_sleep_counter(waits),
            is_stopped=lambda: False,
            interval=2.0,
            max_tries=10,
        )

    assert _run(_run_coro()) is True
    assert reopen_calls["n"] == 2  # 失败一次后重试成功
    assert len(waits) == 2  # 两轮探测各 sleep 一次


def test_max_tries_zero_returns_false():
    """max_tries <= 0（如测试关闭探测）→ 立即返回 False。"""
    waits: List[float] = []

    async def _run_coro():
        return await wait_login_recovery(
            is_expired=lambda: False,
            reopen_chat=lambda: asyncio.sleep(0),
            sleep=_sleep_counter(waits),
            is_stopped=lambda: False,
            interval=1.0,
            max_tries=0,
        )

    assert _run(_run_coro()) is False
    assert waits == []


__all__ = [
    "test_recovery_after_expiry_returns_true",
    "test_expiry_persists_returns_false",
    "test_stop_signal_aborts_probe",
    "test_reopen_chat_failure_continues_probe",
    "test_max_tries_zero_returns_false",
]
