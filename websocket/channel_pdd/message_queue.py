# -*- coding: utf-8 -*-
"""
channel_pdd.message_queue —— 客户消息 FIFO 队列（兼容转发表）
============================================================
本文件为**兼容转发表**（Phase 2 前置）：客户消息队列实现已上移至
``common.utils.message_queue``（纯 asyncio、平台无关，消除跨包 import），
此处仅 re-export 全部符号，保持存量调用方（PDDChannel / TikTokChannel /
connection_manager / 测试）以 ``channel_pdd.message_queue`` 导入路径不变，
**零行为变更**。新增引用请直接导入 ``common.utils.message_queue``。
"""
from common.utils.message_queue import (  # noqa: F401 - 转发表仅 re-export
    DEFAULT_MAX_SIZE,
    FifoMessageQueue,
    MessageQueueManager,
    QueuedMessage,
    QueueStats,
    message_queue_manager,
)

__all__ = [
    "QueuedMessage",
    "QueueStats",
    "FifoMessageQueue",
    "MessageQueueManager",
    "message_queue_manager",
    "DEFAULT_MAX_SIZE",
]
