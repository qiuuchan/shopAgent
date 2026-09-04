# -*- coding: utf-8 -*-
"""
tools.agent_eval.dataset —— golden 数据集加载与校验
===================================================
本文件用途：为「评测模块」（POL-007）提供 golden 数据集的 JSONL 加载与校验。
数据集格式（每行一个 JSON 对象）：

    {id, query, expected_kb_titles[], must_contain[], must_not_contain[], expect_fallback}

字段约定（均为全中文电商客服场景）：
- ``id``：数据点唯一标识（字符串必填）。
- ``query``：买家问题文本（必填）。
- ``expected_kb_titles``：期望检索命中的知识条目标题列表（可为空）。
- ``must_contain``：AI 回复必须包含的子串列表（关键词合规正向约束）。
- ``must_not_contain``：AI 回复必须不含的子串列表（关键词合规负向约束）。
- ``expect_fallback``：布尔，是否期望触发 AI 回退（默认回复）。

设计要点：
- **纯函数**：加载 / 校验不依赖数据库与网络，便于单测与属性测试。
- **确定性**：对同一文件两次加载结果逐字节一致（POL-007 确定性验收基础）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# 数据集文件允许的后缀（防止误加载非 JSONL 文件）。
ALLOWED_SUFFIXES = (".jsonl", ".jsonl.gz", ".ndjson")


@dataclass
class GoldenCase:
    """单条 golden 测试用例（POL-007 数据集行）。"""

    id: str
    query: str
    expected_kb_titles: list[str] = field(default_factory=list)
    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    expect_fallback: bool = False


def validate_case(case: GoldetCode) -> list[str]:
    """校验单条 golden 用例，返回字段错误中文列表（为空表示合法）。

    Args:
        case: 预解析的用例字典。

    Returns:
        错误描述列表；为空表示通过校验。
    """
    errors: list[str] = []
    case_id = case.get("id")
    if case_id is None or str(case_id).strip() == "":
        errors.append("id 缺失或为空")
    if not case.get("query") or not str(case["query"]).strip():
        errors.append("query 缺失或为空")
    # 列表型字段：允许缺省视为空，但若提供则必须为 list。
    for key in ("expected_kb_titles", "must_contain", "must_not_contain"):
        if key in case and case[key] is not None and not isinstance(case[key], list):
            errors.append(f"{key} 必须为数组")
    # expect_fallback：缺省 False；提供则必须为布尔。
    if "expect_fallback" in case and not isinstance(case["expect_fallback"], bool):
        errors.append("expect_fallback 必须为布尔值")
    return errors


def iter_cases(path: str) -> list[GoldenCase]:
    """从 JSONL 文件加载并校验 golden 用例，非法行抛 ValueError。

    逐行解析 JSON；空行与 '#' 注释行跳过；非法 JSON 或校验失败即抛错
    （宁可失败也不静默吞掉脏数据，保证评测确定性）。

    Args:
        path: 数据集文件路径。

    Returns:
        合法用例列表（保持文件行序）。

    Raises:
        ValueError: 文件不存在 / 行 JSON 非法 / 用例校验失败。
    """
    import os

    if not os.path.exists(path):
        raise ValueError(f"数据集不存在：{path}")

    cases: list[GoldenCase] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {line_no} 行 JSON 解析失败：{exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"第 {line_no} 行必须为 JSON 对象")
            errors = validate_case(item)
            if errors:
                raise ValueError(f"第 {line_no} 行校验失败：{'；'.join(errors)}")
            cases.append(_to_case(item))
    return cases


def _to_case(item: dict[str, Any]) -> GoldenCase:
    """将字典转换为 GoldenCase（数组字段确保为列表）。"""
    return GoldenCase(
        id=str(item["id"]),
        query=str(item["query"]),
        expected_kb_titles=[str(x) for x in (item.get("expected_kb_titles") or [])],
        must_contain=[str(x) for x in (item.get("must_contain") or [])],
        must_not_contain=[str(x) for x in (item.get("must_not_contain") or [])],
        expect_fallback=bool(item.get("expect_fallback", False)),
    )


def write_cases(path: str, cases: list[GoldenCase]) -> None:
    """将用例列表写出为 JSONL（幂等，供测试确定性比对）。"""
    with open(path, "w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(_to_dict(case), ensure_ascii=False) + "\n")


def _to_dict(case: GoldenCase) -> dict[str, Any]:
    """将 GoldenCase 转为可 JSON 序列化的字典。"""
    return {
        "id": case.id,
        "query": case.query,
        "expected_kb_titles": case.expected_kb_titles,
        "must_contain": case.must_contain,
        "must_not_contain": case.must_not_contain,
        "expect_fallback": case.expect_fallback,
    }


__all__ = [
    "GoldenCase",
    "validate_case",
    "iter_cases",
    "write_cases",
]
