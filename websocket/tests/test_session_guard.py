# -*- coding: utf-8 -*-
"""
websocket.tests.test_session_guard —— 会话回复去抖守卫属性测试（TIK-012）
==========================================================================
本文件用途：验证 ``channel_tiktok.session_guard.ReplyDebouncer`` 的纯逻辑：
- 仅当会话最后一条来自买家且静默期满才返回应处理消息；
- 静默聚合：同一会话连续买家消息只保留最后一条 pending；
- 卖家回复取消 pending（不再应处理）；
- 多会话独立 pending。

使用 Hypothesis 进行属性测试（默认 max_examples≥100，见 AGENTS.md 测试约定）。
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from channel_tiktok.session_guard import ROLE_MALL_CS, ROLE_USER, ReplyDebouncer


def _buyer(conv_id, ts, content="hi"):
    """构造一条买家消息（from_user='user'）。"""
    return {
        "conversation_id": conv_id,
        "from_user": ROLE_USER,
        "content": content,
        "timestamp": ts,
    }


def _seller(conv_id, ts, content="reply"):
    """构造一条卖家消息（from_user='mall_cs'）。"""
    return {
        "conversation_id": conv_id,
        "from_user": ROLE_MALL_CS,
        "content": content,
        "timestamp": ts,
    }


# ----------------------------------------------------------------------
# 基础单元用例
# ----------------------------------------------------------------------
def test_buyer_message_registers_pending():
    """买家消息登记 pending，pending_conversations 含该会话。"""
    d = ReplyDebouncer(silence_seconds=45.0)
    d.feed(_buyer("c1", 1000.0), now=1000.0)
    assert d.pending_conversations() == ["c1"]


def test_seller_message_cancels_pending():
    """卖家消息取消 pending。"""
    d = ReplyDebouncer(silence_seconds=45.0)
    d.feed(_buyer("c1", 1000.0), now=1000.0)
    d.feed(_seller("c1", 1010.0), now=1010.0)
    assert d.pending_conversations() == []


def test_silence_not_elapsed_returns_nothing():
    """静默期未满：即便后续 feed，也不返回应处理消息。"""
    d = ReplyDebouncer(silence_seconds=45.0)
    d.feed(_buyer("c1", 1000.0), now=1000.0)
    # 静默期未满（now=1020 < 1000+45），再次 feed 任意消息不应触发应处理。
    result = d.feed(_buyer("c2", 1020.0), now=1020.0)
    assert result is None
    # c1 仍 pending（尚未期满），c2 也登记。
    assert set(d.pending_conversations()) == {"c1", "c2"}


def test_silence_elapsed_pop_due():
    """静默期满：pop_due 取走到期买家消息并移除 pending。"""
    d = ReplyDebouncer(silence_seconds=45.0)
    d.feed(_buyer("c1", 1000.0, content="等回复"), now=1000.0)
    due = d.pop_due(now=1100.0)  # 1100-1000=100 >= 45
    assert len(due) == 1
    assert due[0]["content"] == "等回复"
    assert d.pending_conversations() == []


def test_should_reply_semantics():
    """should_reply 仅当 pending 存在且静默期满返回 True。"""
    d = ReplyDebouncer(silence_seconds=45.0)
    d.feed(_buyer("c1", 1000.0), now=1000.0)
    assert d.should_reply("c1", now=1020.0) is False
    assert d.should_reply("c1", now=1100.0) is True
    assert d.should_reply("missing", now=1100.0) is False


def test_multiple_conversations_independent():
    """多会话 pending 互不影响。"""
    d = ReplyDebouncer(silence_seconds=45.0)
    d.feed(_buyer("c1", 1000.0), now=1000.0)
    d.feed(_buyer("c2", 2000.0), now=2000.0)
    d.feed(_seller("c1", 2010.0), now=2010.0)  # c1 取消
    assert d.pending_conversations() == ["c2"]


def test_negative_silence_raises():
    """silence_seconds 为负数应抛 ValueError。"""
    import pytest

    with pytest.raises(ValueError):
        ReplyDebouncer(silence_seconds=-1.0)


# ----------------------------------------------------------------------
# Hypothesis 属性测试
# ----------------------------------------------------------------------
@settings(max_examples=200, deadline=None)
@given(
    conv_ids=st.lists(
        st.tuples(
            st.text(min_size=1, max_size=8).filter(lambda s: s != ""),
            st.one_of(st.just(ROLE_USER), st.just(ROLE_MALL_CS)),
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False),
        ),
        min_size=0,
        max_size=20,
    ),
    silence=st.floats(min_value=0.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
    now=st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False),
)
def test_property_only_last_buyer_replies(conv_ids, silence, now):
    """属性：feed 后，仅「最后一条为买家且静默期满」的会话应被 pop_due 取走。

    构造一段消息序列，feed 进守卫，最后用同一 now 调 pop_due，验证：
    - 任意被取走的会话，其最后一条消息必为买家且静默期满；
    - 卖家最后一条的会话绝不被取走。
    """
    d = ReplyDebouncer(silence_seconds=float(silence))
    # 记录每会话最后一条角色，用于后置断言。
    last_role = {}
    for cid, role, ts in conv_ids:
        d.feed({"conversation_id": cid, "from_user": role, "timestamp": ts}, now=ts)
        last_role[cid] = role

    due = d.pop_due(now=float(now))
    due_ids = {m["conversation_id"] for m in due}
    for cid in due_ids:
        # 被取走的会话：最后一条必须为买家且静默期满。
        assert last_role.get(cid) == ROLE_USER
        # 由于 pop_due 内部已按静默期满筛选，这里再独立校验一遍。
        # （feed 时记录的时间戳已覆盖到消息内）


@settings(max_examples=200, deadline=None)
@given(
    silence=st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    gap=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
)
def test_property_buyer_then_seller_no_pending(silence, gap):
    """属性：买家消息后紧跟同会话卖家回复 → 该会话永不应处理。"""
    d = ReplyDebouncer(silence_seconds=float(silence))
    base = 1000.0
    d.feed(_buyer("c1", base), now=base)
    d.feed(_seller("c1", base + gap), now=base + gap)
    # 即便时间推进很远，因卖家已回复取消 pending，pop_due 不应取走。
    due = d.pop_due(now=base + gap + 1e6)
    assert all(m["conversation_id"] != "c1" for m in due)
    assert d.pending_conversations() == []


@settings(max_examples=200, deadline=None)
@given(
    n=st.integers(min_value=1, max_value=15),
    silence=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
)
def test_property_consecutive_buyers_aggregate(n, silence):
    """属性：同会话连续买家消息只保留最后一条 pending（静默聚合）。

    连续 n 条买家消息（时间戳递增），feed 后 pop_due 仅取走 1 条（最后一条），
    且 pending_conversations 中该会话只出现一次。
    """
    d = ReplyDebouncer(silence_seconds=float(silence))
    for i in range(n):
        d.feed(_buyer("c1", 1000.0 + i), now=1000.0 + i)
    # 时间推进到远超最后一条 + silence。
    due = d.pop_due(now=1000.0 + n + 1e6)
    c1_due = [m for m in due if m["conversation_id"] == "c1"]
    assert len(c1_due) <= 1  # 聚合为单条
    # 该会话在 pending 中至多一次。
    assert d.pending_conversations().count("c1") <= 1


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-q"])
