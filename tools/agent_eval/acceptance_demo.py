# -*- coding: utf-8 -*-
"""
tools.agent_eval.acceptance_demo —— 评测报告演示（POL-009）
===========================================================
本工具用途：在无真实 LLM 配置的现实条件下，用 **mock 档**生成评测模块的三份
报告演示（mock-llm 档 / judge 档 / baseline-vs-candidate 对比），落盘
`tools/agent_eval/reports/`，供简历引用评测体系的形态与确定性。

⚠️ 声明：本演示 **mock 了 LLM 与知识检索后端**（确定性假值），非真实 LLM 输出；
仅演示报告生成 / 指标聚合 / 对比管线。真实档需真实 LLM 配置 + 真实店铺知识
（当前环境无 key、库无知识数据，见 ACCEPTANCE 遗留项）。

演示内容：
- baseline.jsonl：纯关键词检索（mock 返回分词命中的标题）。
- candidate.jsonl：混合检索（mock 返回语义命中的标题，含 baseline 漏掉的）。
- 用 cli.runner 分别跑 mock-llm 档生成两份 report，再经 compare.py 对比，
  并叠加 judge 档（mock judge 打分），三份报告落盘。
"""
from __future__ import annotations

import os
import sys
from typing import Any

# 保证 tools / common / websocket 可从仓库根导入（脚本方式直接运行）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.agent_eval.dataset import GoldenCase, iter_cases
from tools.agent_eval.judge import average_judge, build_judge_prompt, parse_judge_response
from tools.agent_eval.report import build_report, save_report
from tools.agent_eval.runner import CaseOutcome

# 演示：部分 query 在「纯关键词」下漏掉期望知识，在「混合检索」下命中。
# 键用 seed 数据集中实际存在的 query（口语化场景），baseline 字面匹配会漏检。
_NEED_SEMANTIC: dict[str, list[str]] = {
    "你们支持七天无理由退换货吗": ["退换货政策"],
    "支持七天无理由退换货吗": ["退换货政策"],
    "能退吗": ["退换货政策"],
    "支持七天无理由吗": ["退换货政策"],
    "多久发货": ["发货时效"],
    "这个商品多久发货": ["发货时效"],
    "运费谁出": ["运费说明"],
    "有优惠吗": ["优惠活动"],
}


def _mock_search(semantic: bool):
    """构造 mock 检索后端：semantic=True 时返回更多语义命中的标题。"""

    def _kb_search(session, **kwargs):
        query = kwargs.get("query", "")
        titles = ["退换货政策", "发货时效", "运费说明", "优惠活动", "尺码说明"]
        # 需语义命中的 query（口语化/同义词）：baseline 字面匹配会漏检。
        if semantic:
            # candidate：语义命中，返回期望知识（补齐 baseline 漏检项）。
            matched = _NEED_SEMANTIC.get(query)
            if matched is None:
                matched = [t for t in titles if any(ch in t for ch in query)][:2]
        else:
            # baseline：字面匹配；对需语义命中的 query 返回空（模拟 jieba 漏检）。
            if query in _NEED_SEMANTIC:
                matched = []
            else:
                matched = [t for t in titles if any(ch in t for ch in query)][:2]

        class _R:
            customer_service_knowledge = [
                type("K", (), {"title": t})() for t in matched
            ]

        return _R()

    return _kb_search


async def _mock_generate_reply(query, *, config=None, client=None, **kwargs):
    """mock 生成器：复述 query，命中 must_contain（确定性）。"""

    class _Res:
        content = f"您好，为您解答：{query}"
        used_default = False
        error = None

    return _Res()


def _mock_judge(query: str, reply: str) -> dict[str, Any]:
    """mock judge：按固定规则打分（确定性），模拟 LLM-as-judge 解析结果。"""
    # 模拟解析后的打分（与 judge.parse 结构一致）。
    return {"准确性": 5, "相关性": 4, "合规性": 5, "理由": "回答准确且合规（仿真）"}


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    dataset = os.path.join(here, "datasets", "seed_shop.jsonl")
    out_dir = os.path.join(here, "reports")
    os.makedirs(out_dir, exist_ok=True)

    cases = iter_cases(dataset)
    # 只取与本演示覆盖的问题子集（其余走通用 mock 检索）。
    demo_queries = list(_NEED_SEMANTIC.keys())
    sub_cases = [c for c in cases if c.query in demo_queries] or cases[:8]

    sessions = {}
    baseline_outcomes: list[CaseOutcome] = []
    candidate_outcomes: list[CaseOutcome] = []
    judgements: list[Any] = []

    for case in sub_cases:
        for label, semantic in (("baseline", False), ("candidate", True)):
            titles = _mock_search(semantic)(None, query=case.query, limit=5).customer_service_knowledge
            result_titles = [getattr(t, "title") for t in titles]
            reply = "您好，为您解答：" + case.query
            outcome = CaseOutcome(
                id=case.id, query=case.query,
                expected_kb_titles=case.expected_kb_titles,
                result_titles=result_titles, reply=reply,
                used_default=False, expected_fallback=case.expect_fallback,
                latency_ms=0.0, completed=True,
            )
            if label == "baseline":
                baseline_outcomes.append(outcome)
            else:
                candidate_outcomes.append(outcome)

    # 生成 baseline / candidate 报告。
    baseline_report = build_report(baseline_outcomes)
    candidate_report = build_report(candidate_outcomes)
    save_report(baseline_report, out_dir, "report_baseline")
    save_report(candidate_report, out_dir, "report_candidate")

    # judge 档：对 candidate 报告逐条打分（仿真）。
    judge_avg = average_judge([_mock_judge(c["query"], c["reply"]) for c in candidate_report["cases"]])
    candidate_report["judge"] = judge_avg
    save_report(candidate_report, out_dir, "report_candidate_with_judge")
    # 另存一份 judge 平均分。
    import json

    with open(os.path.join(out_dir, "judge_scores.json"), "w", encoding="utf-8") as fh:
        json.dump(judge_avg, fh, ensure_ascii=False, indent=2)

    # compare：baseline vs candidate。
    from tools.agent_eval.compare import compare_reports

    diff = compare_reports(baseline_report, candidate_report)
    with open(os.path.join(out_dir, "compare_baseline_vs_candidate.json"), "w", encoding="utf-8") as fh:
        json.dump(diff, fh, ensure_ascii=False, indent=2)

    b_hit = baseline_report["summary"]["retrieval_hit_at_k"]
    c_hit = candidate_report["summary"]["retrieval_hit_at_k"]
    print(
        f"已生成报告到 {out_dir}\n"
        f"baseline hit@k={b_hit:.2f} / candidate hit@k={c_hit:.2f} "
        f"(delta={c_hit - b_hit:+.2f})\njudge avg={judge_avg}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
