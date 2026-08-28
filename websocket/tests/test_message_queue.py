# -*- coding: utf-8 -*-
"""
test_message_queue —— 消息队列兼容转发表测试（Phase 2 前置）
============================================================
消息队列实现已上移 ``common.utils.message_queue``；``channel_pdd.message_queue``
保留为**兼容转发表**（re-export）。本文件验证：转发表完整暴露既有符号、且与
common 实现为同一对象（re-export 身份一致），保证存量调用方（PDDChannel /
TikTokChannel / connection_manager）以原导入路径零改动可用。

完整 FIFO 行为测试已随实现迁至 ``common/tests/test_message_queue.py``。
"""
from __future__ import annotations

import asyncio

import channel_pdd.message_queue as shim
from common.utils import message_queue as impl

# 转发表应暴露的全部既有符号（与 common 实现 __all__ 对齐）。
_EXPECTED_SYMBOLS = (
    "QueuedMessage",
    "QueueStats",
    "FifoMessageQueue",
    "MessageQueueManager",
    "message_queue_manager",
    "DEFAULT_MAX_SIZE",
)


def test_shim_exposes_all_symbols():
    """转发表完整暴露既有符号（存量导入路径零改动）。"""
    for name in _EXPECTED_SYMBOLS:
        assert hasattr(shim, name), f"转发表缺失符号 {name}"


def test_shim_reexports_same_objects():
    """转发表各符号与 common 实现为同一对象（re-export 身份一致）。"""
    assert shim.FifoMessageQueue is impl.FifoMessageQueue
    assert shim.MessageQueueManager is impl.MessageQueueManager
    assert shim.message_queue_manager is impl.message_queue_manager
    assert shim.QueuedMessage is impl.QueuedMessage
    assert shim.QueueStats is impl.QueueStats
    assert shim.DEFAULT_MAX_SIZE == impl.DEFAULT_MAX_SIZE


def test_shim_manager_functional():
    """经转发表直接可用（FIFO 语义由 common 实现保证，此处冒烟）。"""

    async def _run():
        queue = shim.FifoMessageQueue("shim:1")
        await queue.put("a")
        return (await queue.get()).payload

    assert asyncio.run(_run()) == "a"
