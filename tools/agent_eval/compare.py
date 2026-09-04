# -*- coding: utf-8 -*-
"""
tools.agent_eval.compare —— 评测报告对比（baseline vs candidate）
==================================================================
本文件用途：对比两份评测报告（如「纯关键词 vs 混合检索」或「prompt 改动前
后」），输出指标差分表与逐条回归明细（POL-007 回归门禁）。

设计要点：
- **零差分恒等**：对比同一份报告两次应输出零差分（属性测试验证）；
- 指标差分：hit@k / 回退率 / 合规率 / 延迟，按 candidate - baseline 方向报告；
- 回归明细：仅列出行为变化（回复/回退/合规变化）的用例，便于定位。
"""
from __future__ import annotations

from typing import Any


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """对比两份报告，返回 {summary_diff, regressions}。

    Args:
        baseline: 基线报告（如纯关键词检索）。
        candidate: 候选报告（如混合检索 / prompt 改动后）。

    Returns:
        含指标差分与逐条回归明细的对比字典。
    """
    b_sum = baseline.get("summary", {})
    c_sum = candidate.get("summary", {})
    diff = {
        "total": c_sum.get("total", 0),
        "retrieval_hit_at_k_delta": _delta(c_sum, b_sum, "retrieval_hit_at_k"),
        "ai_fallback_rate_delta": _delta(c_sum, b_sum, "ai_fallback_rate"),
        "keyword_compliance_rate_delta": _delta(c_sum, b_sum, "keyword_compliance_rate"),
        "latency_p50_delta": _delta(
            c_sum.get("latency", {}), b_sum.get("latency", {}), "p50"
        ),
        "latency_p95_delta": _delta(
            c_sum.get("latency", {}), b_sum.get("latency", {}), "p95"
        ),
    }
    return {
        "summary_diff": diff,
        "regressions": _regressions(baseline, candidate),
    }


def _delta(case: dict[str, Any], base: dict[str, Any], key: str) -> float:
    """计算 candidate - baseline 的指标差。"""
    c = case.get(key, 0.0) or 0.0
    b = base.get(key, 0.0) or 0.0
    return c - b


def _regressions(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """列出行为发生变化的用例（回复 / 回退 / 合规变化）。"""
    b_cases = {c.get("id"): c for c in baseline.get("cases", [])}
    regressions: list[dict[str, Any]] = []
    for c in candidate.get("cases", []):
        b = b_cases.get(c.get("id"))
        if b is None:
            regressions.append({"id": c.get("id"), "change": "新增用例"})
            continue
        changed = (
            str(b.get("reply") or "") != str(c.get("reply") or "")
            or bool(b.get("used_default")) != bool(c.get("used_default"))
            or _comply(b) != _comply(c)
        )
        if changed:
            regressions.append(
                {
                    "id": c.get("id"),
                    "query": c.get("query"),
                    "baseline_reply": b.get("reply"),
                    "candidate_reply": c.get("reply"),
                    "baseline_used_default": b.get("used_default"),
                    "candidate_used_default": c.get("used_default"),
                }
            )
    return regressions


def _comply(case: dict[str, Any]) -> bool:
    """判断单条用例是否合规。"""
    reply = str(case.get("reply") or "")
    must_contain = [str(x) for x in (case.get("must_contain") or [])]
    must_not_contain = [str(x) for x in (case.get("must_not_contain") or [])]
    return all(m in reply for m in must_contain) and all(
        m not in reply for m in must_not_contain
    )


__all__ = ["compare_reports"]
