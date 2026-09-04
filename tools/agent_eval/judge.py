# -*- coding: utf-8 -*-
"""
tools.agent_eval.judge —— LLM-as-judge 可选档
=============================================
本文件用途：为「评测模块」（POL-008）提供 `--judge` 可选档——用已配置 LLM 按
固定 rubric 对 AI 回复打分（准确性 / 相关性 / 合规性 各 1-5 分 + 一句中文理由）。

设计要点：
- **默认关闭**：仅显式 `--judge` 启用；judge 失败（超时 / 解析失败）记为 None，
  不影响确定性指标。
- **纯函数分离**：prompt 构建（``build_judge_prompt``）与结果解析
  （``parse_judge_response``）拆为纯函数，便于单测（mock LLM 返回合法 / 非法 JSON）。
- 判别与评分解耦：judge 使用与 runner 不同的 LLM 调用路径（一次对话即可），
  不走工具切换。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# rubric 维度（固定，打分 1-5）。
JUDGE_DIMENSIONS: tuple[str, ...] = ("准确性", "相关性", "合规性")


def _rubric_text() -> str:
    """构造 rubric 中文说明（固定，保证确定性）。"""
    return (
        "请按以下三个维度对 AI 客服回复打分（各 1-5 分，5 为最佳）：\n"
        "1. 准确性：回复是否准确回答了买家问题，无事实错误；\n"
        "2. 相关性：回复是否切题，未偏离买家问的要点；\n"
        "3. 合规性：回复是否合规，无不实承诺（如漏发、假一赔十）或违规话术。\n"
        "请严格输出 JSON，格式："
        '{"准确性": <1-5整数>, "相关性": <1-5整数>, "合规性": <1-5整数>, "理由": "<一句中文理由>"}'
    )


def build_judge_prompt(query: str, reply: str, rubric_text: str | None = None) -> str:
    """构造 judge 提示词（纯函数）。

    Args:
        query: 买家问题。
        reply: AI 回复文本。
        rubric_text: rubric 说明（缺省用固定 rubric，便于测试注入）。

    Returns:
        发送给 judge 模型的完整提示词。
    """
    rubric = rubric_text or _rubric_text()
    return (
        f"{rubric}\n\n"
        f"买家问题：{query}\n"
        f"AI 回复：{reply}\n"
        f"请打分并输出 JSON。"
    )


def parse_judge_response(raw: str | None) -> Optional[dict[str, Any]]:
    """解析 judge 模型返回文本为结构化打分；失败返回 None。

    兼容模型输出被 markdown 代码块包裹（```json ... ```）或含前后导语的情况：
    尝试提取 JSON 片段并对分数做 1-5 区间规整；任一维度缺省或非法 → 整体失败。

    Args:
        raw: judge 模型返回的原始文本。

    Returns:
        {"准确性": int, "相关性": int, "合规性": int, "理由": str}；
        解析失败返回 None。
    """
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    # 剥掉可能的 ```json / ``` 包裹。
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    else:
        # 取首尾大括号 JSON 片段（容忍导语）。
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    scores: dict[str, int] = {}
    for dim in JUDGE_DIMENSIONS:
        val = obj.get(dim)
        try:
            num = int(val)
        except (TypeError, ValueError):
            return None
        if num < 1 or num > 5:
            return None
        scores[dim] = num
    reason = str(obj.get("理由") or "").strip()
    if not reason:
        return None
    return {
        "准确性": scores["准确性"],
        "相关性": scores["相关性"],
        "合规性": scores["合规性"],
        "理由": reason,
    }


def average_judge(judgments: list[Optional[dict[str, Any]]]) -> dict[str, float | None]:
    """聚合多条 judge 打分为平均分（缺失记 None，不纳入平均）。

    Args:
        judgments: 每条用例的 judge 结果（可为 None / 已解析 dict）。

    Returns:
        各维度平均分（缺失维度返回 None）。
    """
    valid = [j for j in judgments if j]
    out: dict[str, float | None] = {}
    for dim in JUDGE_DIMENSIONS:
        vals = [float(j[dim]) for j in valid if isinstance(j, dict) and j.get(dim)]
        out[dim] = sum(vals) / len(vals) if vals else None
    return out


__all__ = [
    "JUDGE_DIMENSIONS",
    "build_judge_prompt",
    "parse_judge_response",
    "average_judge",
]
