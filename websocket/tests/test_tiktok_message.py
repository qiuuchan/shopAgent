# -*- coding: utf-8 -*-
"""
websocket.tests.test_tiktok_message —— TikTok 消息解析单元测试（TIK-012）
=========================================================================
本文件用途：验证 ``channel_tiktok.tiktok_message`` 的解析逻辑：
- raw → Context（复用 channel_pdd.pdd_message.Context）；
- 角色归一化（buyer→'user' / seller→'mall_cs'）；
- 消息类型映射（text→TEXT / image→IMAGE / 其余→SYSTEM_STATUS）；
- 非法 raw 容错（非字典 / 缺字段不抛异常，返回 None 或等价处理）。
"""
from __future__ import annotations

import pytest

from channel_pdd.pdd_message import Context, ContextType
from channel_tiktok.tiktok_message import TikTokChatMessage, parse_tiktok_raw


def _raw(sender_role="buyer", msg_type="text", **extra):
    """构造一条 TikTok 原始会话消息。"""
    msg = {
        "conversation_id": "conv_1",
        "sender_role": sender_role,
        "msg_type": msg_type,
        "content": "你好",
        "msg_id": "m1",
        "nickname": "买家A",
        "from_uid": "u_1",
        "to_uid": "shop_1",
        "timestamp": 1700000000,
    }
    msg.update(extra)
    return msg


def test_buyer_text_to_user_text():
    """买家文本消息：角色归一化为 'user'，类型为 TEXT。"""
    ctx = TikTokChatMessage(_raw()).to_context()
    assert isinstance(ctx, Context)
    assert ctx.kwargs["from_user"] == "user"
    assert ctx.type == ContextType.TEXT
    assert ctx.content == "你好"
    assert ctx.kwargs["conversation_id"] == "conv_1"


def test_seller_text_to_mall_cs():
    """卖家文本消息：角色归一化为 'mall_cs'。"""
    ctx = TikTokChatMessage(_raw(sender_role="seller")).to_context()
    assert ctx.kwargs["from_user"] == "mall_cs"
    assert ctx.type == ContextType.TEXT


def test_image_type_mapping():
    """图片消息映射为 IMAGE 类型。"""
    ctx = TikTokChatMessage(_raw(msg_type="image", content="u.png")).to_context()
    assert ctx.type == ContextType.IMAGE
    assert ctx.content == "u.png"


def test_other_type_to_system_status():
    """其余类型（如撤回 / 订单 / 系统）映射为 SYSTEM_STATUS，不进决策链。"""
    ctx = TikTokChatMessage(_raw(msg_type="recall", content="已撤回")).to_context()
    assert ctx.type == ContextType.SYSTEM_STATUS


def test_unknown_role_to_system_status():
    """未知发送角色：归为 SYSTEM_STATUS 占位，不抛异常。"""
    ctx = TikTokChatMessage(_raw(sender_role="robot")).to_context()
    assert ctx.type == ContextType.SYSTEM_STATUS
    assert ctx.kwargs["from_user"] == "robot"


def test_shop_id_injected():
    """to_context 注入 shop_id / shop_name 到 kwargs。"""
    ctx = TikTokChatMessage(_raw()).to_context(shop_id="s1", shop_name="店A")
    assert ctx.kwargs["shop_id"] == "s1"
    assert ctx.kwargs["shop_name"] == "店A"


def test_invalid_raw_none():
    """非法 raw（None）：valid=False，to_context 返回 None，不抛异常。"""
    chat = TikTokChatMessage(None)
    assert chat.valid is False
    assert chat.to_context() is None


def test_invalid_raw_non_dict():
    """非法 raw（字符串）：to_context 返回 None。"""
    assert TikTokChatMessage("not-a-dict").to_context() is None


def test_parse_tiktok_raw_function():
    """parse_tiktok_raw 快捷函数：成功返回 Context，非法返回 None。"""
    ctx = parse_tiktok_raw(_raw())
    assert isinstance(ctx, Context)
    assert ctx.kwargs["from_user"] == "user"

    assert parse_tiktok_raw(123) is None
    assert parse_tiktok_raw({}) is not None  # 空字典合法但角色未知 → SYSTEM_STATUS


def test_empty_dict_raw_does_not_raise():
    """空字典 raw：不抛异常，解析为 SYSTEM_STATUS（未知角色占位）。"""
    ctx = TikTokChatMessage({}).to_context()
    assert ctx is not None
    assert ctx.type == ContextType.SYSTEM_STATUS


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
