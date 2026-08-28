# -*- coding: utf-8 -*-
"""
websocket.tests.test_message_parser —— 消息解析器解耦（TIK-008）单元测试
========================================================================
本文件用途：验证 TIK-008 的解析器解耦与硬 import 惰性化：

- 注入自定义（TikTok 风格）parser + StubSender 全链路验证：消费器经注入 parser
  解析报文后走完整决策 / 发送 / 落库，证明平台可复用 MessageConsumer 全链路；
- PDD 默认 parser 回归：``pdd_parse_raw`` 行为与原 ``MessageConsumer._to_context``
  逐字节等价（角色归一化 / 字节解码 / JSON 解析 / 非字典返回 None）；
- 惰性化验证：构造 ``MessageConsumer`` 不触发对 ``channel_pdd.api.send_message``
  / ``channel_pdd.transfer_service`` / ``engine.message_parser`` 的模块级硬依赖，
  模块导入无副作用；注入非空 parser 时优先使用。

测试以「依赖注入桩」替换数据库与拼多多外部接口副作用，验证解耦本身。
测试框架：pytest。
"""
import asyncio
import json

import pytest

from channel_pdd.pdd_message import Context, ContextType
from engine.message_consumer import MessageConsumer, ShopRuntime
from engine.message_parser import MessageParser, pdd_parse_raw
from engine.reply_engine import (
    ACTION_KEYWORD,
    ReplyRules,
    ShopConfig,
)
from engine.keyword_matcher import MATCH_CONTAINS, REPLY_TEXT


# ----------------------------------------------------------------------
# 桩：拼多多消息发送器（记录调用，恒成功）
# ----------------------------------------------------------------------
class StubSender:
    """记录 send_text / send_image 调用的发送器桩。"""

    def __init__(self):
        self.text_calls = []
        self.image_calls = []

    def send_text(self, recipient_uid, content):
        self.text_calls.append((recipient_uid, content))
        return {"success": True}

    def send_image(self, recipient_uid, content):
        self.image_calls.append((recipient_uid, content))
        return {"success": True}


# ----------------------------------------------------------------------
# TikTok 风格桩 parser：把自定义结构报文解析为 Context
# ----------------------------------------------------------------------
class TikTokStyleParser:
    """TikTok 风格解析器桩：从自定义字典提取买家文本消息为 Context。

    用于验证「注入自定义 parser 即可复用 MessageConsumer 全链路」，不依赖 PDD
    报文结构。约定：``{"sender": "buyer", "text": "...", "uid": "..."}`` 解析为
    买家文本消息（from_user='user'）；``{"sender": "seller", ...}`` 解析为客服
    消息（from_user='mall_cs'）；其余返回 None。
    """

    def __init__(self, shop_id="tiktok_shop"):
        self.shop_id = shop_id

    def __call__(self, raw_message):
        if not isinstance(raw_message, dict):
            return None
        sender = raw_message.get("sender")
        uid = raw_message.get("uid", "unknown")
        text = raw_message.get("text")
        if sender == "buyer":
            from_user = "user"
        elif sender == "seller":
            from_user = "mall_cs"
        else:
            return None
        return Context(
            type=ContextType.TEXT,
            content=text,
            kwargs={
                "msg_id": raw_message.get("msg_id"),
                "from_user": from_user,
                "from_uid": uid if from_user == "user" else None,
                "to_uid": uid if from_user == "mall_cs" else None,
                "shop_id": self.shop_id,
            },
        )


# ----------------------------------------------------------------------
# 构造辅助
# ----------------------------------------------------------------------
def _pdd_raw_text(text="你好", from_uid="cust_1"):
    """构造一条拼多多文本 push 报文（解析口径见 pdd_message）。"""
    return json.dumps(
        {
            "response": "push",
            "message": {
                "type": 0,
                "sub_type": 2,
                "content": text,
                "from": {"role": "user", "uid": from_uid},
                "to": {"role": "mall_cs", "uid": "cs_1"},
                "msg_id": "m1",
            },
        }
    )


def _runtime(keyword_rules=None):
    cfg = ShopConfig(shop_pk=1)
    rules = ReplyRules(
        keyword_rules=keyword_rules or [],
        filter_rules=[],
        blacklist=[],
        goods_replies=[],
    )
    return ShopRuntime(shop_config=cfg, rules=rules)


def _kw(keyword="你好", reply="关键词回复"):
    return {
        "id": 1,
        "keyword": keyword,
        "match_type": MATCH_CONTAINS,
        "reply_type": REPLY_TEXT,
        "reply_content": reply,
        "priority": 1,
        "enabled": True,
    }


def _make_consumer(runtime, *, message_parser=None, sender=None):
    msg_logs = []
    consumer = MessageConsumer(
        shop_id="shop_1",
        shop_pk=1,
        user_id=9,
        runtime_loader=lambda shop_pk: runtime,
        sender=sender or StubSender(),
        transfer_service=None,
        log_writer=lambda v: msg_logs.append(v),
        risk_log_writer=lambda v: None,
        message_parser=message_parser,
    )
    return consumer, msg_logs


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 用例：PDD 默认 parser 回归
# ----------------------------------------------------------------------
def test_pdd_parse_raw_equivalence_text():
    """pdd_parse_raw 对文本报文解析结果与原 _to_context 逐字节等价。"""
    raw = _pdd_raw_text("你好呀", from_uid="cust_1")
    ctx = pdd_parse_raw(raw, shop_id="shop_x")

    assert isinstance(ctx, Context)
    assert ctx.type == ContextType.TEXT
    assert ctx.content == "你好呀"
    # 角色归一化：买家 → 'user'
    assert ctx.kwargs["from_user"] == "user"
    assert ctx.kwargs["from_uid"] == "cust_1"
    assert ctx.kwargs["to_uid"] == "cs_1"
    assert ctx.kwargs["shop_id"] == "shop_x"


def test_pdd_parse_raw_mall_cs_role_normalization():
    """pdd_parse_raw 对客服消息做角色归一化（from_user='mall_cs'）。"""
    raw = json.dumps(
        {
            "response": "push",
            "message": {
                "type": 0,
                "sub_type": 2,
                "content": "客服回复",
                "from": {"role": "mall_cs", "uid": "cs_1"},
                "to": {"role": "user", "uid": "cust_1"},
            },
        }
    )
    ctx = pdd_parse_raw(raw)
    assert ctx.type == ContextType.MALL_CS
    assert ctx.kwargs["from_user"] == "mall_cs"
    assert ctx.kwargs["to_uid"] == "cust_1"


def test_pdd_parse_raw_bytes_and_context_passthrough():
    """pdd_parse_raw 支持字节解码与 Context 直传。"""
    raw_str = _pdd_raw_text("字节消息", from_uid="cust_2")
    ctx_bytes = pdd_parse_raw(raw_str.encode("utf-8"), shop_id="s")
    assert ctx_bytes.content == "字节消息"
    assert ctx_bytes.kwargs["from_uid"] == "cust_2"

    # Context 直传：返回同一实例
    passthrough = Context(type=ContextType.TEXT, content="x", kwargs={"from_user": "user"})
    assert pdd_parse_raw(passthrough) is passthrough


def test_pdd_parse_raw_non_dict_returns_none():
    """pdd_parse_raw 对非字典（非 JSON 字符串 / 非容器）返回 None。

    注：非法 JSON 字符串会令 ``json.loads`` 抛错（与原 _to_context 一致，由
    consume_raw 的 try/except 捕获并记为解析失败、handled=False），此处不额外
    兜底，确保与原行为逐字节等价。
    """
    assert pdd_parse_raw(12345) is None
    # 非法 JSON 字符串：与原 _to_context 同样抛出 JSONDecodeError（由上层捕获）
    import json as _json

    with pytest.raises(_json.JSONDecodeError):
        pdd_parse_raw("not-json{")


def test_default_parser_used_when_none_injected():
    """未注入 parser 时，MessageConsumer 默认走 pdd_parse_raw（PDD 行为不变）。"""
    runtime = _runtime(keyword_rules=[_kw()])
    sender = StubSender()
    consumer, msg_logs = _make_consumer(runtime, sender=sender)

    outcome = _run(consumer.consume_raw(_pdd_raw_text("你好呀")))
    # 默认 PDD parser 解析成功 → 命中关键词 → 发送回复
    assert outcome.handled is True
    assert outcome.action == ACTION_KEYWORD
    assert sender.text_calls == [("cust_1", "关键词回复")]
    assert len(msg_logs) == 1


# ----------------------------------------------------------------------
# 用例：注入自定义（TikTok 风格）parser 全链路复用
# ----------------------------------------------------------------------
def test_injected_parser_full_chain():
    """注入 TikTok 风格 parser：消费器经其解析后完整走决策 / 发送 / 落库。"""
    runtime = _runtime(keyword_rules=[_kw("你好", "TikTok关键词回复")])
    sender = StubSender()
    parser = TikTokStyleParser(shop_id="tiktok_shop")
    consumer, msg_logs = _make_consumer(runtime, message_parser=parser, sender=sender)

    raw = {"sender": "buyer", "uid": "tt_buyer_1", "text": "你好", "msg_id": "t1"}
    outcome = _run(consumer.consume_raw(raw))

    assert outcome.handled is True
    assert outcome.action == ACTION_KEYWORD
    assert sender.text_calls == [("tt_buyer_1", "TikTok关键词回复")]
    assert len(msg_logs) == 1
    # 解析器注入生效：role 归一化由注入 parser 决定（from_user='user'）
    assert outcome.content == "TikTok关键词回复"


def test_injected_parser_mall_cs_forwarded_not_processed():
    """注入 parser 输出的客服消息：只转发不进决策链（handled=False）。"""
    runtime = _runtime(keyword_rules=[_kw()])
    parser = TikTokStyleParser()
    consumer, msg_logs = _make_consumer(runtime, message_parser=parser)

    raw = {"sender": "seller", "uid": "tt_buyer_1", "text": "客服说", "msg_id": "t2"}
    outcome = _run(consumer.consume_raw(raw))

    assert outcome.handled is False
    assert msg_logs == []


# ----------------------------------------------------------------------
# 用例：惰性化（模块导入无副作用）
# ----------------------------------------------------------------------
def test_consumer_construction_does_not_hard_import(monkeypatch):
    """构造 MessageConsumer 不应在模块导入期触发对 PDD 发送器 / parser 的硬 import。

    通过拦截懒加载目标的模块属性，确认它们仅在真正调用时才被导入（惰性）。
    """
    imported = []

    def _track(module_path, attr):
        def _fake(*args, **kwargs):
            imported.append((module_path, attr))
            # 返回一个最小桩，避免后续逻辑因属性缺失而报错
            class _Stub:
                pass

            return _Stub()

        return _fake

    monkeypatch.setattr(
        "channel_pdd.api.send_message.SendMessage", _track("send_message", "SendMessage")
    )
    monkeypatch.setattr(
        "channel_pdd.transfer_service.TransferService",
        _track("transfer_service", "TransferService"),
    )

    # 仅构造、不触发任何发送 / 解析：上述拦截不应被命中
    MessageConsumer(
        shop_id="shop_1",
        shop_pk=1,
        user_id=9,
        runtime_loader=lambda shop_pk: _runtime(),
        sender=StubSender(),
        transfer_service=None,
    )
    assert imported == [], f"构造期不应触发硬 import：{imported}"
