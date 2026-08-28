# -*- coding: utf-8 -*-
"""
websocket.engine.message_parser —— 消息解析器（平台可注入的报文 → Context 解耦）

本文件用途：把「原始报文 → Context」的解析逻辑从 MessageConsumer 中拆出，定义为
一个可注入的函数类型 ``MessageParser``，使 TikTok 等其它平台店铺只需注入自己的
解析器即可复用 MessageConsumer 的整条消费链路（解析 → 决策 → 发送 → 落库 → 通知），
而无需改动 MessageConsumer 的编排逻辑（PLAN §4.3，工单 TIK-008）。

设计要点：
- ``MessageParser`` 为 ``Callable[[Any], Optional[Context]]`` 类型别名：
  - 入参为原始报文（JSON 字符串 / 字节 / 已解析字典 / Context）；
  - 返回解析得到的 ``Context``；无法解析时返回 ``None``。
- ``pdd_parse_raw`` 忠实搬移原 ``MessageConsumer._to_context`` 的拼多多解析逻辑，
  语义逐字节等价：Context 直传 → 字节按 utf-8 忽略解码 → JSON 字符串解析为字典 →
  非字典返回 None → 经 ``PDDChatMessage(raw).to_context(shop_id=...)`` 解析。
- 角色归一化（买家 → ``from_user='user'`` / 本店客服 → ``from_user='mall_cs'``）由
  ``PDDChatMessage`` / ``to_context`` 内部完成（见 channel_pdd.pdd_message），本解析器
  不重复处理，确保与历史行为一致；``consume_raw`` 的角色过滤逻辑因此保持不变。
- 本模块只做解析，不依赖 MessageConsumer，纯函数便于单元测试与属性测试。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、单文件 ≤500 行（35）、
全中文（50）、日志禁用 debug（38）。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from channel_pdd.pdd_message import Context, PDDChatMessage


# 消息解析器类型：把一条原始报文解析为统一 Context；无法解析返回 None。
# 入参 raw_message 可能是字节 / JSON 字符串 / 已解析字典 / 已构建的 Context，
# 由各平台解析器自行决定支持的形态（PDD 支持上述全部形态）。
MessageParser = Callable[[Any], Optional[Context]]


def pdd_parse_raw(raw_message: Any, *, shop_id: Optional[str] = None) -> Optional[Context]:
    """拼多多原始报文解析为 Context（忠实搬移原 MessageConsumer._to_context）。

    解析顺序（与原 _to_context 逐字节等价，含角色归一化）：
    1. 已是 Context：直接返回（避免重复解析）；
    2. 字节 / 字节数组：按 utf-8（忽略非法字节）解码为字符串；
    3. 字符串：按 JSON 解析为字典；
    4. 非字典（解析失败 / 其它类型）：返回 None；
    5. 字典：经 ``PDDChatMessage(raw).to_context`` 解析，由该组件完成
       ``from_user`` 角色归一化（'user' / 'mall_cs'）与订单 / 商品上下文提取。

    Args:
        raw_message: 原始报文（Context / 字节 / JSON 字符串 / 字典）。
        shop_id: 拼多多店铺业务标识，透传给 ``to_context`` 写入 kwargs。

    Returns:
        解析得到的 Context；非字典 / 无法解析返回 None。
    """
    if isinstance(raw_message, Context):
        return raw_message
    if isinstance(raw_message, (bytes, bytearray)):
        raw_message = raw_message.decode("utf-8", errors="ignore")
    if isinstance(raw_message, str):
        raw_message = json.loads(raw_message)
    if not isinstance(raw_message, dict):
        return None
    return PDDChatMessage(raw_message).to_context(shop_id=shop_id)


__all__ = [
    "MessageParser",
    "pdd_parse_raw",
]
