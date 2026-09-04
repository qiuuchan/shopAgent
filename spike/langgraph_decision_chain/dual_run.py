# -*- coding: utf-8 -*-
"""
spike.langgraph_decision_chain.dual_run —— 自研 vs LangGraph 双跑对拍（POL-010）
================================================================================
本脚本用途：从 `websocket/tests/test_reply_engine.py` 抽取的 decide_reply 用例集，
对「自研顺序短路实现」与「LangGraph StateGraph 版」**双跑**，断言两实现决策结果
（action / log_result / content / should_reply / matched_rule_id）完全一致。

运行（使用本 spike venv，已装 langgraph + tzdata）：
    spike/langgraph_decision_chain/.venv/Scripts/python dual_run.py

输出：对每条用例打印两实现结果是否一致；全部一致则退出码 0。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_WS_DIR = os.path.join(_REPO_ROOT, "websocket")
if _WS_DIR not in sys.path:
    sys.path.insert(0, _WS_DIR)

from websocket.engine.keyword_matcher import MATCH_CONTAINS, REPLY_IMAGE, REPLY_TEXT  # noqa: E402
from websocket.engine.message_filter import CONDITION_CONTAINS  # noqa: E402
from websocket.engine.reply_engine import ReplyRules, ShopConfig, decide_reply  # noqa: E402
from spike.langgraph_decision_chain.langgraph_decision_chain import (  # noqa: E402
    decide_reply_langgraph,
)

NOW = datetime(2024, 1, 1, 10, 0, 0)


@dataclass
class Case:
    """一条双跑用例：提供 context / shop_config / rules / now。"""

    name: str
    context: list[Any] | None = None  # 便于构造 reuse
    shop_config_ctor: Callable[[], Any] | None = None
    rules_ctor: Callable[[], Any] | None = None
    now: datetime = NOW


def _ctx(text="你好", *, from_uid="cust_1", goods_id=None, msg_type="text"):
    goods_context = {"goods_id": goods_id} if goods_id is not None else None
    return {
        "type": msg_type,
        "content": text,
        "goods_context": goods_context,
        "order_context": None,
        "kwargs": {"from_uid": from_uid},
    }


def _kw_rule(keyword="你好", reply="关键词回复", priority=1, rid=1):
    return {
        "id": rid, "keyword": keyword, "match_type": MATCH_CONTAINS, "reply_type": REPLY_TEXT,
        "reply_content": reply, "priority": priority, "enabled": True,
    }


def _filter_rule(value="你好", rid=10):
    return {"id": rid, "condition_type": CONDITION_CONTAINS, "condition_value": value, "enabled": True}


def _blacklist(uid="cust_1"):
    return [{"customer_uid": uid, "is_active": True}]


def _goods_reply(goods_id="g1", reply="商品专属回复", rid=20):
    return {"id": rid, "goods_id": goods_id, "reply_type": REPLY_TEXT, "reply_content": reply, "enabled": True}


def _cases() -> list[Case]:
    """构造覆盖全部 9 级优先级与边界变体的双跑用例集。"""
    c = []

    # 1) 黑名单短路（需求 12.4）
    c.append(Case("blacklist_wins", shop_config_ctor=lambda: ShopConfig(shop_pk=1),
                  rules_ctor=lambda: ReplyRules(keyword_rules=[_kw_rule()], filter_rules=[_filter_rule()], blacklist=_blacklist("cust_1"))))
    # 2) 过滤 > 关键词
    c.append(Case("filter_wins", shop_config_ctor=lambda: ShopConfig(shop_pk=1),
                  rules_ctor=lambda: ReplyRules(keyword_rules=[_kw_rule()], filter_rules=[_filter_rule("你好")])))
    # 3) 非营业时间 > 关键词
    c.append(Case("off_hours", shop_config_ctor=lambda: ShopConfig(shop_pk=1, business_enabled=True, business_start="08:00", business_end="09:00"),
                  rules_ctor=lambda: ReplyRules(keyword_rules=[_kw_rule()])))
    # 3b) 营业时间内继续
    c.append(Case("within_hours", shop_config_ctor=lambda: ShopConfig(shop_pk=1, business_enabled=True, business_start="08:00", business_end="12:00"),
                  rules_ctor=lambda: ReplyRules(keyword_rules=[_kw_rule()])))
    # 4) 风控 > 关键词（达上限）
    c.append(Case("risk_blocked", shop_config_ctor=lambda: ShopConfig(shop_pk=7, risk_enabled=True, session_reply_limit=2, window_seconds=60,
                  session_reply_times=[NOW - timedelta(seconds=10), NOW - timedelta(seconds=20)]),
                  rules_ctor=lambda: ReplyRules(keyword_rules=[_kw_rule()])))
    # 4b) 风控未达上限继续
    c.append(Case("risk_not_exceeded", shop_config_ctor=lambda: ShopConfig(shop_pk=7, risk_enabled=True, session_reply_limit=5, window_seconds=60,
                  session_reply_times=[NOW - timedelta(seconds=10)]),
                  rules_ctor=lambda: ReplyRules(keyword_rules=[_kw_rule()])))
    # 5) 关键词 > 商品专属/AI/默认
    c.append(Case("keyword_over_all", shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=True, default_reply_content="默认"),
                  rules_ctor=lambda: ReplyRules(keyword_rules=[_kw_rule(reply="关键词内容")], goods_replies=[_goods_reply("g1")])))
    c.append(Case("keyword_image", shop_config_ctor=lambda: ShopConfig(shop_pk=1),
                  rules_ctor=lambda: ReplyRules(keyword_rules=[_kw_rule(reply="https://img/a.png")])))
    # 6) 商品专属 > AI/默认
    c.append(Case("goods_specific", context=_ctx("随便问问", goods_id="g1"),
                  shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=True, default_reply_content="默认"),
                  rules_ctor=lambda: ReplyRules(goods_replies=[_goods_reply("g1", reply="专属内容", rid=20)])))
    c.append(Case("goods_specific_miss", context=_ctx("随便", goods_id="g2"),
                  shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=False, default_reply_content="默认"),
                  rules_ctor=lambda: ReplyRules(goods_replies=[_goods_reply("g1")])))
    # 7) AI > 默认
    c.append(Case("ai_wins", shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=True, default_reply_content="默认"),
                  rules_ctor=lambda: ReplyRules()))
    # 8) 默认回复兜底
    c.append(Case("default_fallback", shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=False, default_reply_content="默认回复内容"),
                  rules_ctor=lambda: ReplyRules()))
    # 9) 无匹配（no_match）
    c.append(Case("no_match", shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=False, default_reply_content=None),
                  rules_ctor=lambda: ReplyRules()))
    c.append(Case("empty_default_no_match", shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=False, default_reply_content=""),
                  rules_ctor=lambda: ReplyRules()))
    # 8b) 默认只回一次：已发送跳过
    c.append(Case("default_once_skip", shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=False, default_reply_content="默认", default_reply_once=True, default_reply_already_sent=True),
                  rules_ctor=lambda: ReplyRules()))
    # 8c) 默认只回一次：未发送则发送
    c.append(Case("default_once_send", shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=False, default_reply_content="默认", default_reply_once=True, default_reply_already_sent=False),
                  rules_ctor=lambda: ReplyRules()))
    # 8d) 未开启只回一次：有发送记录仍发送
    c.append(Case("default_no_once_send", shop_config_ctor=lambda: ShopConfig(shop_pk=1, ai_enabled=False, default_reply_content="默认", default_reply_once=False, default_reply_already_sent=True),
                  rules_ctor=lambda: ReplyRules()))
    return c


def _normalize(decision) -> dict[str, Any]:
    """抽取决策结果的可对拍字段（用于一致性命中判定）。"""
    return {
        "action": getattr(decision, "action", None),
        "log_result": getattr(decision, "log_result", None),
        "content": getattr(decision, "content", None),
        "reply_type": getattr(decision, "reply_type", None),
        "should_reply": getattr(decision, "should_reply", None),
        "matched_rule_id": getattr(decision, "matched_rule_id", None),
    }


def main() -> int:
    """对拍全部用例，输出任一不一致即退出码 1。"""
    failures = 0
    for case in _cases():
        ctx = case.context if case.context is not None else _ctx()
        cfg = case.shop_config_ctor() if case.shop_config_ctor else ShopConfig(shop_pk=1)
        rules = case.rules_ctor() if case.rules_ctor else ReplyRules()
        own = _normalize(decide_reply(ctx, cfg, rules, now=case.now))
        lg = _normalize(decide_reply_langgraph(ctx, cfg, rules, now=case.now))
        ok = own == lg
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{status}] {case.name}: own={own['action']} lg={lg['action']}")
    total = len(_cases())
    print(f"\n对拍完成：{total} 例，不一致 {failures} 例。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
