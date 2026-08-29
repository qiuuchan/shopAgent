# -*- coding: utf-8 -*-
"""
websocket.tests.test_risk_control —— 风控频率限制判断单元测试
==============================================================
本文件用途：验证 websocket.engine.risk_control 的频率限制纯逻辑，覆盖需求 13.2
与 Property 18（风控频率限制）：

- 统计窗口内的回复计数（``count_within_window``）：窗口边界、未来时刻、窗口
  未配置（None / ≤0）时的口径；
- 单维度上限判定（``is_limit_exceeded``）：上限为空不限制、达上限语义为
  「次数 ≥ 上限」；
- 综合判定（``check_reply_frequency``）：先单会话后单店铺的优先级、风控未启用
  恒放行、未配置任何上限恒放行（对应「未配风控规则的店铺行为不变」）、达上限
  时生成风控日志（风控类型 ``frequency_limit``，需求 13.4）。

工单背景：TIK-020「频率断路器配置化」。风控规则经 backend RiskRule 配置
（session_reply_limit / shop_reply_limit / window_seconds / enabled），运行时
由 reply_engine 决策链第 4 级调用本模块判定，本文件为其纯逻辑基线。

测试框架：pytest。
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.risk_control import (
    RISK_TYPE_FREQUENCY_LIMIT,
    SCOPE_SESSION,
    SCOPE_SHOP,
    check_reply_frequency,
    count_within_window,
    is_limit_exceeded,
)

# 固定参考时刻（北京时间口径，朴素 datetime），使窗口判定结果可控。
NOW = datetime(2024, 1, 1, 12, 0, 0)


# ----------------------------------------------------------------------
# 构造辅助
# ----------------------------------------------------------------------
def _ago(seconds: float) -> datetime:
    """返回相对固定参考时刻 NOW 之前 seconds 秒的时刻。"""
    return NOW - timedelta(seconds=seconds)


# ----------------------------------------------------------------------
# count_within_window：窗口内回复计数
# ----------------------------------------------------------------------
def test_count_within_window_empty():
    """无回复记录（None / 空序列）：计数为 0。"""
    assert count_within_window(None, 60, now=NOW) == 0
    assert count_within_window([], 60, now=NOW) == 0


def test_count_within_window_counts_inside():
    """窗口内的回复全部计入（含恰好等于参考时刻）。"""
    times = [_ago(1), _ago(30), NOW]
    assert count_within_window(times, 60, now=NOW) == 3


def test_count_within_window_excludes_outdated():
    """早于窗口起点的回复不计入（滑出窗口即失效）。

    窗口为 (now-60, now]：恰好落在窗口起点的时刻不计入（严格大于起点）。
    """
    times = [_ago(60), _ago(61), _ago(10)]
    assert count_within_window(times, 60, now=NOW) == 1


def test_count_within_window_excludes_future():
    """晚于参考时刻的「未来」回复不计入，防止异常数据放大计数。"""
    times = [_ago(10), NOW + timedelta(seconds=5)]
    assert count_within_window(times, 60, now=NOW) == 1


def test_count_within_window_without_window():
    """窗口未配置（None / 0 / 负数）：不按时间截断，统计全部不晚于 now 的回复。"""
    times = [_ago(3600), _ago(10)]
    for window in (None, 0, -1):
        assert count_within_window(times, window, now=NOW) == 2


def test_count_within_window_accepts_aware_datetime():
    """带时区的回复时刻：统一换算到北京时间后比较，口径一致。

    UTC 03:59:30 即北京时间 11:59:30，落在 (11:59:00, 12:00:00] 窗口内。
    """
    aware_utc = datetime(2024, 1, 1, 3, 59, 30, tzinfo=ZoneInfo("UTC"))
    assert count_within_window([aware_utc], 60, now=NOW) == 1


# ----------------------------------------------------------------------
# is_limit_exceeded：单维度上限判定
# ----------------------------------------------------------------------
def test_is_limit_exceeded_none_limit():
    """上限为空：该维度不限制，恒不达上限。"""
    assert is_limit_exceeded(100, None) is False


def test_is_limit_exceeded_reach_and_below():
    """达上限语义为「次数 ≥ 上限」：等于上限即达上限，小于则未达。"""
    assert is_limit_exceeded(3, 3) is True
    assert is_limit_exceeded(4, 3) is True
    assert is_limit_exceeded(2, 3) is False


def test_is_limit_exceeded_zero_limit_blocks_all():
    """上限为 0：该维度完全禁止回复（0 次即达上限）。"""
    assert is_limit_exceeded(0, 0) is True


# ----------------------------------------------------------------------
# check_reply_frequency：综合判定（需求 13.2 / Property 18）
# ----------------------------------------------------------------------
def test_check_disabled_always_pass():
    """风控未启用（enabled=False）：恒放行，不生成风控日志。"""
    result = check_reply_frequency(
        1,
        session_reply_times=[_ago(1)] * 5,
        shop_reply_times=[_ago(1)] * 5,
        session_reply_limit=2,
        shop_reply_limit=2,
        window_seconds=60,
        enabled=False,
        now=NOW,
    )
    assert result.blocked is False
    assert result.risk_log is None


def test_check_no_limit_configured_always_pass():
    """未配置任何上限（两维度均为 None）：恒放行（未配规则店铺行为不变）。"""
    result = check_reply_frequency(
        1,
        session_reply_times=[_ago(1)] * 99,
        shop_reply_times=[_ago(1)] * 99,
        window_seconds=60,
        enabled=True,
        now=NOW,
    )
    assert result.blocked is False
    assert result.risk_log is None


def test_check_session_limit_blocked():
    """单会话达上限：暂停回复，维度为 session，并生成风控日志。"""
    result = check_reply_frequency(
        1,
        session_reply_times=[_ago(30), _ago(20), _ago(10)],
        shop_reply_times=[_ago(10)],
        session_reply_limit=3,
        shop_reply_limit=10,
        window_seconds=60,
        enabled=True,
        now=NOW,
    )
    assert result.blocked is True
    assert result.scope == SCOPE_SESSION
    assert result.reply_count == 3
    assert result.limit == 3
    assert result.risk_log is not None
    assert result.risk_log.risk_type == RISK_TYPE_FREQUENCY_LIMIT
    assert "单会话" in (result.risk_log.trigger_reason or "")


def test_check_shop_limit_blocked():
    """单店铺达上限（会话未达）：暂停回复，维度为 shop。"""
    result = check_reply_frequency(
        7,
        session_reply_times=[_ago(10)],
        shop_reply_times=[_ago(40), _ago(30), _ago(20), _ago(10)],
        session_reply_limit=3,
        shop_reply_limit=4,
        window_seconds=60,
        enabled=True,
        now=NOW,
    )
    assert result.blocked is True
    assert result.scope == SCOPE_SHOP
    assert result.reply_count == 4
    assert result.risk_log.shop_pk == 7
    assert "单店铺" in (result.risk_log.trigger_reason or "")


def test_check_session_takes_priority_over_shop():
    """两维度同时达上限：按「先会话后店铺」顺序，只上报会话维度一条日志。"""
    result = check_reply_frequency(
        1,
        session_reply_times=[_ago(10), _ago(5)],
        shop_reply_times=[_ago(10), _ago(5)],
        session_reply_limit=2,
        shop_reply_limit=2,
        window_seconds=60,
        enabled=True,
        now=NOW,
    )
    assert result.blocked is True
    assert result.scope == SCOPE_SESSION


def test_check_below_limit_passes():
    """两维度均未达上限：放行且不生成风控日志。"""
    result = check_reply_frequency(
        1,
        session_reply_times=[_ago(10)],
        shop_reply_times=[_ago(10), _ago(5)],
        session_reply_limit=3,
        shop_reply_limit=5,
        window_seconds=60,
        enabled=True,
        now=NOW,
    )
    assert result.blocked is False
    assert result.risk_log is None


def test_check_recovers_after_window_slides():
    """窗口滑出后计数归零：超时的旧回复不再计入，可继续回复。"""
    # 1 小时窗口、上限 20（TIK-020 测试店固化配置口径）：窗口内的 20 次已用满
    full = [_ago(120)] * 20
    blocked = check_reply_frequency(
        1,
        shop_reply_times=full,
        shop_reply_limit=20,
        window_seconds=3600,
        enabled=True,
        now=NOW,
    )
    assert blocked.blocked is True

    # 参考时刻前进 1 小时以上，旧回复滑出窗口 → 恢复放行
    later = NOW + timedelta(seconds=3601)
    passed = check_reply_frequency(
        1,
        shop_reply_times=full,
        shop_reply_limit=20,
        window_seconds=3600,
        enabled=True,
        now=later,
    )
    assert passed.blocked is False


def test_check_risk_log_as_dict_fields():
    """风控日志数据可序列化落库：字段齐全且时间为北京时间 ISO 串。"""
    result = check_reply_frequency(
        1,
        shop_reply_times=[_ago(10)],
        shop_reply_limit=1,
        window_seconds=3600,
        enabled=True,
        now=NOW,
    )
    data = result.risk_log.as_dict()
    assert data["shop_pk"] == 1
    assert data["risk_type"] == RISK_TYPE_FREQUENCY_LIMIT
    assert "3600秒统计窗口内" in (data["trigger_reason"] or "")
    assert isinstance(data["log_time"], str)
    assert data["log_time"].startswith("2024-01-01T12:00:00")
