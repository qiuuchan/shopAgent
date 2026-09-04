# -*- coding: utf-8 -*-
"""
tools.agent_eval.report —— 评测报告生成（JSON + Markdown）
==========================================================
本文件用途：聚合用例结果并计算确定性指标，产出 JSON / Markdown 报告
（POL-007）。指标均为纯函数（``metrics``），报告生成幂等：对同一输入
两次生成逐字节一致（确定性验收基础）。

报告结构：
- 汇总：用例数 / 检索 hit@k / AI 回退率 / 关键词合规率 / 延迟 p50·p95；
- 明细：每条用例的 h、检索标题、回复、合规、延迟。
"""
from __future__ import annotations

import json
import os
from typing import Any

from tools.agent_eval.metrics import (
    average_hit_at_k,
    fallback_rate,
    keyword_compliance,
    latency_distribution,
)
from tools.agent_eval.runner import CaseOutcome


def build_report(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """聚合结果为报告字典（JSON 序列化前结构）。

    Args:
        outcomes: 用例原始结果列表。

    Returns:
        {summary, cases} 结构的报告字典。
    """
    case_dicts = [o.to_dict() for o in outcomes]
    latencies = [o.latency_ms for o in outcomes if o.completed]
    summary = {
        "total": len(outcomes),
        "completed": sum(1 for o in outcomes if o.completed),
        "retrieval_hit_at_k": average_hit_at_k(case_dicts),
        "ai_fallback_rate": fallback_rate(case_dicts),
        "keyword_compliance_rate": keyword_compliance(case_dicts),
        "latency": latency_distribution(latencies),
    }
    return {"summary": summary, "cases": case_dicts}


def to_json(report: dict[str, Any], indent: int = 2) -> str:
    """序列化为 JSON 字符串（ensure_ascii=False，保留中文）。"""
    return json.dumps(report, ensure_ascii=False, indent=indent)


def to_markdown(report: dict[str, Any]) -> str:
    """渲染报告为 Markdown 文本（供人读 / 落盘 ACCEPTANCE）。"""
    s = report["summary"]
    lines: list[str] = []
    lines.append("# 评测报告")
    lines.append("")
    lines.append(f"- 用例数：{s['total']}（完成 {s['completed']}）")
    lines.append(f"- 检索 hit@k：{s['retrieval_hit_at_k']:.3f}")
    lines.append(f"- AI 回退率：{s['ai_fallback_rate']:.3f}")
    lines.append(f"- 关键词合规率：{s['keyword_compliance_rate']:.3f}")
    lat = s.get("latency", {})
    lines.append(
        f"- 延迟：p50={lat.get('p50', 0):.1f}ms；p95={lat.get('p95', 0):.1f}ms"
    )
    lines.append("")
    lines.append("## 明细")
    lines.append("")
    lines.append("| id | 命中 | 回退 | 合规 | 延迟(ms) |")
    lines.append("| --- | --- | --- | --- | --- |")
    for case in report["cases"]:
        hit = case.get("result_titles", [])
        expected = case.get("expected_kb_titles", [])
        ok = 1.0 if any(t in hit for t in expected) else 0.0 if expected else 1.0
        comply = _case_comply(case)
        lines.append(
            f"| {case['id']} | {ok:.1f} | "
            f"{'是' if case.get('used_default') else '否'} | "
            f"{'是' if comply else '否'} | {case.get('latency_ms', 0):.0f} |"
        )
    return "\n".join(lines)


def _case_comply(case: dict[str, Any]) -> bool:
    """判断单条用例是否满足关键词合规。"""
    reply = str(case.get("reply") or "")
    must_contain = [str(x) for x in (case.get("must_contain") or [])]
    must_not_contain = [str(x) for x in (case.get("must_not_contain") or [])]
    return all(m in reply for m in must_contain) and all(
        m not in reply for m in must_not_contain
    )


def save_report(report: dict[str, Any], out_dir: str, basename: str) -> str:
    """落盘 JSON 与 Markdown 报告，返回生成的 JSON 文件路径。

    Args:
        report: 报告字典。
        out_dir: 输出目录。
        basename: 文件名基础名（不含后缀）。

    Returns:
        JSON 文件绝对路径。
    """
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"{basename}.json")
    md_path = os.path.join(out_dir, f"{basename}.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(to_json(report))
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(report))
    return json_path


__all__ = ["build_report", "to_json", "to_markdown", "save_report"]
