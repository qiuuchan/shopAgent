# -*- coding: utf-8 -*-
"""
common.utils.business_hours —— 营业时间判定纯函数（无 I/O）
============================================================
本文件用途：为「营业时间判定」提供纯函数实现，供 websocket 引擎
（``websocket.engine.business_hours``）与 scheduler 定时任务（``run_tiktok_window``）
共用，避免跨服务重复实现或 scheduler 反向依赖 websocket 包（规范 52：公共逻辑
收敛至 common 库；scheduler 容器镜像不含 websocket 代码，不得 import 它）。

函数 ``is_within_business_hours`` 判定规则（对应需求 11.2 / 11.3 / 11.4）：
- 时间口径统一为北京时间，复用 ``common.utils.time_utils.now_beijing``；
- 支持跨午夜区间（如 22:00~06:00）：当结束时刻早于开始时刻时，营业区间
  跨越自然日零点，判定逻辑相应翻转；
- 未配置（起或止时刻缺失）默认全天营业，返回 True；
- 起止时刻相等视为「全天营业」（区间覆盖 24 小时），返回 True；
- 星期维度（``weekdays``）：为逗号分隔的星期序号（1=周一 … 7=周日），
  如 ``"1,2,3,4,5"`` 表示周一至周五；为空 / None 表示「每天」（兼容旧数据）；
- 入参时刻兼容多种形式：datetime.time 对象、"HH:MM" 或 "HH:MM:SS" 字符串，
  空字符串 / None 视为未配置。

本模块为纯函数、无 I/O，便于单元与属性测试。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、全中文（50）、
单文件 ≤500 行（35）、日志禁用 debug（38）。
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Optional, Union

from common.utils.time_utils import BEIJING_TZ, now_beijing
from common.utils.weekdays import is_weekday_matched, parse_weekdays

# 时刻入参允许的类型：time 对象、字符串（HH:MM / HH:MM:SS），或 None（未配置）
TimeLike = Union[time, str, None]


def _parse_time(value: TimeLike) -> Optional[time]:
    """将时刻入参解析为 ``datetime.time``，未配置时返回 ``None``。

    兼容以下输入形式：
    - ``datetime.time`` 对象：原样返回；
    - ``"HH:MM"`` 或 ``"HH:MM:SS"`` 字符串：解析为 ``time``（忽略两端空白）；
    - ``None`` 或空字符串：视为未配置，返回 ``None``。

    Args:
        value: 待解析的时刻入参。

    Returns:
        解析得到的 ``datetime.time``；当入参表示未配置时返回 ``None``。

    Raises:
        ValueError: 当字符串格式非法（无法解析为合法时刻）时抛出。
    """
    # time 对象直接返回
    if isinstance(value, time):
        return value
    # None 视为未配置
    if value is None:
        return None
    # 字符串：去除两端空白后判断是否为空
    text = str(value).strip()
    if not text:
        return None
    # 依次尝试 HH:MM:SS 与 HH:MM 两种格式
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    # 两种格式均无法解析 → 非法时刻
    raise ValueError(f"非法的营业时间时刻：{value!r}")


def is_within_business_hours(
    start_time: TimeLike,
    end_time: TimeLike,
    *,
    enabled: bool = True,
    weekdays: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """判断给定时刻（默认当前北京时间）是否处于营业时间区间内。

    判定规则（对应需求 11.2 / 11.3 / 11.4）：
    - ``enabled=False``（营业时间控制关闭）：默认全天营业，返回 ``True``；
    - 起或止时刻任一未配置：默认全天营业，返回 ``True``（需求 11.4）；
    - 起止时刻相等：视为覆盖 24 小时的全天营业，返回 ``True``；
    - 普通区间（start < end，如 08:00~22:00）：当 start ≤ 当前 < end 时为营业中；
    - 跨午夜区间（start > end，如 22:00~06:00）：当 当前 ≥ start 或 当前 < end
      时为营业中；
    - 星期维度（``weekdays``）：为逗号分隔的星期序号（1=周一 … 7=周日），
      如 ``"1,2,3,4,5"`` 表示周一至周五；为空 / None 表示「每天」（兼容旧数据，
      行为零变更）。当前星期不在 ``weekdays`` 范围内时视为非营业，返回 ``False``。

    Args:
        start_time: 营业开始时刻（time / "HH:MM" / "HH:MM:SS" / None）。
        end_time: 营业结束时刻（同上，可早于开始时刻表示跨午夜）。
        enabled: 营业时间控制是否启用；为 ``False`` 时默认全天营业。
        weekdays: 营业星期维度表达式（"1,2,3,4,5"），空 / None 表示每天。
        now: 用于判定的参考时刻；默认取当前北京时间。可传入用于测试。

    Returns:
        处于营业时间区间内返回 ``True``，否则返回 ``False``。
    """
    # 营业时间控制关闭：默认全天营业
    if not enabled:
        return True

    # 确定参考时刻：默认当前北京时间；带时区的入参先归一到北京时间
    reference = now if now is not None else now_beijing()
    if reference.tzinfo is not None:
        reference = reference.astimezone(BEIJING_TZ)

    # 星期维度：当前星期未命中配置的星期集合 → 非营业（需求 11.x 扩展）。
    # weekdays 空 / None = 每天，行为与旧实现一致。
    if not is_weekday_matched(reference.isoweekday(), parse_weekdays(weekdays)):
        return False

    # 解析起止时刻
    start = _parse_time(start_time)
    end = _parse_time(end_time)

    # 未配置（任一时刻缺失）：默认全天营业（需求 11.4）
    if start is None or end is None:
        return True

    current = reference.time()

    # 起止相等：视为全天营业（覆盖 24 小时）
    if start == end:
        return True

    # 普通区间：start < end，营业时段不跨越零点
    if start < end:
        return start <= current < end

    # 跨午夜区间：start > end，营业时段跨越零点（如 22:00~06:00）
    return current >= start or current < end


__all__ = [
    "TimeLike",
    "is_within_business_hours",
]
