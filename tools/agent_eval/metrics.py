# -*- coding: utf-8 -*-
"""
tools.agent_eval.metrics —— 评测确定性指标（纯函数）
=====================================================
本文件用途：为「评测模块」（POL-007）提供确定性指标的纯函数计算。全部指标
均为同步纯函数、无 I/O、无网络，可独立单测与属性测试：

- ``hit_at_k``：检索命中率——期望知识条目标题是否出现在检索结果 top-k。
- ``fallback_rate``：AI 回退率——期望回退 / 实际回退的统计。
- ``keyword_compliance``：关键词合规率——回复必须包含 must_contain 全中且
  必须不含 must_not_contain 全不中。
- ``latency_percentile``：延迟分布 p50 / p95。

约定：所有指标值 ∈ [0, 1]；空输入返回 0（避免除零）。
"""
from __future__ import annotations

from typing import Sequence

# 延迟分布默认分位点（p50 / p95）。
LATENCY_PERCENTILES: tuple[float, ...] = (50.0, 95.0)


def hit_at_k(
    result_titles: Sequence[str],
    expected_titles: Sequence[str],
    k: int = 3,
) -> float:
    """检索命中率：期望知识条目标题是否落在检索结果 top-k 内。

    Args:
        result_titles: 检索返回的标题列表（已按相关度排序）。
        expected_titles: 该问题的期望命中标题列表。

    Returns:
        1.0（任一期望标题在 top-k 内命中）或 0.0；expected 为空时视为命中，返回 1.0。
    """
    if not expected_titles:
        return 1.0
    if k <= 0:
        return 0.0
    top = list(result_titles)[: max(1, int(k))]
    top_set = {str(t).strip() for t in top}
    return 1.0 if any(str(exp).strip() in top_set for exp in expected_titles) else 0.0


def average_hit_at_k(cases: Sequence[dict[str, object]]) -> tuple[float, float]:
    """批量检索命中率：返回 (平均 hit@k, 各条命中率列表均值)。

    Args:
        cases: 含 ``result_titles`` 与 ``expected_kb_titles`` 字段的用例 dict 列表。

    Returns:
        (平均 hit@k ∈ [0,1])；无用例返回 0.0。
    """
    if not cases:
        return 0.0
    scores = [
        hit_at_k(
            case.get("result_titles", []),
            case.get("expected_kb_titles", []),
            k=int(case.get("hit_k", 3)),
        )
        for case in cases
    ]
    return sum(scores) / len(scores)


def fallback_rate(
    cases: Sequence[dict[str, object]],
    *,
    key: str = "used_default",
    expected_key: str = "expect_fallback",
) -> float:
    """AI 回退率：实际回退比例（true 为回退）。

    Args:
        cases: 含 ``used_default`` 与实际回退标志的用例 dict 列表。
        key: 实际回退信号字段名。
        expected_key: 期望回退信号字段名（用于可选地统计「期望回退命中」）。

    Returns:
        实际回退用例占比 ∈ [0,1]；无用例返回 0.0。
    """
    if not cases:
        return 0.0
    actual = sum(1 for c in cases if bool(c.get(key)))
    return actual / len(cases)


def keyword_compliance(cases: Sequence[dict[str, object]]) -> float:
    """关键词合规率：must_contain 全中且 must_not_contain 全不中的用例占比。

    Args:
        cases: 含 ``reply``、``must_contain``、``must_not_contain`` 的用例 dict 列表。

    Returns:
        合规用例占比 ∈ [0,1]；无用例返回 0.0。
    """
    if not cases:
        return 0.0
    ok = 0
    for case in cases:
        reply = str(case.get("reply") or "")
        must_contain = [str(x) for x in (case.get("must_contain") or [])]
        must_not_contain = [str(x) for x in (case.get("must_not_contain") or [])]
        if all(m in reply for m in must_contain) and all(m not in reply for m in must_not_contain):
            ok += 1
    return ok / len(cases)


def latency_percentile(latencies: Sequence[float], percentile: float) -> float:
    """计算延迟分布的分位值（升序插值），用于 p50 / p95。

    Args:
        latencies: 响应延迟序列（毫秒）。
        percentile: 分位点（如 50、95）。

    Returns:
        分位延迟值（毫秒）；空序列返回 0.0。
    """
    if not latencies:
        return 0.0
    ordered = sorted(float(v) for v in latencies)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(100.0, float(percentile))) / 100.0 * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def latency_distribution(latencies: Sequence[float]) -> dict[str, float]:
    """返回延迟分布 p50 / p95 汇总字典。"""
    return {
        f"p{int(p)}": latency_percentile(latencies, p) for p in LATENCY_PERCENTILES
    }


__all__ = [
    "LATENCY_PERCENTILES",
    "hit_at_k",
    "average_hit_at_k",
    "fallback_rate",
    "keyword_compliance",
    "latency_percentile",
    "latency_distribution",
]
