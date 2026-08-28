# -*- coding: utf-8 -*-
"""
tools.tiktok_acceptance.latency —— 买家首响时长统计纯函数
========================================================
本模块用途：为 TIK-018 端到端验收（验收标准 1「买家新消息 5 分钟内收到模板首响」）
提供首响时长的纯逻辑计算，不依赖数据库与 common 包，便于单元测试。

口径说明（对齐 ReplyDebouncer 的静默聚合语义）：
- 一个「回复周期（cycle）」= 一段买家连续消息 + 其后本店首次回复；
- 周期起点 = 该段第一条买家消息（direction='in'）的 msg_time；
- 周期内后续买家消息只更新 last_in，不重置起点（多条买家消息聚合为一次待回复）；
- 周期终点 = 该段之后第一条本店回复（direction='out'）的 msg_time；
- 首响时长 = 终点 - 起点（秒）；>300 秒判定为超时（5 分钟首响标准）；
- 窗口结束时仍处于「起点后无回复」状态的周期 = 待回复（pending）；
- 无周期时的本店回复（窗口截断 / 人工发送 / 历史回填）不参与统计。

输入约定：messages 为 (direction, msg_time) 序列，按时间升序传入
（调用方按 customer_uid 分组后逐会话处理）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Iterable, List, Optional, Tuple

# 5 分钟首响标准（秒），与验收标准 1 对齐
FIRST_RESPONSE_THRESHOLD_SECONDS: float = 300.0

DIRECTION_IN = "in"    # 收（买家 → 本店）
DIRECTION_OUT = "out"  # 发（本店 → 买家）


@dataclass(frozen=True)
class Cycle:
    """一个已回复的周期：买家消息段 + 其后本店首次回复。"""

    first_in: datetime   # 周期内第一条买家消息时间（起点）
    last_in: datetime    # 周期内最后一条买家消息时间
    out: datetime        # 周期内本店首次回复时间（终点）

    @property
    def latency_seconds(self) -> float:
        """首响时长（秒）= 终点 - 起点。"""
        return (self.out - self.first_in).total_seconds()


@dataclass(frozen=True)
class PendingCycle:
    """一个待回复周期：窗口结束时仍有买家消息未获回复。"""

    first_in: datetime   # 待回复段第一条买家消息时间
    last_in: datetime    # 待回复段最后一条买家消息时间


def compute_cycles(
    messages: Iterable[Tuple[str, datetime]],
) -> Tuple[List[Cycle], List[PendingCycle]]:
    """把单个会话的已排序消息切分为回复周期与待回复周期。

    参数:
        messages: (direction, msg_time) 序列，须已按 msg_time 升序（含同刻按 id）。
    返回:
        (cycles, pending)：已回复周期列表、待回复周期列表。
    """
    cycles: List[Cycle] = []
    pending: List[PendingCycle] = []
    open_first: Optional[datetime] = None
    open_last: Optional[datetime] = None

    for direction, ts in messages:
        if direction == DIRECTION_IN:
            if open_first is None:
                open_first = open_last = ts
            else:
                open_last = ts
        elif direction == DIRECTION_OUT:
            if open_first is not None:
                cycles.append(Cycle(open_first, open_last or open_first, ts))
                open_first = open_last = None
            # 无待回复周期时的本店回复：忽略（不参与统计）

    if open_first is not None:
        pending.append(PendingCycle(open_first, open_last or open_first))
    return cycles, pending


def _percentile(values: List[float], percent: float) -> Optional[float]:
    """最近秩分位数：排序后取 ceil(p/100 * n) 位，空列表返回 None。"""
    n = len(values)
    if n == 0:
        return None
    sorted_values = sorted(values)
    index = ceil(percent / 100.0 * n) - 1
    return sorted_values[index]


def first_response_stats(
    cycles: List[Cycle],
    pending_cycles: List[PendingCycle],
    pending_conversations: int = 0,
    threshold_seconds: float = FIRST_RESPONSE_THRESHOLD_SECONDS,
) -> dict:
    """汇总首响时长统计指标。

    参数:
        cycles: 已回复周期列表。
        pending_cycles: 待回复周期列表。
        pending_conversations: 待回复会话数（调用方跨会话聚合后传入）。
        threshold_seconds: 超时阈值（秒），默认 300（5 分钟首响标准）。
    返回:
        统计字典，字段含 responded_cycles / pending_cycles / pending_conversations /
        mean_seconds / p50_seconds / p90_seconds / max_seconds /
        over_threshold_count / over_threshold_ratio / threshold_seconds。
    """
    latencies = [c.latency_seconds for c in cycles]
    over_count = sum(1 for v in latencies if v > threshold_seconds)
    return {
        "responded_cycles": len(cycles),
        "pending_cycles": len(pending_cycles),
        "pending_conversations": pending_conversations,
        "mean_seconds": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "p50_seconds": _percentile(latencies, 50),
        "p90_seconds": _percentile(latencies, 90),
        "max_seconds": max(latencies) if latencies else None,
        "over_threshold_count": over_count,
        "over_threshold_ratio": round(over_count / len(latencies), 4) if latencies else None,
        "threshold_seconds": threshold_seconds,
    }


__all__ = [
    "Cycle",
    "PendingCycle",
    "compute_cycles",
    "first_response_stats",
    "FIRST_RESPONSE_THRESHOLD_SECONDS",
    "DIRECTION_IN",
    "DIRECTION_OUT",
]
