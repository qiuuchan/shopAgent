# -*- coding: utf-8 -*-
"""
tools.tests.test_latency —— 首响时长纯函数单元测试
==================================================
覆盖 compute_cycles / first_response_stats 的核心口径：
- 单周期：买家消息 → 本店回复，首响时长正确；
- 多买家消息静默聚合（起点不变、last_in 更新，与 ReplyDebouncer 语义一致）；
- 无周期时的本店回复忽略（人工发送/窗口截断不参与统计）；
- 窗口结束仍有未回复买家消息 → pending；
- 同会话多个周期正确切分；
- >300 秒超时判定与统计指标（mean/p50/p90/max/占比）。
"""
from datetime import datetime

from tools.tiktok_acceptance.latency import (
    compute_cycles,
    first_response_stats,
)


def _dt(h, m=0, s=0):
    return datetime(2026, 8, 30, h, m, s)


def test_single_cycle_latency():
    msgs = [("in", _dt(10, 0, 0)), ("out", _dt(10, 1, 30))]
    cycles, pending = compute_cycles(msgs)
    assert len(cycles) == 1
    assert len(pending) == 0
    assert cycles[0].latency_seconds == 90.0
    assert cycles[0].first_in == _dt(10, 0, 0)
    assert cycles[0].out == _dt(10, 1, 30)


def test_multiple_buyer_messages_aggregate():
    # 多条买家消息聚合成一次待回复：起点取第一条，last_in 更新
    msgs = [("in", _dt(10, 0, 0)), ("in", _dt(10, 0, 5)), ("out", _dt(10, 0, 50))]
    cycles, pending = compute_cycles(msgs)
    assert len(cycles) == 1
    assert cycles[0].first_in == _dt(10, 0, 0)
    assert cycles[0].last_in == _dt(10, 0, 5)
    assert cycles[0].latency_seconds == 50.0


def test_seller_reply_without_cycle_ignored():
    # 窗口内只有本店回复（无买家消息）：不产生周期、不参与统计
    msgs = [("out", _dt(9, 0, 0)), ("out", _dt(9, 1, 0))]
    cycles, pending = compute_cycles(msgs)
    assert cycles == []
    assert pending == []


def test_pending_when_window_ends_open():
    msgs = [("in", _dt(11, 0, 0)), ("in", _dt(11, 0, 10))]
    cycles, pending = compute_cycles(msgs)
    assert cycles == []
    assert len(pending) == 1
    assert pending[0].first_in == _dt(11, 0, 0)
    assert pending[0].last_in == _dt(11, 0, 10)


def test_multiple_cycles_in_one_conversation():
    msgs = [
        ("in", _dt(10, 0, 0)),
        ("out", _dt(10, 1, 0)),
        ("in", _dt(10, 2, 0)),
        ("in", _dt(10, 2, 5)),
        ("out", _dt(10, 3, 0)),
        ("in", _dt(10, 4, 0)),  # 结尾未回复 → pending
    ]
    cycles, pending = compute_cycles(msgs)
    assert len(cycles) == 2
    assert [c.latency_seconds for c in cycles] == [60.0, 60.0]
    assert len(pending) == 1
    assert pending[0].first_in == _dt(10, 4, 0)


def test_unknown_direction_ignored():
    msgs = [("in", _dt(10, 0, 0)), ("system", _dt(10, 0, 1)), ("out", _dt(10, 1, 0))]
    cycles, pending = compute_cycles(msgs)
    assert len(cycles) == 1
    assert cycles[0].latency_seconds == 60.0


def test_empty_input():
    cycles, pending = compute_cycles([])
    assert cycles == []
    assert pending == []


def test_stats_empty():
    stats = first_response_stats([], [])
    assert stats["responded_cycles"] == 0
    assert stats["mean_seconds"] is None
    assert stats["p50_seconds"] is None
    assert stats["over_threshold_count"] == 0
    assert stats["over_threshold_ratio"] is None


def test_stats_percentiles_and_over_threshold():
    cycles, _ = compute_cycles(
        [m for i in range(4) for m in (("in", _dt(10, i, 0)), ("out", _dt(10, i, 2)))]
    )
    # 四个周期时长均为 2s，无超时
    stats = first_response_stats(cycles, [])
    assert stats["responded_cycles"] == 4
    assert stats["mean_seconds"] == 2.0
    assert stats["p50_seconds"] == 2.0
    assert stats["p90_seconds"] == 2.0
    assert stats["max_seconds"] == 2.0
    assert stats["over_threshold_count"] == 0


def test_stats_over_threshold_detection():
    cycles, _ = compute_cycles(
        [("in", _dt(10, 0, 0)), ("out", _dt(10, 6, 0))]  # 360s > 300s
    )
    stats = first_response_stats(cycles, [])
    assert stats["over_threshold_count"] == 1
    assert stats["over_threshold_ratio"] == 1.0
    assert stats["max_seconds"] == 360.0


def test_stats_pending_conversations():
    cycles, pending = compute_cycles([("in", _dt(10, 0, 0))])
    stats = first_response_stats(cycles, pending, pending_conversations=3)
    assert stats["pending_cycles"] == 1
    assert stats["pending_conversations"] == 3
