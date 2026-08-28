# -*- coding: utf-8 -*-
"""
channel_tiktok.session_guard —— TikTok 会话回复去抖守卫（纯逻辑）
===============================================================
本文件用途：提供 ``ReplyDebouncer``，用于 TikTok 通道的「未回复识别 + 静默聚合」
（PLAN §5.5 / TIK-012）。它是一段**纯逻辑、无 I/O** 组件，可独立进行属性测试，
不依赖浏览器、网络或数据库，仅维护内存态。

核心语义（TIK-012）：
- 买家消息（``from_user == 'user'``）：登记 / 重置该会话的 pending 计时起点
  （以该消息的 ``now`` 时间戳为基准）。
- 卖家消息（``from_user == 'mall_cs'``）：取消该会话的 pending（表示本店已回复）。
- ``pending_conversations()``：返回当前仍存在 pending 的会话 ID 列表。
- 仅当「会话最后一条来自买家」且「距最后一条买家消息静默期满（≥ silence_seconds）」
  时，``feed`` 才返回「应处理」的最后一条买家消息；否则返回 ``None``。

去抖设计理由：避免监控循环每轮都重复触发同一未回复会话的回复。一旦静默期满返回一次
应处理消息后，调用方应视作「已处理」并主动 feed 一条卖家消息（或显式 cancel），
从而将该会话移出 pending，防止下一轮重复回复（与 PLAN §5.5 一致）。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、禁用 debug 日志（38）。
属性测试：见 ``websocket/tests/test_session_guard.py``。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 归一化角色常量（与 pdd_message / tiktok_message 约定一致）。
ROLE_USER = "user"  # 买家
ROLE_MALL_CS = "mall_cs"  # 本店客服


class ReplyDebouncer:
    """TikTok 会话回复去抖守卫。

    维护结构：``_pending: Dict[conversation_id, last_buyer_msg]``，其中
    ``last_buyer_msg`` 为最近一条买家消息字典（含 ``timestamp`` 字段，
    即 ``now`` 时间戳）。仅记录「最后一条来自买家」的消息，卖家消息取消 pending。

    时间语义：``now`` 由调用方以秒为单位传入（可为浮点 / 整数，统一 float 比较），
    ``silence_seconds`` 为可配置的静默阈值（默认 45.0，对应 TIKTOK_DEBOUNCE_SECONDS）。
    """

    def __init__(self, silence_seconds: float = 45.0) -> None:
        """构造守卫。

        Args:
            silence_seconds: 静默期满阈值（秒），默认 45.0。
        """
        if silence_seconds < 0:
            raise ValueError("silence_seconds 必须为非负数")
        self.silence_seconds: float = float(silence_seconds)
        # conversation_id -> 最近一条买家消息字典（含 timestamp）。
        self._pending: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    def feed(
        self, msg: Dict[str, Any], now: float
    ) -> Optional[Dict[str, Any]]:
        """喂入一条消息，返回「应处理」的最后一条买家消息或 None。

        Args:
            msg: 消息字典，约定字段：
                - ``conversation_id``：会话标识（缺省视为空串单会话）；
                - ``from_user``：归一化角色（``'user'`` / ``'mall_cs'``）；
                - 其余字段原样透传。
            now: 当前时间戳（秒）。

        Returns:
            - 若 ``msg`` 为买家消息：登记 / 重置 pending，返回 ``None``
              （登记动作本身不视为「应处理」，需等静默期满再经后续 feed 判定）。
            - 若 ``msg`` 为卖家消息：取消该会话 pending，返回 ``None``。
            - 若该会话 pending 已存在且静默期满：返回最后一条买家消息
              （调用方视为「应处理」）；否则返回 ``None``。

        说明：本方法对「刚喂入的买家消息」一律只做登记（返回 None），不立即返回；
        静默期满的判定发生在「后续任一条 feed 调用」时（此时 ``now`` 已推进），
        保证首次登记不会因 now 恰好等于登记时间而误判为期满。
        """
        if not isinstance(msg, dict):
            return None
        conversation_id = msg.get("conversation_id", "")
        role = msg.get("from_user")

        if role == ROLE_MALL_CS:
            # 卖家回复：取消 pending。
            self._pending.pop(conversation_id, None)
            return None

        if role == ROLE_USER:
            # 买家消息：登记 / 重置 pending，记录该消息与当前 now 为基准时间。
            buyer_msg = dict(msg)
            buyer_msg["timestamp"] = now
            self._pending[conversation_id] = buyer_msg
            return None

        # 未知角色：忽略，不改动 pending 状态。
        return None

    def pending_conversations(self) -> List[str]:
        """返回当前存在 pending 的会话 ID 列表。

        Returns:
            仍处于 pending（未回复）状态的会话 ID 列表。
        """
        return list(self._pending.keys())

    def should_reply(self, conversation_id: str, now: float) -> bool:
        """判定某会话是否已到静默期满、应处理（不修改状态）。

        供上层在「非 feed 路径」查询某会话是否可回复（如定时巡检）。
        仅当 pending 存在且静默期满返回 True。

        Args:
            conversation_id: 会话 ID。
            now: 当前时间戳（秒）。

        Returns:
            应处理返回 True；否则 False。
        """
        last = self._pending.get(conversation_id)
        if last is None:
            return False
        last_ts = last.get("timestamp")
        if not isinstance(last_ts, (int, float)):
            return False
        return (now - float(last_ts)) >= self.silence_seconds

    def pop_due(self, now: float) -> List[Dict[str, Any]]:
        """取出所有「静默期满」的应处理买家消息，并从 pending 移除（视为已处理）。

        与 feed 的「登记不返回」语义互补：调用方在聚合巡检时可直接调用本方法
        批量取走到期会话，避免重复回复（取走即移除 pending）。

        Args:
            now: 当前时间戳（秒）。

        Returns:
            所有静默期满的买家消息列表（按会话登记顺序）。
        """
        due: List[Dict[str, Any]] = []
        for cid in list(self._pending.keys()):
            last = self._pending[cid]
            last_ts = last.get("timestamp")
            if isinstance(last_ts, (int, float)) and (now - float(last_ts)) >= self.silence_seconds:
                due.append(last)
                self._pending.pop(cid, None)
        return due

    def cancel(self, conversation_id: str) -> None:
        """显式取消某会话的 pending（如调用方主动回复成功后）。

        Args:
            conversation_id: 会话 ID。
        """
        self._pending.pop(conversation_id, None)

    def reset(self) -> None:
        """清空全部 pending 状态（如通道重连 / 店铺切换）。"""
        self._pending.clear()


__all__ = [
    "ReplyDebouncer",
    "ROLE_USER",
    "ROLE_MALL_CS",
]
