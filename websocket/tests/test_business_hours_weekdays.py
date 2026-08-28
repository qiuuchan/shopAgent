# -*- coding: utf-8 -*-
"""
websocket.tests.test_business_hours_weekdays —— 营业时间星期维度测试
================================================================
本文件用途：验证 websocket.engine.business_hours 在「星期维度」扩展下的判定正确性，
对应需求 11.x（营业时间支持按星期维度，如「周一至周五 9:00-18:00」）。

覆盖三种关键场景（交接单硬约束）：
- 每天：weekdays 为空 / None（兼容旧数据），行为与旧实现一致，星期不影响判定；
- 指定星期：weekdays 如 "1,2,3,4,5"（周一至周五），当前星期命中才进入时刻判定，
  否则即使时刻在区间内也判定为非营业；
- 空串兼容：weekdays="" 与 None 等价（表示每天），旧数据零迁移负担。

同时覆盖 common.utils.weekdays 的纯函数解析 / 校验 / 匹配逻辑（属性 + 单测）。

测试框架：pytest + Hypothesis（最少 100 次迭代，本文件常用 200）。
"""
from datetime import datetime, time

from hypothesis import given, settings
from hypothesis import strategies as st

from common.utils.time_utils import BEIJING_TZ
from common.utils.weekdays import (
    is_weekday_matched,
    normalize_weekdays,
    parse_weekdays,
    validate_weekdays,
)
from engine.business_hours import is_within_business_hours

# 星期序号策略：1（周一）~ 7（周日）。
_weekday_strategy = st.integers(min_value=1, max_value=7)
# 星期集合策略：从 1~7 中无放回抽样若干（0~7 个），模拟 "1,2,3,4,5" 这类表达式。
_weekdays_set_strategy = st.lists(_weekday_strategy, min_size=0, max_size=7, unique=True)


def _fmt_weekdays(nums) -> str:
    """将星期序号列表格式化为逗号分隔表达式（升序）。"""
    return ",".join(str(n) for n in sorted(nums))


# ----------------------------------------------------------------------
# common.utils.weekdays 纯函数测试
# ----------------------------------------------------------------------
def test_parse_weekdays_empty_means_everyday():
    """空串 / None 表示「每天」，返回空集。"""
    assert parse_weekdays("") == set()
    assert parse_weekdays(None) == set()
    assert parse_weekdays("   ") == set()


def test_parse_weekdays_normal():
    """正常表达式解析为升序去重集合，空白被忽略。"""
    assert parse_weekdays("1,2,3,4,5") == {1, 2, 3, 4, 5}
    assert parse_weekdays(" 5 , 6 , 7 ") == {5, 6, 7}
    assert parse_weekdays("7,1,3") == {1, 3, 7}  # 顺序无关、去重
    assert parse_weekdays("1") == {1}


def test_parse_weekdays_invalid():
    """非法表达式抛 ValueError：非整数段 / 越界序号 / 空段。"""
    import pytest

    for bad in ("a,b", "0", "8", "1,8", "1,,2", "1.5", ""):
        # 空串是合法的（=每天），跳过
        if bad == "":
            continue
        with pytest.raises(ValueError):
            parse_weekdays(bad)


def test_is_weekday_matched_everyday():
    """空集 / None 表示每天，任何星期均命中。"""
    assert is_weekday_matched(1, set()) is True
    assert is_weekday_matched(7, None) is True
    assert is_weekday_matched(3, []) is True


def test_is_weekday_matched_specific():
    """指定星期集合，仅命中集合内的星期。"""
    weekdays = {1, 2, 3, 4, 5}
    assert is_weekday_matched(1, weekdays) is True
    assert is_weekday_matched(5, weekdays) is True
    assert is_weekday_matched(6, weekdays) is False
    assert is_weekday_matched(7, weekdays) is False


def test_validate_weekdays_passes_on_valid():
    """合法表达式（含空 / None）不抛异常。"""
    validate_weekdays("")
    validate_weekdays(None)
    validate_weekdays("1,2,3,4,5")
    validate_weekdays("6,7")


def test_normalize_weekdays_empty():
    """归一化：None / 空串归一成空串（与旧数据兼容存储形态）。"""
    assert normalize_weekdays(None) == ""
    assert normalize_weekdays("") == ""
    assert normalize_weekdays(" 1,2,3 ") == "1,2,3"


@settings(max_examples=200)
@given(_weekdays_set_strategy)
def test_parse_weekdays_roundtrip(nums):
    """属性：解析出的集合与输入一致（升序去重），且全部落在 1~7。"""
    expr = _fmt_weekdays(nums)
    parsed = parse_weekdays(expr)
    assert parsed == set(nums)
    assert all(1 <= n <= 7 for n in parsed)


@settings(max_examples=200)
@given(_weekday_strategy, _weekdays_set_strategy)
def test_is_weekday_matched_property(weekday, nums):
    """属性：星期命中当且仅当该序号出现在集合中（空集=每天恒命中）。"""
    expected = (not nums) or (weekday in set(nums))
    assert is_weekday_matched(weekday, nums) is expected


# ----------------------------------------------------------------------
# engine.business_hours 星期维度判定测试
# ----------------------------------------------------------------------
def _beijing(weekday: int, at: time) -> datetime:
    """构造一个指定星期与时刻的北京时区 datetime（用于测试参考时刻）。

    Args:
        weekday: 1（周一）~ 7（周日）。
        at: 当日时刻。

    Returns:
        带北京时区的 datetime。
    """
    # 2024-01-01 是周一；据此偏移得到目标星期对应的某天。
    base = datetime(2024, 1, 1, at.hour, at.minute, at.second)
    from datetime import timedelta

    shift = (weekday - 1) % 7
    target = base + timedelta(days=shift)
    return target.replace(tzinfo=BEIJING_TZ)


def test_weekdays_empty_equals_everyday():
    """空串 weekdays：星期不影响判定，与旧实现行为一致（每天）。"""
    # 周一与周日，时刻在 09:00-18:00 内，均应判定为营业。
    mon = _beijing(1, time(10, 0))
    sun = _beijing(7, time(10, 0))
    assert is_within_business_hours("09:00", "18:00", now=mon, weekdays="") is True
    assert is_within_business_hours("09:00", "18:00", now=mon, weekdays=None) is True
    assert is_within_business_hours("09:00", "18:00", now=sun, weekdays="") is True


def test_weekdays_specified_match():
    """指定周一至周五：周一（命中）时刻在区内 -> 营业。"""
    mon = _beijing(1, time(10, 0))
    assert is_within_business_hours("09:00", "18:00", weekdays="1,2,3,4,5", now=mon) is True


def test_weekdays_specified_no_match():
    """指定周一至周五：周日（未命中）即使时刻在区内 -> 非营业。"""
    sun = _beijing(7, time(10, 0))
    assert (
        is_within_business_hours("09:00", "18:00", weekdays="1,2,3,4,5", now=sun)
        is False
    )


def test_weekdays_no_match_outside_hours():
    """指定周一至周五：周一但时刻在区外 -> 非营业（星期命中但时刻不符）。"""
    mon_early = _beijing(1, time(7, 0))
    assert (
        is_within_business_hours("09:00", "18:00", weekdays="1,2,3,4,5", now=mon_early)
        is False
    )


def test_weekdays_saturday_only():
    """仅周六营业：周六命中且在区内 -> 营业；周一未命中 -> 非营业。"""
    sat = _beijing(6, time(12, 0))
    mon = _beijing(1, time(12, 0))
    assert is_within_business_hours("09:00", "18:00", weekdays="6", now=sat) is True
    assert is_within_business_hours("09:00", "18:00", weekdays="6", now=mon) is False


def test_weekdays_disabled_bypass():
    """营业控制关闭（enabled=False）：即使星期/时刻不符仍视为营业。"""
    sun = _beijing(7, time(3, 0))
    assert (
        is_within_business_hours(
            "09:00", "18:00", enabled=False, weekdays="1,2,3,4,5", now=sun
        )
        is True
    )


def test_engine_reexports_common_implementation():
    """engine.business_hours 与 common.utils.business_hours 为同一实现（上移无行为差异）。"""
    from common.utils.business_hours import is_within_business_hours as common_within

    assert is_within_business_hours is common_within


@settings(max_examples=200)
@given(_weekdays_set_strategy, st.times(), st.times())
def test_weekdays_property(nums, h, m):
    """属性：对任一星期表达式与一个固定时刻，判定结果在星期阶段与参考实现一致。

    参考实现：先用 parse_weekdays 解析；若集合非空且参考星期不在集合内 -> False，
    否则落到时刻区间判定（本属性仅校验「星期阶段」的正确性，故固定取区间内时刻，
    使时刻阶段恒为 True，专注验证星期维度短路逻辑）。
    """
    nums = sorted(set(nums))
    expr = _fmt_weekdays(nums)
    # 固定时刻 12:00，区间 09:00~18:00，确保时刻阶段恒为营业中。
    for weekday in (1, 2, 3, 4, 5, 6, 7):
        now = _beijing(weekday, time(12, 0))
        got = is_within_business_hours("09:00", "18:00", weekdays=expr, now=now)
        expected = (not nums) or (weekday in set(nums))
        assert got is expected


__all__ = [
    "test_parse_weekdays_empty_means_everyday",
    "test_parse_weekdays_normal",
    "test_parse_weekdays_invalid",
    "test_is_weekday_matched_everyday",
    "test_is_weekday_matched_specific",
    "test_validate_weekdays_passes_on_valid",
    "test_normalize_weekdays_empty",
    "test_parse_weekdays_roundtrip",
    "test_is_weekday_matched_property",
    "test_weekdays_empty_equals_everyday",
    "test_weekdays_specified_match",
    "test_weekdays_specified_no_match",
    "test_weekdays_no_match_outside_hours",
    "test_weekdays_saturday_only",
    "test_weekdays_disabled_bypass",
    "test_engine_reexports_common_implementation",
    "test_weekdays_property",
]
