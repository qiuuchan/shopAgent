# -*- coding: utf-8 -*-
"""
tools.agent_eval.runner —— 评测执行核心
========================================
本文件用途：为「评测模块」（POL-007）提供逐用例执行检索 + AI 回复的编排核心。
对每条 golden 用例执行：
1. 知识库检索（``common.services.kb_service.search``）取匹配标题；
2. 生成 AI 回复（``websocket.agent.ai_reply_engine.generate_reply``）；
3. 记录回复文本、是否回退、延迟等原始结果。

设计要点：
- **可注入**：``session`` / ``llm_client`` / ``kb_search`` 均可注入，便于测试
  完全 mock（免真实 MySQL / LLM / 网络），保证确定性。
- **mock-llm 模式**：注入假 LLM 客户端（按脚本回复 / 触发工具调用 / 模拟超时），
  使两次运行结果逐字节一致（POL-007 确定性硬验收）。
- 输出与报告分离：本模块只产出 ``CaseOutcome`` 列表，JSON / Markdown 报告由
  ``report.py`` 负责。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from common.services.kb_service import search as _default_search
from tools.agent_eval.dataset import GoldenCase

# generate_reply 返回标记（复用 websocket 定义，避免重复常量）。
RESULT_AI_REPLY = "AI回复"
RESULT_AI_REPLY_FAILED = "AI 回复失败"


@dataclass
class CaseOutcome:
    """单条用例的评测原始结果。"""

    id: str
    query: str
    expected_kb_titles: list[str] = field(default_factory=list)
    result_titles: list[str] = field(default_factory=list)
    reply: str = ""
    used_default: bool = False
    expected_fallback: bool = False
    latency_ms: float = 0.0
    completed: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        return {
            "id": self.id,
            "query": self.query,
            "expected_kb_titles": self.expected_kb_titles,
            "result_titles": self.result_titles,
            "reply": self.reply,
            "used_default": self.used_default,
            "expected_fallback": self.expected_fallback,
            "latency_ms": self.latency_ms,
            "completed": self.completed,
            "error": self.error,
        }


def run_case(
    case: GoldenCase,
    *,
    session: Any,
    llm_client: Any,
    shop_id: int,
    config: Any = None,
    default_reply: str = "",
    kb_search: Callable[..., Any] | None = None,
    generate_reply: Callable[..., Any],
    retrieval_limit: int = 5,
    record_latency: bool = True,
) -> CaseOutcome:
    """执行单条用例：检索 + 生成回复，返回原始结果（含延迟）。

    Args:
        case: 待评测用例。
        session: 数据库会话（供检索使用）。
        llm_client: LLM 客户端（真实或 mock）。
        shop_id: 店铺主键（店铺隔离参数）。
        config: AI 运行时配置（AgentConfig）；mock 档传有效假配置以走通
            ``generate_reply`` 的 LLM 分支（否则因配置无效直接回退）。
        default_reply: AI 回退时的默认回复文本。
        kb_search: 知识库检索函数（可注入；缺省 None 时用默认 kb_service.search）。
        generate_reply: AI 回复生成函数（可注入；默认生成器在 cli 层装配）。
        retrieval_limit: 检索结果上限。
        record_latency: 是否记录墙钟延迟；mock 档设 False 用固定值以保证
            报告逐字节一致（POL-007 确定性）。

    Returns:
        该用例的原始结果。
    """
    search_fn = kb_search or _default_search
    started = time.perf_counter()
    try:
        result = search_fn(session, shop_pk=shop_id, query=case.query, limit=retrieval_limit)
        titles = [t for t in _record_titles(result)]
    except Exception as exc:  # noqa: BLE001 - 检索失败记录为未完成
        elapsed = (time.perf_counter() - started) * 1000 if record_latency else 0.0
        return CaseOutcome(
            id=case.id, query=case.query, expected_kb_titles=case.expected_kb_titles,
            latency_ms=elapsed, completed=False, error=f"检索失败：{exc}",
        )

    # 生成 AI 回复（异步）：注入 llm_client 保证可 mock，且 config 有效以走通 LLM 分支。
    awaitable = generate_reply(
        case.query,
        config=config,
        shop_id=shop_id,
        default_reply=default_reply,
        history=None,
        client=llm_client,
    )
    reply_result = asyncio.run(awaitable)
    elapsed = (time.perf_counter() - started) * 1000 if record_latency else 0.0

    return CaseOutcome(
        id=case.id,
        query=case.query,
        expected_kb_titles=case.expected_kb_titles,
        result_titles=titles,
        reply=reply_result.content or "",
        used_default=bool(reply_result.used_default),
        expected_fallback=case.expect_fallback,
        latency_ms=elapsed,
        completed=True,
        error=reply_result.error,
    )


def _record_titles(result: Any) -> list[str]:
    """从 KbSearchResult 提取客服知识标题列表（商品知识不参与 hit@k）。"""
    titles: list[str] = []
    for rec in getattr(result, "customer_service_knowledge", []) or []:
        title = getattr(rec, "title", None)
        if title:
            titles.append(title)
    return titles


def run_all(
    cases: list[GoldenCase],
    *,
    session: Any,
    llm_client: Any,
    shop_id: int,
    config: Any = None,
    default_reply: str = "",
    kb_search: Callable[..., Any] | None = None,
    generate_reply: Callable[..., Any],
    retrieval_limit: int = 5,
    record_latency: bool = True,
) -> list[CaseOutcome]:
    """对全部用例执行评测（串行，保证可复现与确定性）。"""
    outcomes: list[CaseOutcome] = []
    for case in cases:
        outcomes.append(
            run_case(
                case,
                session=session,
                llm_client=llm_client,
                shop_id=shop_id,
                config=config,
                default_reply=default_reply,
                kb_search=kb_search,
                generate_reply=generate_reply,
                retrieval_limit=retrieval_limit,
                record_latency=record_latency,
            )
        )
    return outcomes


def outcomes_to_dicts(outcomes: list[CaseOutcome]) -> list[dict[str, Any]]:
    """将结果列表转为可 JSON 序列化的字典列表。"""
    return [o.to_dict() for o in outcomes]


__all__ = [
    "CaseOutcome",
    "run_case",
    "run_all",
    "outcomes_to_dicts",
    "RESULT_AI_REPLY",
    "RESULT_AI_REPLY_FAILED",
]
