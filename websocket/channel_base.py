# -*- coding: utf-8 -*-
"""
channel_base —— 平台通道协议与平台常量
=====================================
本文件用途：定义多平台分派所需的最小「通道协议」与平台枚举常量，供
``connection_manager`` 工厂按 ``platform`` 分派具体通道实现（需求 24.x 多平台
分派前置）。本文件由 TIK-010 建立协议层与工厂分派；TikTok 分支最初以占位桩
落地，TIK-013 交付真实通道、TIK-014 完成工厂装配（原 ``TikTokStubChannel``
已随 TIK-014 移除）。

设计约束（不做大协议）：
- 注册表（connection_registry）鸭子类型只消费 ``async stop()``，因此协议只要求
  ``stop`` 为必实现方法；状态查询消费 ``get_connection_status()``。
- 消息入站统一复用 ``common.utils.message_queue`` 的 FIFO 队列 +
  ``message_handler(raw, shop_id, user_id)`` 回调约定（PDDChannel 已定义该签名，
  TikTok 复用同一约定），协议层不再抽象消息入队细节。
- 协议保持「最小」：仅 ``shop_id`` / ``user_id`` 两个属性与
  ``start`` / ``stop`` / ``get_connection_status`` 三个方法，避免过早抽象。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、单文件 ≤500 行（35）、
文件名用下划线（40）、全中文（50）、日志禁 debug（38）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

# 平台枚举常量：拼多多（存量默认）/ TikTok Shop（待 TIK-013 真实现）。
PLATFORM_PDD: str = "pdd"
PLATFORM_TIKTOK: str = "tiktok"

# 允许的平台集合：分派前校验，非法平台回退到 PDD 默认路径（向后兼容）。
ALLOWED_PLATFORMS: tuple[str, ...] = (PLATFORM_PDD, PLATFORM_TIKTOK)


class ChannelAdapter(Protocol):
    """平台通道协议：与 connection_registry（鸭子类型 stop()）与 connection_manager

    装配约定对齐的最小接口。

    实现方（PDDChannel / TikTokChannel 桩 / 未来 TikTokChannel 真实现）须满足：
    - 暴露 ``shop_id``（业务标识）与 ``user_id``（归属用户 ID）两个属性；
    - 提供 ``async start()`` 拉起连接/监控循环；
    - 提供 ``async stop()``（注册表断连唯一调用点）；
    - 提供 ``get_connection_status()`` 供连接状态查询（含 state / 最近心跳等）。
    """

    # 店铺业务标识（字符串）。
    shop_id: str
    # 归属用户 ID（整数）。
    user_id: int

    async def start(self) -> None:
        """拉起通道连接/监控循环。"""
        ...

    async def stop(self) -> None:
        """停止通道并清理资源（注册表断连唯一调用点）。"""
        ...

    def get_connection_status(self) -> Optional[Dict[str, Any]]:
        """返回当前连接状态字典（state / state_label / 最近心跳等）；无记录可 None。"""
        ...


def is_allowed_platform(platform: str) -> bool:
    """判断平台是否在允许集合内。

    Args:
        platform: 待校验平台标识。

    Returns:
        True 表示合法；否则 False（调用方应回退 PDD 默认路径）。
    """
    return platform in ALLOWED_PLATFORMS


__all__ = [
    "PLATFORM_PDD",
    "PLATFORM_TIKTOK",
    "ALLOWED_PLATFORMS",
    "ChannelAdapter",
    "is_allowed_platform",
]
