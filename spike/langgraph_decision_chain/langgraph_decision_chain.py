# -*- coding: utf-8 -*-
"""
spike.langgraph_decision_chain —— LangGraph 复刻 9 级决策链（POL-010 spike）
==============================================================================
本 spike 用途：用 LangGraph StateGraph 重写「拼多多自动回复」的 9 级短路决策链，
与主项目自研 ``websocket.engine.reply_engine.decide_reply`` **双跑对拍**，产出
REPORT.md 定量对比（代码量 / 可测性 / 依赖成本 / 控制力），论证「主项目保持自研」
的依据。spike 不进服务代码、不打进主 pyproject（langgraph 仅装在本 spike venv）。

复刻口径：
- 节点 = 黑名单 / 过滤 / 营业时间 / 风控 / 关键词 / 商品专属 / AI / 默认回复 / 无匹配；
- 边 = 短路优先级（任一节点命中即终止，否则进入下一节点）；
- 复用主项目 `websocket.engine.*` 的**纯逻辑判定**（is_blacklisted / match_filter_rules /
  is_within_business_hours / check_reply_frequency / match_keyword / 商品专属匹配），
  与自研实现唯一差异是**编排方式**（StateGraph 显式节点 + 条件边 vs 顺序 if 短路）。

运行：本 spike 用独立 venv 执行，sys.path 注入仓库根以复用主项目纯逻辑。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

# 仓库根注入：复用主项目 websocket.engine.*（仅读取，不改）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
# websocket 目录单独注入：其模块之间以 ``from engine.xxx`` / ``from agent.xxx``
# 相对包名互导，需把 websocket/ 加入 sys.path 方可 import（spike 只读复用）。
_WS_DIR = os.path.join(_REPO_ROOT, "websocket")
if _WS_DIR not in sys.path:
    sys.path.insert(0, _WS_DIR)

from langgraph.graph import END, StateGraph

from websocket.engine.business_hours import is_within_business_hours
from websocket.engine.keyword_matcher import REPLY_TEXT, match_keyword
from websocket.engine.message_filter import is_blacklisted, match_filter_rules
from websocket.engine.reply_engine import (
    ACTION_AI,
    ACTION_BLACKLISTED,
    ACTION_DEFAULT,
    ACTION_FILTERED,
    ACTION_GOODS_SPECIFIC,
    ACTION_KEYWORD,
    ACTION_NO_MATCH,
    ACTION_OFF_HOURS,
    ACTION_RISK_BLOCKED,
    RESULT_AI_REPLY,
    RESULT_AUTO_REPLY,
    RESULT_BLACKLISTED,
    RESULT_FILTERED,
    RESULT_NON_BUSINESS_HOURS,
    RESULT_NO_MATCH,
    RESULT_RISK_PAUSED,
    ReplyDecision,
    _extract_customer_uid,
    _extract_goods_id,
    _extract_msg_type,
    _extract_text,
    _match_goods_reply,
    _attr,
)
from websocket.engine.risk_control import check_reply_frequency


# ----------------------------------------------------------------------
# 决策状态（StateGraph 共享的单一可变状态）
# ----------------------------------------------------------------------
class DecisionState(dict):
    """LangGraph 决策状态：封装 context / config / rules，产出 decision。

    用 dict 子类以兼容 LangGraph StateSchema（TypedDict 亦可，此处用 plain dict
    的关键字段约定 + 注解便于双实现对拍时复用同一辅助函数）。
    """

    context: Any
    shop_config: Any
    rules: Any
    now: Optional[Any]
    decision: Optional[ReplyDecision] = None


def _finish(state: dict) -> dict:
    """结束条件：已产出 decision 则终止。"""
    return {"__end__": True} if state.get("decision") is not None else {}


def _text(state: dict) -> str:
    return _extract_text(state["context"])


def _msg_type(state: dict) -> Optional[str]:
    return _extract_msg_type(state["context"])


def _uid(state: dict) -> Optional[str]:
    return _extract_customer_uid(state["context"])


# ----------------------------------------------------------------------
# 节点：按 9 级优先级定义为独立的纯函数节点（命中写 decision，未命中断言无产出）
# ----------------------------------------------------------------------
def node_blacklist(state: dict) -> dict:
    """节点 1：黑名单命中 → 不回复短路。"""
    rules = state["rules"]
    if is_blacklisted(_uid(state), rules.blacklist):
        state["decision"] = ReplyDecision(action=ACTION_BLACKLISTED, log_result=RESULT_BLACKLISTED)
    return state


def node_filter(state: dict) -> dict:
    """节点 2：过滤命中 → 不回复短路。"""
    hit = match_filter_rules(_text(state), state["rules"].filter_rules, msg_type=_msg_type(state))
    if hit is not None:
        state["decision"] = ReplyDecision(
            action=ACTION_FILTERED, log_result=RESULT_FILTERED, matched_rule_id=hit.rule_id
        )
    return state


def node_business_hours(state: dict) -> dict:
    """节点 3：非营业时间 → 不回复短路。"""
    cfg = state["shop_config"]
    if not is_within_business_hours(
        cfg.business_start, cfg.business_end, enabled=cfg.business_enabled,
        weekdays=cfg.business_weekdays, now=state["now"],
    ):
        state["decision"] = ReplyDecision(action=ACTION_OFF_HOURS, log_result=RESULT_NON_BUSINESS_HOURS)
    return state


def node_risk(state: dict) -> dict:
    """节点 4：风控达上限 → 暂停回复短路。"""
    cfg = state["shop_config"]
    freq = check_reply_frequency(
        cfg.shop_pk,
        session_reply_times=cfg.session_reply_times,
        shop_reply_times=cfg.shop_reply_times,
        session_reply_limit=cfg.session_reply_limit,
        shop_reply_limit=cfg.shop_reply_limit,
        window_seconds=cfg.window_seconds,
        enabled=cfg.risk_enabled,
        now=state["now"],
    )
    if freq.blocked:
        state["decision"] = ReplyDecision(
            action=ACTION_RISK_BLOCKED, log_result=RESULT_RISK_PAUSED, risk_log=freq.risk_log
        )
    return state


def node_keyword(state: dict) -> dict:
    """节点 5：关键词命中 → 返回该规则回复短路。"""
    hit = match_keyword(_text(state), state["rules"].keyword_rules)
    if hit is not None:
        state["decision"] = ReplyDecision(
            action=ACTION_KEYWORD, reply_type=hit.reply_type, content=hit.reply_content,
            log_result=RESULT_AUTO_REPLY, should_reply=True, matched_rule_id=hit.rule_id,
        )
    return state


def node_goods_specific(state: dict) -> dict:
    """节点 6：商品专属回复命中 → 返回（高于默认回复）。"""
    goods_id = _extract_goods_id(state["context"])
    reply = _match_goods_reply(goods_id, state["rules"].goods_replies)
    if reply is not None:
        state["decision"] = ReplyDecision(
            action=ACTION_GOODS_SPECIFIC,
            reply_type=_attr(reply, "reply_type", REPLY_TEXT) or REPLY_TEXT,
            content=_attr(reply, "reply_content", "") or "",
            log_result=RESULT_AUTO_REPLY, should_reply=True, matched_rule_id=_attr(reply, "id"),
        )
    return state


def node_ai(state: dict) -> dict:
    """节点 7：AI 启用 → 交 AI 引擎（不在此生成内容）。"""
    if state["shop_config"].ai_enabled:
        state["decision"] = ReplyDecision(action=ACTION_AI, log_result=RESULT_AI_REPLY, should_reply=False)
    return state


def node_default(state: dict) -> dict:
    """节点 8：默认回复（只回一次则跳过）。"""
    cfg = state["shop_config"]
    default_content = cfg.default_reply_content
    if default_content is not None and str(default_content) != "":
        if cfg.default_reply_once and cfg.default_reply_already_sent:
            state["decision"] = ReplyDecision(action=ACTION_NO_MATCH, log_result=RESULT_NO_MATCH, should_reply=False)
        else:
            state["decision"] = ReplyDecision(
                action=ACTION_DEFAULT,
                reply_type=cfg.default_reply_type or REPLY_TEXT,
                content=default_content,
                log_result=RESULT_AUTO_REPLY,
                should_reply=True,
            )
    return state


def node_no_match(state: dict) -> dict:
    """节点 9：无任何可用回复 → 不发送。"""
    state["decision"] = ReplyDecision(action=ACTION_NO_MATCH, log_result=RESULT_NO_MATCH, should_reply=False)
    return state


# ----------------------------------------------------------------------
# 条件边：当节点未产出 decision 时流向下一个节点；产出则终止至 END
# ----------------------------------------------------------------------
def _cond():
    def route(state: dict) -> str:
        return "done" if state.get("decision") is not None else "next"
    return route


def build_decision_graph():
    """构建 LangGraph StateGraph 版 9 级短路决策链。

    Returns:
        编译后的图（invoke 一次返回最终 state，含 decision）。
    """
    graph = StateGraph(dict)
    for name in (
        "blacklist", "filter", "business_hours", "risk", "keyword",
        "goods_specific", "ai", "default", "no_match",
    ):
        graph.add_node(name, globals()[f"node_{name}"])

    graph.set_entry_point("blacklist")
    order = [
        "blacklist", "filter", "business_hours", "risk", "keyword",
        "goods_specific", "ai", "default", "no_match",
    ]
    for src, nxt in zip(order, order[1:]):
        graph.add_conditional_edges(src, _cond(), {"done": END, "next": nxt})
    # 最后一个节点：无论命中与否都终止。
    graph.add_conditional_edges("no_match", _cond(), {"done": END, "next": END})
    return graph.compile()


def decide_reply_langgraph(
    context: Any,
    shop_config: Any,
    rules: Any,
    *,
    now: Any = None,
) -> ReplyDecision:
    """LangGraph 版 decide_reply：与主项目自研版同签名、同语义。

    Args:
        context: 客户消息上下文。
        shop_config: 店铺配置（ShopConfig）。
        rules: 规则集合（ReplyRules）。
        now: 参考时刻（默认 None，转交底层）。

    Returns:
        ReplyDecision 决策结果。
    """
    compiled = build_decision_graph()
    state = {"context": context, "shop_config": shop_config, "rules": rules, "now": now, "decision": None}
    final = compiled.invoke(state)
    decision = final.get("decision")
    # 兜底：理论上 no_match 必产出；此处防御性兜底。
    if decision is None:
        decision = ReplyDecision(action=ACTION_NO_MATCH, log_result=RESULT_NO_MATCH, should_reply=False)
    return decision


__all__ = ["decide_reply_langgraph", "build_decision_graph"]
