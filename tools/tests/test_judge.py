# -*- coding: utf-8 -*-
"""
tools.tests.test_judge —— LLM-as-judge 可选档单元测试
=====================================================
本文件用途：验证 ``tools.agent_eval.judge``（POL-008）的纯函数：
- prompt 构建：含 rubric + 问题 + 回复（可注入 rubric 便于测试）；
- 结果解析：mock LLM 返回合法 / 非法 JSON 两分支（含 markdown 包裹、
  带导语、分数越界、缺理由等边界）；
- 平均值聚合：缺失记 None 不纳入平均。

无关真实 LLM / 网络，全部构建测试输入。
"""
import pytest

from tools.agent_eval.judge import (
    average_judge,
    build_judge_prompt,
    parse_judge_response,
)


def test_build_judge_prompt_contains_question_and_reply() -> None:
    """prompt 含 rubric、问题、回复。"""
    prompt = build_judge_prompt("支持退换货吗", "支持七天无理由退货")
    assert "支持退换货吗" in prompt
    assert "支持七天无理由退货" in prompt
    assert "准确性" in prompt and "相关性" in prompt and "合规性" in prompt


def test_build_judge_prompt_inject_rubric() -> None:
    """可注入 rubric 便于测试。"""
    prompt = build_judge_prompt("q", "r", rubric_text="自定义 rubric")
    assert prompt.startswith("自定义 rubric")


def test_parse_judge_response_valid() -> None:
    """合法 JSON 返回结构化打分。"""
    parsed = parse_judge_response('{"准确性": 5, "相关性": 4, "合规性": 3, "理由": "回答准确"}')
    assert parsed == {"准确性": 5, "相关性": 4, "合规性": 3, "理由": "回答准确"}


def test_parse_judge_response_markdown_fenced() -> None:
    """markdown 代码块包裹的 JSON 可解析。"""
    raw = '```json\n{"准确性": 4, "相关性": 5, "合规性": 4, "理由": "切题"}\n```'
    parsed = parse_judge_response(raw)
    assert parsed["准确性"] == 4


def test_parse_judge_response_with_preamble() -> None:
    """带导语的 JSON 片段可解析。"""
    raw = '好的，以下是打分：{"准确性": 3, "相关性": 5, "合规性": 4, "理由": "合理"} 请参考'
    parsed = parse_judge_response(raw)
    assert parsed["相关性"] == 5


def test_parse_judge_response_score_out_of_range() -> None:
    """分数越界（0 或 6）判失败返回 None。"""
    assert parse_judge_response('{"准确性": 6, "相关性": 4, "合规性": 3, "理由": "x"}') is None
    assert parse_judge_response('{"准确性": 0, "相关性": 4, "合规性": 3, "理由": "x"}') is None


def test_parse_judge_response_missing_fields() -> None:
    """缺维度 / 缺理由 → None。"""
    assert parse_judge_response('{"准确性": 5, "相关性": 4, "合规性": 3}') is None
    assert parse_judge_response('{"准确性": "a", "相关性": 4, "合规性": 3, "理由": "x"}') is None


@pytest.mark.parametrize("raw", [None, "", "非法", "```json\n{not json}\n```", "{}"])
def test_parse_judge_response_invalid(raw) -> None:
    """各类非法输入返回 None。"""
    assert parse_judge_response(raw) is None


def test_average_judge_ignores_none() -> None:
    """缺失（None）不纳入平均。"""
    judgments = [
        {"准确性": 5, "相关性": 4, "合规性": 3, "理由": "a"},
        None,
        {"准确性": 3, "相关性": 5, "合规性": 4, "理由": "b"},
    ]
    avg = average_judge(judgments)
    assert avg["准确性"] == pytest.approx(4.0)
    assert avg["相关性"] == pytest.approx(4.5)
    assert avg["合规性"] == pytest.approx(3.5)


def test_average_judge_all_none() -> None:
    """全部缺失时各维度为 None。"""
    avg = average_judge([None, None])
    assert avg["准确性"] is None
    assert avg["相关性"] is None
    assert avg["合规性"] is None
