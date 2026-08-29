# -*- coding: utf-8 -*-
"""
common.utils.latency —— 买家首响时长统计纯函数
==============================================
本模块用途：为「首响时长」相关统计提供与数据库、框架无关的纯逻辑计算，供
websocket 侧对账工具（tools.tiktok_acceptance.reconcile）与 backend 统计接口
（app.services.first_response_service）共用同一口径，避免两处各算各的。

历史：本模块初建于 TIK-018 验收工具（tools/tiktok_acceptance/latency.py），
TIK-025 因 backend 统计接口需复用而**上移至 common**（backend 不应依赖 tools 包），
口径与实现保持不变，仅新增分布分桶与回复率两项汇总函数。

口径说明（对齐 ReplyDebouncer 的静默聚合语义）：
- 一个「回复周期（cycle）」= 一段买家连续消息 + 其后本店首次回复；
- 周期起点 = 该段第一条买家消息（direction='in'）的 msg_time；
- 周期内后续买家消息只更新 last_in，不重置起点（多条买家消息聚合为一次待回复）；
- 周期终点 = 该段之后第一条本店回复（direction='out'）的 msg_time；
- 首响时长 = 终点 - 起点（秒）；>300 秒判定为超时（5 分钟首响标准）；
- 窗口结束时仍处于「起点后无回复」状态的周期 = 待回复（pending）；
- 无周期时的本店回复（窗口截断 / 人工发送 / 历史回填）不参与统计。

回复率口径（Phase 3 出口「24h 回复率 ≥85%」，TIK-026 告警复用）：
- 已回复且首响未超阈值的周期计入达标；超时与待回复均计为未达标；
- 回复率 = (已回复周期数 - 超时周期数) / (已回复周期数 + 待回复周期数)；
- 无任何周期（既无已回复也无待回复）时返回 None（无数据，不判 0 也不判 1）。

输入约定：messages 为 (direction, msg_time) 序列，按时间升序传入
（调用方按 customer_uid 分组后逐会话处理）。

约束：本模块为纯函数，不依赖数据库与 common 其它模块（规范 52：无 I/O 便于单测）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Iterable, List, Optional, Sequence, Tuple

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


@dataclass(frozen=True)
class LatencyBucket:
    """一个首响时长分布桶（左闭右开，``upper=None`` 表示无上界的末桶）。"""

    label: str              # 桶中文标签（如「30 秒内」）
    lower: float            # 下界（含），秒
    upper: Optional[float]  # 上界（不含），秒；None = 无上界

    def contains(self, seconds: float) -> bool:
        """判断某首响时长是否落入本桶（左闭右开）。"""
        if seconds < self.lower:
            return False
        if self.upper is None:
            return True
        return seconds < self.upper


# 默认分布桶：以 5 分钟首响标准为界，向内按 30s / 1min / 3min / 5min 细分，
# 末桶为「超 5 分钟」（与超时判定同界，便于前端直接读末桶即超时占比）。
DEFAULT_BUCKETS: Tuple[LatencyBucket, ...] = (
    LatencyBucket("30 秒内", 0.0, 30.0),
    LatencyBucket("30–60 秒", 30.0, 60.0),
    LatencyBucket("1–3 分钟", 60.0, 180.0),
    LatencyBucket("3–5 分钟", 180.0, 300.0),
    LatencyBucket("超 5 分钟", 300.0, None),
)


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


def latency_distribution(
    cycles: Sequence[Cycle],
    buckets: Sequence[LatencyBucket] = DEFAULT_BUCKETS,
) -> List[dict]:
    """把已回复周期按首响时长分桶，返回分布明细（前端柱状图直接消费）。

    参数:
        cycles: 已回复周期列表。
        buckets: 分布桶定义，默认 ``DEFAULT_BUCKETS``（末桶即超 5 分钟）。
    返回:
        每桶一项的列表：{label, lower, upper, count, ratio}；``upper`` 为 None 表示
        无上界；``ratio`` 为占已回复周期数的比例（无周期时为 0.0），保留 4 位小数。
    """
    total = len(cycles)
    counts = [0] * len(buckets)
    for cycle in cycles:
        seconds = cycle.latency_seconds
        for index, bucket in enumerate(buckets):
            if bucket.contains(seconds):
                counts[index] += 1
                break
    return [
        {
            "label": bucket.label,
            "lower": bucket.lower,
            "upper": bucket.upper,
            "count": counts[index],
            "ratio": round(counts[index] / total, 4) if total else 0.0,
        }
        for index, bucket in enumerate(buckets)
    ]


def reply_rate(
    responded_cycles: int,
    over_threshold_count: int,
    pending_cycles: int = 0,
) -> Optional[float]:
    """计算回复率：达标周期数 / 总周期数（待回复与超时均计未达标）。

    对齐 Phase 3 出口口径「24h 回复率 ≥85%」（TIK-026 告警复用同一函数）：
    - 达标 = 已回复且首响未超阈值；
    - 未达标 = 首响超时 + 窗口结束仍待回复；
    - 分母 = 已回复周期数 + 待回复周期数（即窗口内全部待响应周期）。

    参数:
        responded_cycles: 已回复周期数。
        over_threshold_count: 其中首响超阈值的周期数。
        pending_cycles: 窗口结束时仍待回复的周期数。
    返回:
        回复率（0~1，保留 4 位小数）；无任何周期时返回 None（无数据）。
    """
    total = responded_cycles + pending_cycles
    if total <= 0:
        return None
    return round((responded_cycles - over_threshold_count) / total, 4)


__all__ = [
    "Cycle",
    "PendingCycle",
    "LatencyBucket",
    "DEFAULT_BUCKETS",
    "compute_cycles",
    "first_response_stats",
    "latency_distribution",
    "reply_rate",
    "FIRST_RESPONSE_THRESHOLD_SECONDS",
    "DIRECTION_IN",
    "DIRECTION_OUT",
]
