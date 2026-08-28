# -*- coding: utf-8 -*-
"""
common.utils.message_queue —— 客户消息 FIFO 队列（Phase 2 前置上移）
====================================================================
本文件用途：实现「客户消息队列」结构，缓存待处理的客服消息并按
**先进先出（FIFO）** 顺序消费，保证「入队顺序 == 消费顺序」（需求 5.3）。
本模块**纯 asyncio、平台无关**，供各服务（websocket / 后续 scheduler 复用等）
以 ``common.utils.message_queue`` 统一引用，消除跨包 import。

历史沿革（Phase 2 前置）：原实现位于 ``websocket/channel_pdd/message_queue.py``，
因 PDDChannel / TikTokChannel / connection_manager 均依赖且队列本身平台无关，
按 PLAN §4.1「Phase 2 可评估搬家」上移至此；``channel_pdd.message_queue`` 保留
为兼容转发表（re-export），存量调用方零改动。

设计参照 Customer-Agent-1.2.0 的 ``Message/core/queue.py``
（``SimpleMessageQueue`` / ``QueueManager``，基于 ``asyncio.Queue`` 的 FIFO
``put`` / ``get``），并按本系统多服务架构与开发规范做如下调整：
- 基于 ``asyncio.Queue`` 实现，天然保证 FIFO 顺序与异步并发安全；
- 入队消息以 ``QueuedMessage`` 包装，附带自增序号 ``seq`` 与入队时间戳，
  自增序号用于在测试中校验「入队顺序 == 消费顺序」；
- 提供 ``MessageQueueManager`` 以「店铺 / 会话」为维度管理多个独立队列，
  不同店铺的消息互不串扰；
- 全中文注释，导入置顶，单文件 ≤500 行（规范 35/37/50/51）。

说明：本模块只负责「按序缓存与消费」这一纯结构能力，不与具体消息解析
（任务 10.10）或处理器链（任务 12.x）耦合，``payload`` 以任意对象承载，
便于后续接入 ``Context`` 等结构。
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("common.utils.message_queue")

# 队列默认最大容量（防止异常情况下无限堆积导致内存泄漏）。
DEFAULT_MAX_SIZE: int = 1000


@dataclass
class QueuedMessage:
    """入队消息包装器。

    Attributes:
        payload: 实际承载的消息内容（任意对象，如后续的 ``Context``）。
        seq: 入队自增序号，从 0 起按入队先后单调递增；用于校验消费顺序。
        timestamp: 入队时间戳（``time.time()``，单位秒）。
    """

    payload: Any
    seq: int = -1
    timestamp: float = field(default_factory=time.time)


@dataclass
class QueueStats:
    """队列统计信息。

    Attributes:
        total_enqueued: 累计成功入队条数。
        total_dequeued: 累计成功出队条数。
        current_size: 当前队列内待消费条数。
    """

    total_enqueued: int = 0
    total_dequeued: int = 0
    current_size: int = 0


class FifoMessageQueue:
    """基于 ``asyncio.Queue`` 的客户消息 FIFO 队列。

    保证「入队顺序 == 消费顺序」：``asyncio.Queue`` 内部为 FIFO，单一消费者
    顺序 ``get`` 即可严格按入队先后取出消息（需求 5.3）。

    本类面向「单店铺 / 单会话」的一条消息流，多条消息流由
    ``MessageQueueManager`` 以名称区分管理。
    """

    def __init__(self, name: str, max_size: int = DEFAULT_MAX_SIZE) -> None:
        """初始化队列。

        Args:
            name: 队列名称（通常为店铺 / 会话标识），便于日志定位。
            max_size: 队列最大容量；须为正整数，超出则入队抛出异常。

        Raises:
            ValueError: ``max_size`` 非正整数时抛出。
        """
        if max_size <= 0:
            raise ValueError("队列最大容量 max_size 必须为正整数")

        self.name = name
        self.max_size = max_size
        # asyncio.Queue 天然 FIFO，保证入队与消费顺序一致。
        self._queue: "asyncio.Queue[QueuedMessage]" = asyncio.Queue(maxsize=max_size)
        # 自增序号生成器：为每条入队消息分配单调递增序号。
        self._seq_counter = itertools.count()
        self._stats = QueueStats()
        self._closed = False

    async def put(self, payload: Any) -> QueuedMessage:
        """将一条客户消息入队（按到达顺序追加到队尾）。

        Args:
            payload: 消息内容（任意对象）。

        Returns:
            入队后的 ``QueuedMessage`` 包装（含分配的自增序号 ``seq``）。

        Raises:
            RuntimeError: 队列已关闭或已满时抛出。
        """
        if self._closed:
            raise RuntimeError(f"队列 {self.name} 已关闭，拒绝入队")

        if self._queue.full():
            logger.warning("队列 %s 已满（容量 %d），拒绝入队", self.name, self.max_size)
            raise RuntimeError(f"队列 {self.name} 已满")

        message = QueuedMessage(payload=payload, seq=next(self._seq_counter))
        # asyncio.Queue 未满时 put 立即完成，不会阻塞；顺序追加到队尾。
        await self._queue.put(message)
        self._stats.total_enqueued += 1
        self._stats.current_size = self._queue.qsize()
        return message

    async def get(self, timeout: Optional[float] = None) -> Optional[QueuedMessage]:
        """从队首取出一条消息（FIFO，保证与入队顺序一致）。

        Args:
            timeout: 等待超时时间（秒）；为 ``None`` 时阻塞直到有消息。

        Returns:
            队首的 ``QueuedMessage``；当队列已关闭且为空，或等待超时，返回 ``None``。
        """
        if self._closed and self._queue.empty():
            return None

        try:
            if timeout is not None:
                message = await asyncio.wait_for(self._queue.get(), timeout)
            else:
                message = await self._queue.get()
        except asyncio.TimeoutError:
            return None

        self._stats.total_dequeued += 1
        self._stats.current_size = self._queue.qsize()
        return message

    def size(self) -> int:
        """返回当前队列内待消费消息条数。"""
        return self._queue.qsize()

    def is_empty(self) -> bool:
        """队列是否为空。"""
        return self._queue.empty()

    def is_full(self) -> bool:
        """队列是否已满。"""
        return self._queue.full()

    def get_stats(self) -> QueueStats:
        """返回队列统计信息快照。"""
        return QueueStats(
            total_enqueued=self._stats.total_enqueued,
            total_dequeued=self._stats.total_dequeued,
            current_size=self._queue.qsize(),
        )

    def close(self) -> None:
        """关闭队列：关闭后拒绝入队；已入队消息仍可继续消费直至取空。"""
        self._closed = True
        logger.info("队列 %s 已关闭", self.name)

    @property
    def closed(self) -> bool:
        """队列是否已关闭。"""
        return self._closed

    async def drain(self) -> List[QueuedMessage]:
        """一次性取出当前队列中的全部消息（按 FIFO 顺序）。

        主要用于测试与队列清理场景；不阻塞等待新消息。

        Returns:
            按入队先后排列的 ``QueuedMessage`` 列表。
        """
        drained: List[QueuedMessage] = []
        while not self._queue.empty():
            try:
                message = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._stats.total_dequeued += 1
            drained.append(message)
        self._stats.current_size = self._queue.qsize()
        return drained


class MessageQueueManager:
    """消息队列管理器：以名称（店铺 / 会话标识）维度管理多个独立 FIFO 队列。

    不同名称的队列彼此独立，保证多店铺 / 多会话的消息互不串扰，且各自维持
    自身的 FIFO 顺序。
    """

    def __init__(self, default_max_size: int = DEFAULT_MAX_SIZE) -> None:
        """初始化管理器。

        Args:
            default_max_size: 创建队列时的默认最大容量。
        """
        self._queues: Dict[str, FifoMessageQueue] = {}
        self._default_max_size = default_max_size

    def get_or_create(
        self, name: str, max_size: Optional[int] = None
    ) -> FifoMessageQueue:
        """获取已存在的队列，不存在则按需创建。

        Args:
            name: 队列名称（店铺 / 会话标识）。
            max_size: 队列最大容量；为 ``None`` 时采用管理器默认值。

        Returns:
            对应名称的 ``FifoMessageQueue`` 实例。
        """
        queue = self._queues.get(name)
        if queue is None:
            queue = FifoMessageQueue(
                name, max_size if max_size is not None else self._default_max_size
            )
            self._queues[name] = queue
            logger.info("创建消息队列：%s", name)
        return queue

    def get(self, name: str) -> Optional[FifoMessageQueue]:
        """获取指定名称的队列；不存在返回 ``None``。"""
        return self._queues.get(name)

    def list_names(self) -> List[str]:
        """列出当前所有队列名称。"""
        return list(self._queues.keys())

    def list_stats(self) -> Dict[str, QueueStats]:
        """返回所有队列的统计信息映射。"""
        return {name: queue.get_stats() for name, queue in self._queues.items()}

    def remove(self, name: str) -> None:
        """移除并关闭指定名称的队列（若存在）。"""
        queue = self._queues.pop(name, None)
        if queue is not None:
            queue.close()
            logger.info("移除消息队列：%s", name)

    def close_all(self) -> None:
        """关闭所有队列（不删除，便于继续消费残余消息）。"""
        for queue in self._queues.values():
            queue.close()
        logger.info("已关闭全部消息队列（共 %d 个）", len(self._queues))


# 全局消息队列管理器实例（供 websocket 服务运行时共享使用）。
message_queue_manager = MessageQueueManager()


__all__ = [
    "QueuedMessage",
    "QueueStats",
    "FifoMessageQueue",
    "MessageQueueManager",
    "message_queue_manager",
    "DEFAULT_MAX_SIZE",
]
