# -*- coding: utf-8 -*-
"""
common.utils.weekdays —— 星期表达式解析与校验（纯函数，无 I/O）
============================================================
本文件用途：为「营业时间按星期维度」提供星期表达式的解析与校验纯逻辑，
供 websocket 引擎的营业时间判定与 backend 的营业时间配置读写共用。

星期表达式约定（北京时间口径，周一为每周第一天）：
- 用逗号分隔的星期序号表示，范围为 1~7，其中 1=周一、2=周二、…、7=周日；
- 例如 ``"1,2,3,4,5"`` 表示「周一至周五」；``"6,7"`` 表示「周六、周日」；
- 单个值也可（如 ``"1"`` 表示仅周一）；允许数字前后有空白（解析时去除）；
- 空字符串 / None 表示「不限定星期」，即「每天」（兼容旧数据，旧数据零迁移负担）。

设计要点：
- 本模块为纯函数、无 I/O，便于单元与属性测试；
- ``parse_weekdays``：将表达式解析为升序去重的整数集合（1~7）；空/None 返回
  空集合（语义上表示「每天」）；非法表达式抛出 ``ValueError``；
- ``is_weekday_matched``：给定参考星期序号，判断其是否落在配置的星期集合内；
  集合为空（每天）时恒返回 True；
- ``validate_weekdays``：仅校验格式是否合法（非法抛 ``ValueError``），供 backend
  在写入前对入参做格式校验；合法但表示「每天」的表达式以空串形式归一存储。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、全中文（50）、
单文件 ≤500 行（35）、日志禁用 debug（38）。
"""
from __future__ import annotations

from typing import Iterable, Optional, Set


# 星期序号合法范围：1=周一 … 7=周日（北京时间口径，周一为每周第一天）。
_MIN_WEEKDAY: int = 1
_MAX_WEEKDAY: int = 7


def parse_weekdays(value: Optional[str]) -> Set[int]:
    """将星期表达式解析为升序去重的星期序号集合（1~7）。

    解析规则：
    - 空字符串 / None 视为「不限定星期 = 每天」，返回空集合（调用方据此视为每天）；
    - 以逗号分隔的若干段，每段去除空白后须为 ``1``~``7`` 的整数；
    - 重复序号自动去重；结果以升序集合返回，便于稳定比较。

    Args:
        value: 星期表达式（如 ``"1,2,3,4,5"``）或空 / None。

    Returns:
        星期序号集合（1~7 的整数）；空集表示「每天」。

    Raises:
        ValueError: 当表达式非空但格式非法（含非整数、越界序号或空段）时抛出。
    """
    # 空值 / 空串：语义为「每天」，返回空集。
    if value is None:
        return set()
    text = str(value).strip()
    if not text:
        return set()

    result: Set[int] = set()
    for raw_part in text.split(","):
        part = raw_part.strip()
        # 空段（如 "1,,2" 或尾随逗号）视为非法格式。
        if not part:
            raise ValueError(f"非法的星期表达式（存在空段）：{value!r}")
        try:
            number = int(part)
        except ValueError:
            raise ValueError(f"非法的星期表达式（非整数段）：{value!r}")
        if number < _MIN_WEEKDAY or number > _MAX_WEEKDAY:
            raise ValueError(
                f"非法的星期序号（应为 {_MIN_WEEKDAY}~{_MAX_WEEKDAY}）：{value!r}"
            )
        result.add(number)
    return result


def is_weekday_matched(weekday: int, weekdays: Optional[Iterable[int]]) -> bool:
    """判断给定星期序号是否命中配置的星期集合。

    语义：
    - ``weekdays`` 为空 / None（「每天」）时恒返回 True；
    - 否则判断 ``weekday`` 是否出现在集合中。

    Args:
        weekday: 待判定的星期序号（1=周一 … 7=周日），由调用方按北京时间计算。
        weekdays: 允许的星期序号集合 / 可迭代对象；空或 None 表示「每天」。

    Returns:
        命中（处于营业星期范围）返回 True，否则 False。
    """
    if weekdays is None:
        return True
    # 空集合表示「每天」。
    if not weekdays:
        return True
    return weekday in set(weekdays)


def validate_weekdays(value: Optional[str]) -> None:
    """校验星期表达式格式是否合法（非法抛 ``ValueError``）。

    用于 backend 在写入营业时间配置前对 ``weekdays`` 入参做格式校验；合法时
    不返回任何值。空串 / None 视为「每天」，合法。

    Args:
        value: 星期表达式（如 ``"1,2,3,4,5"``）或空 / None。

    Raises:
        ValueError: 当表达式格式非法时抛出（含非整数段 / 越界序号 / 空段）。
    """
    # 复用 parse_weekdays 完成解析即校验（解析失败会抛 ValueError）。
    parse_weekdays(value)


def normalize_weekdays(value: Optional[str]) -> str:
    """将星期表达式归一为存储形态（合法时返回原串；空 / None 返回空串）。

    存储约定：``weekdays`` 列空串表示「每天」，与旧数据兼容；非空时保留原串。
    写入前应先经 ``validate_weekdays`` 校验，避免脏数据入库。

    Args:
        value: 星期表达式或空 / None。

    Returns:
        归一后的存储字符串：空 / None → 空串；否则返回去除两端空白的原串。
    """
    if value is None:
        return ""
    return str(value).strip()


__all__ = [
    "parse_weekdays",
    "is_weekday_matched",
    "validate_weekdays",
    "normalize_weekdays",
]
