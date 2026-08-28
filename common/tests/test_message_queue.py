# -*- coding: utf-8 -*-
"""
common.tests.test_message_queue —— 客户消息 FIFO 队列单元测试（Phase 2 前置上移）
=================================================================================
本文件验证 ``common.utils.message_queue`` 的核心能力（需求 5.3，原
``websocket/tests/test_message_queue.py`` 随实现上移而来）：
- FIFO 顺序：入队顺序 == 消费顺序；
- 自增序号单调递增且与入队顺序一致；
- 容量上限与关闭后拒绝入队；
- get 超时返回 None；
- MessageQueueManager 多队列相互独立、互不串扰。

测试基于 asyncio，不依赖数据库或网络。
"""
from __future__ import annotations

import asyncio

import pytest

from common.utils.message_queue import (
    FifoMessageQueue,
    MessageQueueManager,
)


def test_fifo_order_preserved():
    """入队顺序应与消费顺序严格一致（FIFO）。"""

    async def _run():
        queue = FifoMessageQueue("shop_1")
        inputs = [f"消息_{i}" for i in range(20)]
        for item in inputs:
            await queue.put(item)

        outputs = []
        while not queue.is_empty():
            message = await queue.get()
            outputs.append(message.payload)
        return inputs, outputs

    inputs, outputs = asyncio.run(_run())
    assert inputs == outputs


def test_seq_is_monotonic_and_matches_enqueue_order():
    """每条消息的自增序号应从 0 起单调递增，并与入队顺序一致。"""

    async def _run():
        queue = FifoMessageQueue("shop_seq")
        for i in range(10):
            await queue.put(i)
        seqs = []
        while not queue.is_empty():
            message = await queue.get()
            seqs.append(message.seq)
        return seqs

    seqs = asyncio.run(_run())
    assert seqs == list(range(10))


def test_concurrent_producers_then_drain_keeps_fifo_within_arrival():
    """单消费者顺序消费应保证全局 FIFO（按实际入队到达顺序）。"""

    async def _run():
        queue = FifoMessageQueue("shop_concurrent")
        # 顺序 await 入队，确保到达顺序确定。
        for i in range(50):
            await queue.put(i)
        drained = await queue.drain()
        return [m.payload for m in drained]

    payloads = asyncio.run(_run())
    assert payloads == list(range(50))


def test_put_raises_when_full():
    """队列满时入队应抛出 RuntimeError。"""

    async def _run():
        queue = FifoMessageQueue("shop_full", max_size=2)
        await queue.put("a")
        await queue.put("b")
        with pytest.raises(RuntimeError):
            await queue.put("c")

    asyncio.run(_run())


def test_put_raises_after_close():
    """关闭后入队应抛出 RuntimeError，但残余消息仍可消费。"""

    async def _run():
        queue = FifoMessageQueue("shop_close")
        await queue.put("a")
        queue.close()
        with pytest.raises(RuntimeError):
            await queue.put("b")
        # 残余消息仍可取出。
        message = await queue.get()
        assert message.payload == "a"
        # 取空后再 get 返回 None。
        assert await queue.get() is None

    asyncio.run(_run())


def test_get_timeout_returns_none():
    """空队列在超时后 get 应返回 None。"""

    async def _run():
        queue = FifoMessageQueue("shop_timeout")
        return await queue.get(timeout=0.05)

    assert asyncio.run(_run()) is None


def test_invalid_max_size_raises():
    """非正的 max_size 应抛出 ValueError。"""
    with pytest.raises(ValueError):
        FifoMessageQueue("bad", max_size=0)


def test_stats_track_enqueue_dequeue():
    """统计信息应正确反映入队 / 出队与当前大小。"""

    async def _run():
        queue = FifoMessageQueue("shop_stats")
        for i in range(5):
            await queue.put(i)
        await queue.get()
        return queue.get_stats()

    stats = asyncio.run(_run())
    assert stats.total_enqueued == 5
    assert stats.total_dequeued == 1
    assert stats.current_size == 4


def test_manager_isolates_queues():
    """管理器中不同名称的队列应相互独立、互不串扰。"""

    async def _run():
        manager = MessageQueueManager()
        q1 = manager.get_or_create("shop_a")
        q2 = manager.get_or_create("shop_b")
        await q1.put("a1")
        await q2.put("b1")
        await q1.put("a2")
        # 取 shop_a 应按 FIFO 得到 a1, a2，与 shop_b 互不影响。
        m1 = await q1.get()
        m2 = await q1.get()
        return m1.payload, m2.payload, q2.size()

    first, second, b_size = asyncio.run(_run())
    assert (first, second) == ("a1", "a2")
    assert b_size == 1


def test_manager_get_or_create_returns_same_instance():
    """同名 get_or_create 应返回同一队列实例。"""
    manager = MessageQueueManager()
    q1 = manager.get_or_create("same")
    q2 = manager.get_or_create("same")
    assert q1 is q2
