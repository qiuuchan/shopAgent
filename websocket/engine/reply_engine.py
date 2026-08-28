# -*- coding: utf-8 -*-
"""
websocket.engine.reply_engine —— 自动回复决策链编排（纯逻辑）
============================================================
本文件用途：实现 websocket 自动回复引擎的「决策主流程编排」纯逻辑，按固定优先级
对一条客户消息做出回复决策，输出 ``ReplyDecision`` 纯数据结构，供上层（消息消费者）
据此发送回复 / 转人工 / 记录日志。本模块复用 engine 内已实现的纯逻辑组件：

- message_filter：黑名单判断（需求 12.4/12.5）与过滤规则命中（需求 12.2）；
- business_hours：营业时间判定（需求 11.2/11.3/11.4）；
- risk_control：风控频率限制判定与风控日志生成（需求 13.2/13.4）；
- keyword_matcher：关键词规则按优先级唯一命中（需求 6.2-6.5/6.7）。

固定优先级（对应 Property 13，design.md「自动回复决策优先级链」）：

    黑名单 / 过滤  →  非营业时间  →  风控  →  关键词  →  商品专属  →  AI  →  默认回复  →  无匹配规则

判定语义：
- 命中黑名单 → 不回复，决策 ``blacklisted``（需求 12.4，记「黑名单拦截」）；
- 命中过滤规则 → 不回复，决策 ``filtered``（需求 12.2，记「已过滤」）；
- 非营业时间 → 不回复，决策 ``off_hours``（需求 11.3，记「非营业时间」）；
- 触发风控频率上限 → 暂停回复，决策 ``risk_blocked``（需求 13.2，记风控日志「风控暂停」）；
- 命中关键词规则 → 决策 ``keyword``，返回该规则回复类型与内容（需求 6.3/6.5）；
- 命中商品专属回复 → 决策 ``goods_specific``，返回商品专属回复（需求 7.4，优先级高于默认回复）；
- AI 启用且前述均未命中 → 决策 ``ai``，交由 AI 回复引擎生成内容（需求 8.1）；
- 配置了默认回复 → 决策 ``default``，返回默认回复内容（需求 7.1）；
- 以上皆不满足 → 决策 ``no_match``，不发送回复（需求 7.2，记「无匹配规则」）。

设计要点：
- 纯逻辑、无 I/O：店铺配置、各类规则与运行时回复计数序列均由上层（从持久化层 /
  运行时状态）取出后传入；本模块仅做决策判定，不访问数据库、不调用 LLM、不发消息；
- AI 分支仅给出「应调用 AI」的决策（``action='ai'``），实际 LLM 调用与失败降级
  （需求 8.5，Property 14）由 AI 回复引擎（任务 13）执行，不在本模块内完成；
- ``ReplyDecision`` 为冻结 dataclass，``log_result`` 取值与数据字典 ``process_result``
  一致（common.services.dict_seed_data），便于上层落库与前端中文展示。

实现约束（开发规范）：
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）；
  文件名用下划线（规范 40）；全中文（规范 50）；复用 engine 已有组件（规范 52）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from engine.business_hours import TimeLike, is_within_business_hours
from engine.keyword_matcher import REPLY_TEXT, match_keyword
from engine.message_filter import is_blacklisted, match_filter_rules
from engine.risk_control import RiskLogData, check_reply_frequency

# ----------------------------------------------------------------------
# 决策动作常量（action）：与 design.md 约定的取值集合一致
# action ∈ {keyword, goods_specific, ai, default, filtered, blacklisted,
#           off_hours, risk_blocked, no_match}
# ----------------------------------------------------------------------
ACTION_KEYWORD: str = "keyword"  # 命中关键词规则
ACTION_GOODS_SPECIFIC: str = "goods_specific"  # 命中商品专属回复
ACTION_AI: str = "ai"  # 交由 AI 回复引擎生成
ACTION_DEFAULT: str = "default"  # 返回默认回复
ACTION_FILTERED: str = "filtered"  # 命中过滤规则，不回复
ACTION_BLACKLISTED: str = "blacklisted"  # 命中黑名单，不回复
ACTION_OFF_HOURS: str = "off_hours"  # 非营业时间，不回复
ACTION_RISK_BLOCKED: str = "risk_blocked"  # 触发风控，暂停回复
ACTION_NO_MATCH: str = "no_match"  # 无任何回复可用，不发送

# ----------------------------------------------------------------------
# 消息处理结果常量（log_result）：与数据字典 process_result 的 dict_key 对齐
# （common.services.dict_seed_data.DICT_SEED_DATA["process_result"]）
# ----------------------------------------------------------------------
RESULT_AUTO_REPLY: str = "auto_reply"  # 自动回复（关键词/商品专属/默认回复）
RESULT_AI_REPLY: str = "ai_reply"  # AI 回复
RESULT_FILTERED: str = "filtered"  # 已过滤
RESULT_BLACKLISTED: str = "blacklisted"  # 黑名单拦截
RESULT_NON_BUSINESS_HOURS: str = "non_business_hours"  # 非营业时间
RESULT_RISK_PAUSED: str = "risk_paused"  # 风控暂停
RESULT_NO_MATCH: str = "no_match"  # 无匹配规则


# ----------------------------------------------------------------------
# 店铺配置入参（纯数据结构）
# ----------------------------------------------------------------------
@dataclass
class ShopConfig:
    """决策所需的店铺级配置与运行时计数（由上层从持久化层 / 运行时状态取出后传入）。

    Attributes:
        shop_pk: 店铺主键 shop.id（写入风控日志）。
        business_enabled: 营业时间控制是否启用；False 视为全天营业（需求 11.4）。
        business_start: 营业开始时刻（time / "HH:MM" / "HH:MM:SS" / None）。
        business_end: 营业结束时刻（同上，可早于开始时刻表示跨午夜）。
        business_weekdays: 营业星期维度表达式（"1,2,3,4,5" 表示周一至周五）；
            None / 空串表示每天（兼容旧数据，行为不变）。
        risk_enabled: 风控是否启用；False 时不做频率限制（需求 13.2）。
        session_reply_limit: 单会话回复频率上限（None 表示不限制）。
        shop_reply_limit: 单店铺回复频率上限（None 表示不限制）。
        window_seconds: 风控统计窗口秒数（None 或 ≤0 表示不按时间窗口截断）。
        session_reply_times: 当前会话窗口内的历史回复时刻序列。
        shop_reply_times: 当前店铺窗口内的历史回复时刻序列。
        ai_enabled: AI 智能回复是否启用（需求 8.1）。
        default_reply_content: 默认回复内容（空 / None 视为未配置默认回复，需求 7.2）。
        default_reply_type: 默认回复类型（text 文本 / image 图片，默认 text）。
        default_reply_once: 默认回复是否「只回复一次」（同一客户仅发送一次，需求 7.1）。
        default_reply_already_sent: 当前客户在本店铺是否已收到过默认回复（由上层
            从「默认回复发送记录」查询后传入）；为 True 且开启只回复一次时跳过默认回复。
    """

    shop_pk: int = 0
    business_enabled: bool = True
    business_start: TimeLike = None
    business_end: TimeLike = None
    business_weekdays: Optional[str] = None
    risk_enabled: bool = True
    session_reply_limit: Optional[int] = None
    shop_reply_limit: Optional[int] = None
    window_seconds: Optional[int] = None
    session_reply_times: Optional[Iterable[datetime]] = None
    shop_reply_times: Optional[Iterable[datetime]] = None
    ai_enabled: bool = False
    default_reply_content: Optional[str] = None
    default_reply_type: str = REPLY_TEXT
    default_reply_once: bool = False
    default_reply_already_sent: bool = False


# ----------------------------------------------------------------------
# 各类规则入参（纯数据结构）
# ----------------------------------------------------------------------
@dataclass
class ReplyRules:
    """决策所需的各类规则集合（由上层从持久化层取出后传入）。

    元素均可为模型对象或 dict（由各匹配组件兼容处理）。

    Attributes:
        keyword_rules: 关键词规则集合（含 keyword/match_type/reply_type/
            reply_content/priority/enabled）。
        filter_rules: 消息过滤规则集合（含 condition_type/condition_value/enabled）。
        blacklist: 黑名单记录集合（含 customer_uid/is_active）。
        goods_replies: 商品专属回复集合（含 goods_id/reply_type/reply_content/enabled）。
    """

    keyword_rules: Optional[Iterable[Any]] = None
    filter_rules: Optional[Iterable[Any]] = None
    blacklist: Optional[Iterable[Any]] = None
    goods_replies: Optional[Iterable[Any]] = None


# ----------------------------------------------------------------------
# 决策结果（纯数据结构）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ReplyDecision:
    """自动回复引擎决策结果（纯数据结构，便于属性测试）。

    Attributes:
        action: 决策动作（见本模块 ACTION_* 常量）。
        reply_type: 回复类型（text 文本 / image 图片）；不发送回复时为 None。
        content: 回复内容（文本 / 图片地址）；不发送回复或交由 AI 生成时为 None。
        log_result: 消息处理结果（与数据字典 process_result 一致，供上层落库）。
        should_reply: 是否应当发送一条回复内容（仅关键词/商品专属/默认回复为 True；
            AI 分支需由 AI 引擎生成内容，故为 False）。
        matched_rule_id: 命中的规则 ID（关键词 / 过滤 / 商品专属），无则为 None。
        risk_log: 触发风控时生成的风控日志数据，否则为 None。
    """

    action: str
    reply_type: Optional[str] = None
    content: Optional[str] = None
    log_result: str = RESULT_NO_MATCH
    should_reply: bool = False
    matched_rule_id: Optional[int] = None
    risk_log: Optional[RiskLogData] = None


# ----------------------------------------------------------------------
# 上下文取值辅助：兼容 Context 对象与 dict 两种入参形态
# ----------------------------------------------------------------------
def _ctx_get(context: Any, key: str, default: Any = None) -> Any:
    """从 Context 对象或字典中读取字段值，二者通用。

    Args:
        context: Context 数据对象或字典。
        key: 字段名 / 键名。
        default: 缺失时的默认值。

    Returns:
        字段值；缺失返回 default。
    """
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


def _extract_text(context: Any) -> str:
    """从上下文提取用于关键词 / 过滤匹配的文本内容。

    仅文本类消息的 content 为字符串；商品咨询 / 规格 / 订单等消息 content 为结构化
    字典，对其文本匹配返回空串（不参与关键词 / 包含匹配）。

    Args:
        context: 消息上下文。

    Returns:
        文本内容；非文本消息返回空串。
    """
    content = _ctx_get(context, "content")
    return content if isinstance(content, str) else ""


def _extract_msg_type(context: Any) -> Optional[str]:
    """从上下文提取消息类型字符串（供过滤规则 msg_type 条件判断）。

    兼容 ContextType 枚举（取其 value）与普通字符串 / None。

    Args:
        context: 消息上下文。

    Returns:
        消息类型字符串；无法识别时返回 None。
    """
    msg_type = _ctx_get(context, "type")
    if msg_type is None:
        return None
    # ContextType 为 str 枚举，value 即字符串键；其它类型尽力转字符串
    return getattr(msg_type, "value", None) or str(msg_type)


def _extract_customer_uid(context: Any) -> Optional[str]:
    """从上下文提取客户唯一标识（用于黑名单判断）。

    客户为消息发送方，对应解析层 kwargs 中的 ``from_uid``；兼容上下文直接携带
    ``customer_uid`` / ``from_uid`` 字段的情况。

    Args:
        context: 消息上下文。

    Returns:
        客户标识；缺失返回 None。
    """
    # 优先从 kwargs 取 from_uid（pdd_message 解析口径）
    kwargs = _ctx_get(context, "kwargs")
    if isinstance(kwargs, dict):
        uid = kwargs.get("from_uid") or kwargs.get("customer_uid")
        if uid:
            return uid
    # 兼容上下文顶层直接携带的字段
    return _ctx_get(context, "customer_uid") or _ctx_get(context, "from_uid")


def _extract_goods_id(context: Any) -> Optional[str]:
    """从上下文提取关联商品 goods_id（用于商品专属回复匹配）。

    依次从商品上下文（商品咨询 / 规格）与订单上下文中提取 goods_id。

    Args:
        context: 消息上下文。

    Returns:
        商品 goods_id；缺失返回 None。
    """
    for ctx_key in ("goods_context", "order_context"):
        ctx_val = _ctx_get(context, ctx_key)
        if isinstance(ctx_val, dict):
            goods_id = ctx_val.get("goods_id")
            if goods_id is not None and goods_id != "":
                return str(goods_id)
    return None


def _match_goods_reply(goods_id: Optional[str], goods_replies: Optional[Iterable[Any]]):
    """在商品专属回复集合中匹配当前商品（需求 7.3/7.4）。

    仅启用（enabled=True）的商品专属回复参与匹配；按入参顺序返回首个 goods_id
    相等的启用规则；商品 goods_id 为空或集合为空一律不命中。

    Args:
        goods_id: 当前消息关联的商品 goods_id。
        goods_replies: 商品专属回复集合（元素为模型对象或 dict）。

    Returns:
        命中的商品专属回复（原对象 / dict）；未命中返回 None。
    """
    if not goods_id or not goods_replies:
        return None

    for item in goods_replies:
        enabled = item.get("enabled", True) if isinstance(item, dict) else getattr(item, "enabled", True)
        if not bool(enabled):
            continue
        item_goods_id = (
            item.get("goods_id") if isinstance(item, dict) else getattr(item, "goods_id", None)
        )
        if item_goods_id is not None and str(item_goods_id) == goods_id:
            return item
    return None


def _attr(item: Any, key: str, default: Any = None) -> Any:
    """从模型对象或 dict 读取字段值（商品专属回复字段提取用）。"""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


# ----------------------------------------------------------------------
# 决策主流程（需求 7.1/7.2/7.4/8.1/11.2/11.3/12.2/16.3，Property 13）
# ----------------------------------------------------------------------
def decide_reply(
    context: Any,
    shop_config: ShopConfig,
    rules: ReplyRules,
    *,
    now: Optional[datetime] = None,
) -> ReplyDecision:
    """按固定优先级对一条客户消息做出自动回复决策（Property 13）。

    优先级（任一环节命中即短路返回，不再向下判定）：
    黑名单 → 过滤 → 非营业时间 → 风控 → 关键词 → 商品专属 → AI → 默认回复 → 无匹配规则。

    Args:
        context: 客户消息上下文（Context 对象或 dict）。
        shop_config: 店铺级配置与运行时回复计数（ShopConfig）。
        rules: 各类规则集合（ReplyRules）。
        now: 参考时刻（北京时间口径）；默认取当前北京时间。可传入用于测试。

    Returns:
        决策结果 ReplyDecision。
    """
    rules = rules or ReplyRules()
    shop_config = shop_config or ShopConfig()

    text = _extract_text(context)
    msg_type = _extract_msg_type(context)
    customer_uid = _extract_customer_uid(context)

    # 1) 黑名单：命中则不回复（需求 12.4 / 12.5）
    if is_blacklisted(customer_uid, rules.blacklist):
        return ReplyDecision(
            action=ACTION_BLACKLISTED,
            log_result=RESULT_BLACKLISTED,
        )

    # 2) 过滤规则：命中则跳过自动回复（需求 12.2）
    filter_hit = match_filter_rules(text, rules.filter_rules, msg_type=msg_type)
    if filter_hit is not None:
        return ReplyDecision(
            action=ACTION_FILTERED,
            log_result=RESULT_FILTERED,
            matched_rule_id=filter_hit.rule_id,
        )

    # 3) 营业时间：非营业时间不发送自动回复（需求 11.2 / 11.3 / 11.4）
    if not is_within_business_hours(
        shop_config.business_start,
        shop_config.business_end,
        enabled=shop_config.business_enabled,
        weekdays=shop_config.business_weekdays,
        now=now,
    ):
        return ReplyDecision(
            action=ACTION_OFF_HOURS,
            log_result=RESULT_NON_BUSINESS_HOURS,
        )

    # 4) 风控频率：达上限则暂停回复并记风控日志（需求 13.2 / 13.4）
    freq = check_reply_frequency(
        shop_config.shop_pk,
        session_reply_times=shop_config.session_reply_times,
        shop_reply_times=shop_config.shop_reply_times,
        session_reply_limit=shop_config.session_reply_limit,
        shop_reply_limit=shop_config.shop_reply_limit,
        window_seconds=shop_config.window_seconds,
        enabled=shop_config.risk_enabled,
        now=now,
    )
    if freq.blocked:
        return ReplyDecision(
            action=ACTION_RISK_BLOCKED,
            log_result=RESULT_RISK_PAUSED,
            risk_log=freq.risk_log,
        )

    # 5) 关键词规则：命中则返回优先级最高规则的回复（需求 6.2-6.5）
    keyword_hit = match_keyword(text, rules.keyword_rules)
    if keyword_hit is not None:
        return ReplyDecision(
            action=ACTION_KEYWORD,
            reply_type=keyword_hit.reply_type,
            content=keyword_hit.reply_content,
            log_result=RESULT_AUTO_REPLY,
            should_reply=True,
            matched_rule_id=keyword_hit.rule_id,
        )

    # 6) 商品专属回复：命中则返回商品专属回复，优先级高于默认回复（需求 7.4）
    goods_id = _extract_goods_id(context)
    goods_reply = _match_goods_reply(goods_id, rules.goods_replies)
    if goods_reply is not None:
        return ReplyDecision(
            action=ACTION_GOODS_SPECIFIC,
            reply_type=_attr(goods_reply, "reply_type", REPLY_TEXT) or REPLY_TEXT,
            content=_attr(goods_reply, "reply_content", "") or "",
            log_result=RESULT_AUTO_REPLY,
            should_reply=True,
            matched_rule_id=_attr(goods_reply, "id"),
        )

    # 7) AI 启用：交由 AI 回复引擎生成内容（需求 8.1）
    if shop_config.ai_enabled:
        return ReplyDecision(
            action=ACTION_AI,
            log_result=RESULT_AI_REPLY,
            should_reply=False,
        )

    # 8) 默认回复：配置了默认回复则返回（需求 7.1）
    default_content = shop_config.default_reply_content
    if default_content is not None and str(default_content) != "":
        # 只回复一次：该客户在本店铺已收到过默认回复时不再发送（需求 7.1），
        # 视为无匹配规则、不发送回复。
        if shop_config.default_reply_once and shop_config.default_reply_already_sent:
            return ReplyDecision(
                action=ACTION_NO_MATCH,
                log_result=RESULT_NO_MATCH,
                should_reply=False,
            )
        return ReplyDecision(
            action=ACTION_DEFAULT,
            reply_type=shop_config.default_reply_type or REPLY_TEXT,
            content=default_content,
            log_result=RESULT_AUTO_REPLY,
            should_reply=True,
        )

    # 9) 无任何回复可用：不发送，记「无匹配规则」（需求 7.2）
    return ReplyDecision(
        action=ACTION_NO_MATCH,
        log_result=RESULT_NO_MATCH,
        should_reply=False,
    )


__all__ = [
    "ACTION_KEYWORD",
    "ACTION_GOODS_SPECIFIC",
    "ACTION_AI",
    "ACTION_DEFAULT",
    "ACTION_FILTERED",
    "ACTION_BLACKLISTED",
    "ACTION_OFF_HOURS",
    "ACTION_RISK_BLOCKED",
    "ACTION_NO_MATCH",
    "RESULT_AUTO_REPLY",
    "RESULT_AI_REPLY",
    "RESULT_FILTERED",
    "RESULT_BLACKLISTED",
    "RESULT_NON_BUSINESS_HOURS",
    "RESULT_RISK_PAUSED",
    "RESULT_NO_MATCH",
    "ShopConfig",
    "ReplyRules",
    "ReplyDecision",
    "decide_reply",
]
