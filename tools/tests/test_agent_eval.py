# -*- coding: utf-8 -*-
"""
tools.tests.test_agent_eval —— 评测模块单元测试
================================================
本文件用途：验证 ``tools.agent_eval``（POL-007）的纯函数与编排逻辑：
- dataset：JSONL 加载 / 校验 / 非法行抛错 / 往返一致；
- metrics：hit@k / 回退率 / 合规率 / 延迟 p50·p95（含边界与属性测试）；
- compare：指标差分表 + 逐条回归明细，对相同输入输出零差分；
- runner：注入 mock kb_search / llm_client 的端到端确定性（两次运行一致）。

无关真实 DB / 网络：全部经 mock / 数据注入。
"""
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tools.agent_eval.compare import compare_reports
from tools.agent_eval.dataset import (
    GoldenCase,
    iter_cases,
    validate_case,
    write_cases,
)
from tools.agent_eval.metrics import (
    average_hit_at_k,
    fallback_rate,
    hit_at_k,
    keyword_compliance,
    latency_percentile,
)
from tools.agent_eval.report import build_report, to_json, to_markdown


# ---------------------------------------------------------------------------
# dataset 测试
# ---------------------------------------------------------------------------
def _write_tmp(tmp_path, lines):
    path = tmp_path / "ds.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_iter_cases_loads_valid(tmp_path) -> None:
    """合法 JSONL 加载成功。"""
    data = (
        '{"id": "a", "query": "支持退换货吗", "expected_kb_titles": ["退换货"],'
        ' "must_contain": ["七天"], "must_not_contain": [], "expect_fallback": false}'
    )
    path = _write_tmp(tmp_path, [data])
    cases = iter_cases(path)
    assert len(cases) == 1
    assert cases[0].id == "a"
    assert cases[0].must_contain == ["七天"]


def test_iter_cases_skips_blank_and_comment(tmp_path) -> None:
    """空行与 '#' 注释行跳过。"""
    data = (
        '{"id": "a", "query": "支持退换货吗"}',
        "# 这是注释",
        "",
        '{"id": "b", "query": "怎么退货"}',
    )
    cases = iter_cases(_write_tmp(tmp_path, data))
    assert [c.id for c in cases] == ["a", "b"]


def test_iter_cases_rejects_invalid_json(tmp_path) -> None:
    """非法 JSON 行抛 ValueError。"""
    path = _write_tmp(tmp_path, ["{not json"])
    with pytest.raises(ValueError):
        iter_cases(path)


def test_iter_cases_rejects_missing_query(tmp_path) -> None:
    """缺 query 抛 ValueError。"""
    path = _write_tmp(tmp_path, ['{"id": "a"}'])
    with pytest.raises(ValueError):
        iter_cases(path)


def test_validate_case_field_types() -> None:
    """列表字段必须为数组，expect_fallback 必须布尔。"""
    assert "must_contain 必须为数组" in validate_case({"id": "a", "query": "q", "must_contain": "x"})
    assert validate_case({"id": "a", "query": "q"}) == []


def test_write_cases_roundtrip(tmp_path) -> None:
    """write_cases 写出后可再读取（幂等往返）。"""
    cases = [GoldenCase(id="a", query="支持退换货吗", must_contain=["七天"])]
    path = str(tmp_path / "out.jsonl")
    write_cases(path, cases)
    assert iter_cases(path)[0].query == "支持退换货吗"


# ---------------------------------------------------------------------------
# metrics 测试
# ---------------------------------------------------------------------------
def test_hit_at_k_hit_and_miss() -> None:
    """hit@k：期望标题在 top-k 内命中返回 1，否则 0。"""
    assert hit_at_k(["退换货", "物流"], ["退换货"], k=3) == 1.0
    assert hit_at_k(["物流", "运费"], ["退换货"], k=3) == 0.0


def test_hit_at_k_empty_expected_is_hit() -> None:
    """期望为空视为命中（无约束）。"""
    assert hit_at_k([], [], k=3) == 1.0


def test_hit_at_k_zero_k_is_miss() -> None:
    """k<=0 视为未命中。"""
    assert hit_at_k(["退换货"], ["退换货"], k=0) == 0.0


def test_metrics_deterministic() -> None:
    """同输入两次结果一致（确定性）。"""
    cases = [
        {"result_titles": ["退换货"], "expected_kb_titles": ["退换货"], "reply": "支持七天无理由", "must_contain": ["七天"], "must_not_contain": []},
        {"result_titles": [], "expected_kb_titles": ["物流"], "reply": "默认回复", "must_contain": [], "must_not_contain": []},
    ]
    assert average_hit_at_k(cases) == pytest.approx(average_hit_at_k(cases))
    assert keyword_compliance(cases) == pytest.approx(keyword_compliance(cases))


@settings(max_examples=100, deadline=None)
@given(s=st.lists(st.integers(min_value=-10, max_value=100), min_size=0, max_size=50))
def test_latency_percentile_within_range(s) -> None:
    """延迟分位值落在输入范围内。"""
    lat = [float(x) for x in s]
    p50 = latency_percentile(lat, 50)
    if lat:
        assert min(lat) <= p50 <= max(lat)


# ---------------------------------------------------------------------------
# compare 测试
# ---------------------------------------------------------------------------
def _report(count, hit, fallback, reply="支持七天无理由退货", cat="kb-001"):
    return {
        "summary": {"total": count, "retrieval_hit_at_k": hit, "ai_fallback_rate": fallback, "keyword_compliance_rate": 0.5, "latency": {"p50": 10.0, "p95": 20.0}},
        "cases": [{"id": cat, "query": "退换货", "reply": reply, "used_default": False, "must_contain": ["七天"], "must_not_contain": [], "expected_kb_titles": ["退换货"]}],
    }


def test_compare_same_report_zero_diff() -> None:
    """对比同一份报告 → 零差分（属性测试基础）。"""
    r = _report(10, 0.8, 0.1)
    diff = compare_reports(r, r)["summary_diff"]
    assert diff["retrieval_hit_at_k_delta"] == 0.0
    assert diff["ai_fallback_rate_delta"] == 0.0
    assert diff["latency_p50_delta"] == 0.0


def test_compare_detects_hit_improvement() -> None:
    """检索命中率提升 → 差分 > 0；回退率上升 → 差分 > 0。"""
    base = _report(10, 0.6, 0.2)
    cand = _report(10, 0.9, 0.3)
    diff = compare_reports(base, cand)["summary_diff"]
    assert diff["retrieval_hit_at_k_delta"] == pytest.approx(0.3)
    assert diff["ai_fallback_rate_delta"] == pytest.approx(0.1)


def test_compare_lists_regression_on_reply_change() -> None:
    """回复变化 → 出现在回归明细。"""
    base = _report(1, 0.8, 0.1, reply="旧回复", cat="x")
    cand = _report(1, 0.8, 0.1, reply="新回复", cat="x")
    regressions = compare_reports(base, cand)["regressions"]
    assert len(regressions) == 1
    assert regressions[0]["id"] == "x"


def test_report_json_and_markdown_generation() -> None:
    """报告 JSON / Markdown 生成。"""
    from tools.agent_eval.runner import CaseOutcome

    outcome = CaseOutcome(
        id="x", query="退换货", reply="支持七天", used_default=False,
        expected_kb_titles=["退换货"], result_titles=["退换货"],
        latency_ms=12.5, completed=True,
    )
    report = build_report([outcome])
    assert json.loads(to_json(report))["summary"]["total"] == 1
    assert "# 评测报告" in to_markdown(report)


def test_runner_mock_determinism() -> None:
    """mock-llm 档两次运行报告逐字节一致（POL-007 确定性硬验收）。

    注入 mock 检索（返回固定标题）与 mock 生成器（返回固定回复 + 固定延迟），
    且 record_latency=False，验证两次产出 JSON 完全相同，可进 CI 回归门禁。
    """
    from tools.agent_eval.runner import CaseOutcome, run_all

    cases = [
        GoldenCase(id="a", query="支持退换货吗", expected_kb_titles=["退换货"],
                   must_contain=["七天"]),
        GoldenCase(id="b", query="什么时候发货", expected_kb_titles=["物流"]),
    ]

    def fake_kb_search(session, **kwargs):
        class _R:
            customer_service_knowledge = [
                type("K", (), {"title": t})() for t in (["退换货"] if kwargs.get("query") == "支持退换货吗" else [])
            ]
        return _R()

    async def fake_generate_reply(query, *, config=None, client=None, **kwargs):
        class _Res:
            content = f"为您解答：{query}"
            used_default = False
            error = None
        return _Res()

    def go():
        outcomes = run_all(
            cases, session=None, llm_client=object(), shop_id=1,
            config=object(), kb_search=fake_kb_search,
            generate_reply=fake_generate_reply, record_latency=False,
        )
        return to_json(build_report(outcomes))

    assert go() == go()  # 确定性：两次逐字节一致
    assert json.loads(go())["summary"]["total"] == 2
    assert json.loads(go())["summary"]["completed"] == 2


def test_run_case_uses_default_search_when_none() -> None:
    """kb_search=None 时回退默认 kb_service.search（不抛错）。"""
    from tools.agent_eval.runner import run_case

    async def fake_generate_reply(query, *, config=None, client=None, **kwargs):
        class _Res:
            content = "reply"
            used_default = False
            error = None
        return _Res()

    # 内存 SQLite + 默认检索：无知识行时检索返回空（不抛错）。
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker, Session
    from common.models.base import Base

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)
    with factory() as s:
        outcome = run_case(
            GoldenCase(id="a", query="退换货"), session=s, llm_client=object(),
            shop_id=1, config=object(), kb_search=None,
            generate_reply=fake_generate_reply, record_latency=False,
        )
    assert outcome.completed is True
    assert outcome.result_titles == []
