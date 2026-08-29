# -*- coding: utf-8 -*-
"""
common.tests.test_latency —— 首响时长纯函数单元测试
==================================================
覆盖 compute_cycles / first_response_stats / latency_distribution / reply_rate
的核心口径：
- 单周期：买家消息 → 本店回复，首响时长正确；
- 多买家消息静默聚合（起点不变、last_in 更新，与 ReplyDebouncer 语义一致）；
- 无周期时的本店回复忽略（人工发送/窗口截断不参与统计）；
- 窗口结束仍有未回复买家消息 → pending；
- 同会话多个周期正确切分；
- >300 秒超时判定与统计指标（mean/p50/p90/max/占比）；
- 分布分桶（左闭右开、末桶无上界即超 5 分钟）与回复率（超时+待回复均计未达标）。

历史：本文件随模块上移由 tools/tests 迁至 common/tests（TIK-025）。
"""
from datetime import datetime

import pytest

from common.utils.latency import (
    DEFAULT_BUCKETS,
    LatencyBucket,
    compute_cycles,
    first_response_stats,
    latency_distribution,
    reply_rate,
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


# ----------------------------------------------------------------------
# 分布分桶（TIK-025）
# ----------------------------------------------------------------------
def _cycle_with_latency(seconds: float):
    """构造一个首响时长为指定秒数的周期（起点 10:00:00）。"""
    cycles, _ = compute_cycles(
        [("in", _dt(10, 0, 0)), ("out", _dt(10, 0, 0) + _sec(seconds))]
    )
    return cycles[0]


def _sec(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def test_distribution_empty_cycles():
    """无已回复周期时每桶计数与占比均为 0（不除零）。"""
    buckets = latency_distribution([])
    assert len(buckets) == len(DEFAULT_BUCKETS)
    assert all(b["count"] == 0 and b["ratio"] == 0.0 for b in buckets)


def test_distribution_buckets_are_left_closed_right_open():
    """分桶左闭右开：恰为边界值的时长落入下一桶。"""
    cycles = [
        _cycle_with_latency(0.0),    # 0-30s
        _cycle_with_latency(29.9),   # 0-30s
        _cycle_with_latency(30.0),   # 30-60s（边界归右桶）
        _cycle_with_latency(60.0),   # 1-3min
        _cycle_with_latency(180.0),  # 3-5min
        _cycle_with_latency(300.0),  # 超 5 分钟（=阈值即超时）
    ]
    buckets = latency_distribution(cycles)
    counts = [b["count"] for b in buckets]
    assert counts == [2, 1, 1, 1, 1]
    # 占比按已回复周期数归一（共 6 个，各桶 ratio 保留 4 位小数故用近似比较）
    assert sum(b["ratio"] for b in buckets) == pytest.approx(1.0, abs=1e-3)


def test_distribution_last_bucket_matches_over_threshold():
    """末桶（超 5 分钟）计数应等于 first_response_stats 的超时计数。"""
    cycles = [
        _cycle_with_latency(10.0),
        _cycle_with_latency(120.0),
        _cycle_with_latency(600.0),
    ]
    buckets = latency_distribution(cycles)
    stats = first_response_stats(cycles, [])
    assert buckets[-1]["count"] == stats["over_threshold_count"] == 1
    assert buckets[-1]["ratio"] == stats["over_threshold_ratio"]


def test_distribution_custom_buckets():
    """自定义桶定义可覆盖默认分桶（便于按平台/阈值调整）。"""
    custom = (
        LatencyBucket("1 分钟内", 0.0, 60.0),
        LatencyBucket("1 分钟以上", 60.0, None),
    )
    cycles = [_cycle_with_latency(10.0), _cycle_with_latency(90.0)]
    buckets = latency_distribution(cycles, custom)
    assert [b["label"] for b in buckets] == ["1 分钟内", "1 分钟以上"]
    assert [b["count"] for b in buckets] == [1, 1]
    assert buckets[-1]["upper"] is None


# ----------------------------------------------------------------------
# 回复率（TIK-025，供 TIK-026 告警复用）
# ----------------------------------------------------------------------
def test_reply_rate_all_qualified():
    """全部已回复且未超时 → 回复率 1.0。"""
    assert reply_rate(10, 0, 0) == 1.0


def test_reply_rate_pending_counts_as_failed():
    """待回复计未达标（计入分母）：10 个已回复 + 2 个待回复 → 10/12 ≈ 0.8333。"""
    assert reply_rate(10, 0, 2) == 0.8333


def test_reply_rate_over_threshold_counts_as_failed():
    """超时计未达标：10 个已回复中 3 个超时 → 0.7。"""
    assert reply_rate(10, 3, 0) == 0.7


def test_reply_rate_combined_failures():
    """超时与待回复同时计入未达标：(10-3)/(10+2) ≈ 0.5833。"""
    assert reply_rate(10, 3, 2) == 0.5833


def test_reply_rate_no_data_returns_none():
    """无任何周期（已回复 0 + 待回复 0）→ None，不判 0 也不判 1。"""
    assert reply_rate(0, 0, 0) is None
