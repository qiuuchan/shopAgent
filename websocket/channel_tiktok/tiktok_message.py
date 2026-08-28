# -*- coding: utf-8 -*-
"""
channel_tiktok.tiktok_message —— TikTok 消息解析与上下文提取
============================================================
本文件用途：将 TikTok 卖家后台监控循环产出的「原始会话消息」解析为本系统统一的
``Context`` 数据结构（复用 ``channel_pdd.pdd_message`` 的 ``Context`` / ``ContextType``，
仅 import 不搬移，避免历史位置重复实现），供自动回复引擎、AI 回复引擎与消息日志使用。

职责（TIK-012）：
- 角色归一化：原始 ``sender_role`` 取值 ``buyer``（买家）→ ``Context.kwargs["from_user"]``
  取值 ``'user'``；``seller``（本店客服）→ 取值 ``'mall_cs'``；与 PDD 语义对齐，
  MessageConsumer 的角色过滤零改动（见 PLAN §4.3）。
- 消息类型映射：``text`` → ``ContextType.TEXT``；``image`` → ``ContextType.IMAGE``；
  其余（系统状态 / 撤回 / 订单 / 商品等尚未进入决策链的类型）→ ``ContextType.SYSTEM_STATUS``
  （不进决策链，仅做占位与日志）。
- 非法 raw 容错：输入非字典 / 缺关键字段时不抛异常，返回 ``None``（等价处理），
  由上层跳过该条消息。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、日志禁用 debug（38）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# 复用 PDD 既有 DTO（位置历史原因，仅 import 不搬移）。
from channel_pdd.pdd_message import Context, ContextType

logger = None  # 延迟导入，避免模块顶部耦合 logging（保持轻量）


def _get_logger():
    """延迟获取模块级 logger（禁用 debug 级别，规范 38）。"""
    global logger
    if logger is None:
        import logging

        logger = logging.getLogger("channel_tiktok.tiktok_message")
    return logger


# 原始消息类型（TikTok 监控循环产出 raw 的 ``msg_type`` 取值）。
_RAW_TEXT = "text"
_RAW_IMAGE = "image"

# 原始角色（TikTok 会话消息 ``sender_role`` 取值）。
_ROLE_BUYER = "buyer"
_ROLE_SELLER = "seller"


class TikTokChatMessage:
    """TikTok 消息解析入口。

    构造时解析原始报文（``raw``），得到：
    - ``user_msg_type``：解析出的 ``ContextType``；
    - ``content``：消息内容（文本 / 图片地址 / 提示文本）；
    - ``from_user``：归一化后的角色（``'user'`` / ``'mall_cs'``）；
    - 以及 msg_id / nickname / from_uid / to_uid / timestamp 等元信息。

    并提供 ``to_context()`` 将解析结果转换为统一的 ``Context`` 数据结构。
    """

    def __init__(self, raw: Dict[str, Any]) -> None:
        """解析 TikTok 原始消息。

        Args:
            raw: 监控循环产出的原始字典，约定字段包括
                ``conversation_id`` / ``sender_role`` / ``msg_type`` /
                ``content`` / ``msg_id`` / ``nickname`` / ``timestamp`` 等。
                非字典或结构异常时，解析结果置为无效（``valid=False``）。
        """
        self.raw: Dict[str, Any] = raw if isinstance(raw, dict) else {}
        self.valid: bool = True

        self.msg_id: Optional[str] = None
        self.nickname: Optional[str] = None
        self.from_user: Optional[str] = None
        self.from_uid: Optional[str] = None
        self.to_uid: Optional[str] = None
        self.timestamp: Optional[Any] = None
        self.conversation_id: Optional[str] = None
        self.user_msg_type: ContextType = ContextType.SYSTEM_STATUS
        self.content: Any = None

        if not isinstance(raw, dict):
            # 非法 raw 容错：不抛异常，标记为无效。
            self.valid = False
            return

        self._process()

    # ------------------------------------------------------------------
    # 解析逻辑
    # ------------------------------------------------------------------
    def _process(self) -> None:
        """抽取基础元信息并按类型 / 角色归一化。"""
        self.msg_id = self.raw.get("msg_id")
        self.nickname = self.raw.get("nickname")
        self.from_uid = self.raw.get("from_uid")
        self.to_uid = self.raw.get("to_uid")
        self.timestamp = self.raw.get("timestamp")
        self.conversation_id = self.raw.get("conversation_id")

        # 角色归一化（PLAN §4.3）。
        sender_role = self.raw.get("sender_role")
        if sender_role == _ROLE_BUYER:
            self.from_user = "user"
        elif sender_role == _ROLE_SELLER:
            self.from_user = "mall_cs"
        else:
            # 未知角色：作为系统状态占位，不进决策链。
            self.from_user = sender_role
            self.user_msg_type = ContextType.SYSTEM_STATUS
            self.content = f"未知发送角色: {sender_role}"
            return

        # 消息类型映射。
        msg_type = self.raw.get("msg_type")
        if msg_type == _RAW_TEXT:
            self.user_msg_type = ContextType.TEXT
            self.content = self.raw.get("content")
        elif msg_type == _RAW_IMAGE:
            self.user_msg_type = ContextType.IMAGE
            self.content = self.raw.get("content")
        else:
            # 其余类型（撤回 / 订单 / 系统状态等）Phase 1 不进决策链。
            self.user_msg_type = ContextType.SYSTEM_STATUS
            self.content = self.raw.get("content")

    def to_context(
        self, shop_id: Optional[str] = None, shop_name: Optional[str] = None
    ) -> Optional[Context]:
        """将解析结果转换为统一的 Context 数据结构。

        非法 raw（``valid=False``）时返回 ``None``，由上层跳过。

        Args:
            shop_id: 店铺业务标识（可选，注入 kwargs）。
            shop_name: 店铺名称（可选，注入 kwargs）。

        Returns:
            成功返回 ``Context``；无效消息返回 ``None``。
        """
        if not self.valid:
            _get_logger().warning("跳过非法 TikTok 原始消息: raw=%s", type(self.raw).__name__)
            return None

        kwargs: Dict[str, Any] = {
            "msg_id": self.msg_id,
            "nickname": self.nickname,
            "from_user": self.from_user,
            "from_uid": self.from_uid,
            "to_user": None,  # TikTok raw 暂无独立 to 角色字段，置空保持对齐
            "to_uid": self.to_uid,
            "timestamp": self.timestamp,
            "conversation_id": self.conversation_id,
            "shop_id": shop_id,
            "shop_name": shop_name,
        }

        return Context(
            type=self.user_msg_type,
            content=self.content,
            order_context=None,
            goods_context=None,
            kwargs=kwargs,
        )


def parse_tiktok_raw(
    raw: Any, shop_id: Optional[str] = None, shop_name: Optional[str] = None
) -> Optional[Context]:
    """TikTok 原始消息快速解析函数（供 MessageConsumer 注入 message_parser 使用）。

    Args:
        raw: 原始消息（任意类型；非字典返回 None）。
        shop_id: 店铺业务标识（可选）。
        shop_name: 店铺名称（可选）。

    Returns:
        解析成功返回 ``Context``；非法 raw 返回 ``None``。
    """
    if not isinstance(raw, dict):
        return None
    return TikTokChatMessage(raw).to_context(shop_id=shop_id, shop_name=shop_name)


__all__ = [
    "TikTokChatMessage",
    "parse_tiktok_raw",
]
