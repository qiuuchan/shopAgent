# -*- coding: utf-8 -*-
"""
websocket.engine.message_consumer —— 消息处理全链路消费器（端到端串联）
======================================================================
本文件用途：将 websocket 服务已实现的各组件串成一条端到端的消息处理链
（任务 19.2），确保「无孤立未集成代码」。处理链覆盖（与 design.md「消息处理
时序」一致）：

    PDDChannel 收消息（10.4） → pdd_message 解析为 Context（10.10） →
    FIFO 队列按序消费（10.6） → 转人工判定（16.3） →
    reply_engine.decide_reply 决策链（12.8，复用 keyword_matcher /
    business_hours / message_filter / risk_control 12.x） →
    知识库 kb.search（6.9）/ AI 回复引擎（13.1） →
    SendMessage 发送回复（13.5）/ 商品卡片签名降级（13.3） →
    记消息日志 / 风控日志 / 通知（经 common 落库，必要时 HTTP 调 backend）

设计要点（健壮性 + 可测）：
- 决策链、AI、降级、转人工均复用既有纯逻辑 / 服务组件，本模块仅做「编排」，
  不重复实现任何匹配 / 检索 / 发送逻辑（规范 36 / 52）。
- 规则与配置经 common 仓储（参数化查询，规范 16）从数据库读出后传入决策链；
  消息日志 / 风控日志经 common 仓储落库（design：common → MySQL）。
- 运行时回复频率计数在消费器内以「会话 / 店铺」维度维护，供风控判定使用。
- 关键依赖（runtime 加载器 / 发送器 / 转人工服务 / 日志写入器 / 通知器）均可
  注入，缺省走真实实现，便于端到端测试以桩替换外部副作用。
- 任一环节异常均被捕获并落系统日志，不中断后续消息处理（需求 16.5 / 26）。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、单文件 ≤500 行（35）、
文件名用下划线（40）、全中文（50）、日志禁用 debug（38）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from agent import ai_reply_engine
from agent.agent_config import AgentConfig
from agent.goods_card_fallback import build_goods_text, resolve_goods_card_reply
from channel_pdd.pdd_message import Context, ContextType
from channel_pdd.chat_event_forwarder import forward_new_message
from sqlalchemy import or_, select
from common.db.repository import Repository, run_in_session
from common.models.config_models import (
    Blacklist,
    BusinessHours,
    LlmConfig,
    MessageFilterRule,
    RiskRule,
    TransferKeyword,
)
from common.models.log_models import ChatMessage, Conversation, MessageLog, RiskLog
from common.models.reply_models import (
    DefaultReply,
    DefaultReplyRecord,
    GoodsReply,
    KeywordRule,
)
from common.utils.time_utils import now_beijing, now_beijing_naive
from engine.keyword_matcher import REPLY_IMAGE, REPLY_TEXT
from engine.reply_engine import (
    ACTION_AI,
    ACTION_BLACKLISTED,
    ACTION_DEFAULT,
    ACTION_FILTERED,
    ACTION_GOODS_SPECIFIC,
    ACTION_KEYWORD,
    ACTION_NO_MATCH,
    ACTION_OFF_HOURS,
    ACTION_RISK_BLOCKED,
    ReplyDecision,
    ReplyRules,
    ShopConfig,
    decide_reply,
)

logger = logging.getLogger("websocket.engine.message_consumer")

# 决策动作 / 转人工 的中文说明（仅用于日志可读性展示，便于排查触发了哪种回复）。
_ACTION_LABELS: Dict[str, str] = {
    ACTION_KEYWORD: "关键词回复",
    ACTION_GOODS_SPECIFIC: "商品专属回复",
    ACTION_AI: "AI 回复",
    ACTION_DEFAULT: "默认回复",
    ACTION_FILTERED: "命中过滤规则(不回复)",
    ACTION_BLACKLISTED: "命中黑名单(不回复)",
    ACTION_OFF_HOURS: "非营业时间(不回复)",
    ACTION_RISK_BLOCKED: "触发风控(暂停回复)",
    ACTION_NO_MATCH: "无匹配规则(不回复)",
    "transferred": "转人工",
}


def _action_label(action: Optional[str]) -> str:
    """返回决策动作的中文说明（未知动作回退原值，便于日志可读）。"""
    if action is None:
        return "无"
    return _ACTION_LABELS.get(action, action)

# 可触发自动回复处理的客户消息类型（其余类型仅记录 / 跳过，不进入决策链）。
_ELIGIBLE_TYPES = (
    ContextType.TEXT,
    ContextType.GOODS_INQUIRY,
    ContextType.GOODS_SPEC,
    ContextType.ORDER_INFO,
)

# 运行时回复时刻的保留期（秒）：超过该期限的历史时刻在记录新回复时被裁剪，
# 避免会话 / 店铺回复时间序列只增不减导致内存泄漏。该值（24 小时）远大于任何
# 合理的风控统计窗口，故不影响频率判定结果。
_REPLY_RETENTION_SECONDS: int = 24 * 3600

# 处理结果常量（与数据字典 process_result 对齐，转人工记「已转人工」）。
RESULT_TRANSFERRED: str = "transferred"

# 会话历史相关默认值：传给 AI 的最近历史消息条数上限（不含当前消息）。
# 条数过多会增加 token 消耗与延迟，默认取最近 10 条（按时间正序）。
_AI_HISTORY_MAX_MESSAGES: int = 10

# 消息方向常量（与 chat_message.direction 字段约定一致：in=收 / out=发）。
_DIRECTION_IN: str = "in"
_DIRECTION_OUT: str = "out"


@dataclass
class ShopRuntime:
    """店铺运行时配置与规则集合（一次从数据库读出，供决策链与 AI 使用）。

    Attributes:
        shop_config: 决策链所需的店铺级配置（营业 / 风控 / AI / 默认回复）。
        rules: 各类规则集合（关键词 / 过滤 / 黑名单 / 商品专属）。
        agent_config: AI 运行时配置（AI 未启用 / 无配置时为 None）。
        transfer_keywords: 启用的转人工关键词列表。
    """

    shop_config: ShopConfig
    rules: ReplyRules
    agent_config: Optional[AgentConfig] = None
    transfer_keywords: List[str] = field(default_factory=list)


@dataclass
class ProcessOutcome:
    """单条消息处理结果（便于测试断言与上层观测）。

    Attributes:
        handled: 是否进入了自动回复处理（非客户消息 / 不可处理类型为 False）。
        action: 决策动作（reply_engine 的 ACTION_*）；转人工为「transferred」。
        replied: 是否实际发送了一条回复内容。
        transferred: 是否触发转人工。
        downgraded: 商品卡片是否因签名缺失降级为文本。
        log_result: 落库的处理结果（process_result）。
        content: 实际发送的回复内容（未发送为 None）。
    """

    handled: bool = False
    action: Optional[str] = None
    replied: bool = False
    transferred: bool = False
    downgraded: bool = False
    log_result: Optional[str] = None
    content: Optional[str] = None


# ----------------------------------------------------------------------
# 取值辅助
# ----------------------------------------------------------------------
def _attr(item: Any, key: str, default: Any = None) -> Any:
    """从模型对象或字典读取字段值（二者通用）。"""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


# ----------------------------------------------------------------------
# 店铺运行时配置加载（经 common 仓储参数化读取）
# ----------------------------------------------------------------------
def load_shop_runtime(shop_pk: int, session: Any) -> ShopRuntime:
    """从数据库读取店铺规则与配置，组装为决策链可用的 ShopRuntime。

    仅取启用项参与运行时判定（关键词 / 过滤 / 商品专属 enabled=True、黑名单
    is_active=True）；LlmConfig 优先取店铺级，缺失时回退全局（shop_pk 为空）。

    Args:
        shop_pk: 店铺主键 shop.id。
        session: 事务性会话（由外层 run_in_session 管理）。

    Returns:
        组装好的 ShopRuntime。
    """
    keyword_rules = Repository(KeywordRule, session).list(
        filters={"shop_pk": shop_pk, "enabled": True}, order_by=False
    )
    filter_rules = Repository(MessageFilterRule, session).list(
        filters={"shop_pk": shop_pk, "enabled": True}, order_by=False
    )
    blacklist = Repository(Blacklist, session).list(
        filters={"shop_pk": shop_pk, "is_active": True}, order_by=False
    )
    goods_replies = Repository(GoodsReply, session).list(
        filters={"shop_pk": shop_pk, "enabled": True}, order_by=False
    )
    business = Repository(BusinessHours, session).get_by(shop_pk=shop_pk)
    risk = Repository(RiskRule, session).get_by(shop_pk=shop_pk)
    default_reply = Repository(DefaultReply, session).get_by(
        shop_pk=shop_pk, enabled=True
    )
    transfer_rows = Repository(TransferKeyword, session).list(
        filters={"shop_pk": shop_pk, "enabled": True}, order_by=False
    )

    # LLM 配置：优先店铺级，缺失回退全局（shop_pk 为空）
    llm = Repository(LlmConfig, session).get_by(shop_pk=shop_pk)
    if llm is None:
        llm = Repository(LlmConfig, session).get_by(shop_pk=None)
    agent_config = AgentConfig.from_llm_config(llm) if llm is not None else None
    ai_enabled = bool(agent_config.ai_enabled) if agent_config is not None else False

    shop_config = ShopConfig(
        shop_pk=shop_pk,
        business_enabled=bool(_attr(business, "enabled", True)) if business else True,
        business_start=_attr(business, "start_time") if business else None,
        business_end=_attr(business, "end_time") if business else None,
        business_weekdays=_attr(business, "weekdays") if business else None,
        risk_enabled=bool(_attr(risk, "enabled", True)) if risk else False,
        session_reply_limit=_attr(risk, "session_reply_limit") if risk else None,
        shop_reply_limit=_attr(risk, "shop_reply_limit") if risk else None,
        window_seconds=_attr(risk, "window_seconds") if risk else None,
        ai_enabled=ai_enabled,
        default_reply_content=_attr(default_reply, "content") if default_reply else None,
        default_reply_once=bool(_attr(default_reply, "reply_once", False)) if default_reply else False,
    )

    rules = ReplyRules(
        keyword_rules=list(keyword_rules),
        filter_rules=list(filter_rules),
        blacklist=list(blacklist),
        goods_replies=list(goods_replies),
    )
    transfer_keywords = [r.keyword for r in transfer_rows if getattr(r, "keyword", None)]

    return ShopRuntime(
        shop_config=shop_config,
        rules=rules,
        agent_config=agent_config,
        transfer_keywords=transfer_keywords,
    )


# 类型别名：运行时加载器 / 日志写入器 / 通知器（均可注入便于测试）。
RuntimeLoader = Callable[[int], ShopRuntime]
LogWriter = Callable[[Dict[str, Any]], None]
RiskLogWriter = Callable[[Dict[str, Any]], None]
Notifier = Callable[[str, str], None]
# 聊天消息落库写入器：入参为一条 chat_message 字段字典（含会话 upsert 所需信息）。
ChatMessageWriter = Callable[[Dict[str, Any]], None]
# 会话历史读取器：入参为 (shop_pk, customer_uid, limit)，返回按时间正序的历史消息
# 字典列表（每项含 direction / content）。
ChatHistoryReader = Callable[[int, str, int], List[Dict[str, Any]]]
# 消息解析器：把一条原始报文解析为统一 Context；无法解析返回 None（TIK-008）。
# 类型定义为字符串注解以规避模块级对 engine.message_parser 的硬依赖（按需惰性导入）。
MessageParser = "Callable[[Any], Optional[Context]]"
# 默认回复「只回复一次」记录读 / 写器：入参为 (shop_pk, customer_uid)。
# 读器返回该客户在本店铺是否已收到过默认回复；写器登记一次「已回复」事实。
DefaultReplyRecordReader = Callable[[int, str], bool]
DefaultReplyRecordWriter = Callable[[int, str], None]


class MessageConsumer:
    """消息处理全链路消费器：把一条原始报文从解析处理到回复发出 / 落库。

    每个店铺连接对应一个消费器实例；从 PDDChannel 的消息队列取出原始报文后，
    调用 ``consume_raw`` 完成解析与端到端处理。
    """

    def __init__(
        self,
        shop_id: str,
        shop_pk: int,
        user_id: int,
        *,
        channel_name: str = "pinduoduo",
        runtime_loader: Optional[RuntimeLoader] = None,
        sender: Optional["SendMessage"] = None,
        transfer_service: Optional["TransferService"] = None,
        message_parser: "Optional[MessageParser]" = None,
        log_writer: Optional[LogWriter] = None,
        risk_log_writer: Optional[RiskLogWriter] = None,
        notifier: Optional[Notifier] = None,
        default_reply_record_reader: Optional[DefaultReplyRecordReader] = None,
        default_reply_record_writer: Optional[DefaultReplyRecordWriter] = None,
        chat_message_writer: Optional[ChatMessageWriter] = None,
        chat_history_reader: Optional[ChatHistoryReader] = None,
        product_context_reader: Optional[
            "Callable[[int, str], Optional[Dict[str, Any]]]"
        ] = None,
    ) -> None:
        """构造消费器。

        Args:
            shop_id: 拼多多店铺业务标识。
            shop_pk: 店铺主键 shop.id（落库与决策用）。
            user_id: 归属用户 ID。
            channel_name: 渠道名称（默认 pinduoduo）。
            runtime_loader: 运行时配置加载器（缺省经 common 仓储从库读取）。
            sender: 拼多多消息发送器（缺省自建 SendMessage）。
            transfer_service: 转人工服务（缺省自建 TransferService）。
            message_parser: 原始报文解析器（缺省惰性取 pdd_parse_raw，保持 PDD
                默认行为；TikTok 等平台可注入各自解析器复用本消费链路，TIK-008）。
            log_writer: 消息日志写入器（缺省经 common 仓储落库）。
            risk_log_writer: 风控日志写入器（缺省经 common 仓储落库）。
            notifier: 系统事件通知器（缺省尽力而为调 backend，失败不影响主流程）。
            default_reply_record_reader: 默认回复「只回复一次」记录读取器（缺省
                经 common 仓储查询客户是否已收到过默认回复）。
            default_reply_record_writer: 默认回复「只回复一次」记录写入器（缺省
                经 common 仓储登记客户已收到默认回复）。
        """
        self.shop_id = shop_id
        self.shop_pk = shop_pk
        self.user_id = user_id
        self.channel_name = channel_name

        self._runtime_loader = runtime_loader or self._default_runtime_loader
        self._sender = sender
        self._transfer_service = transfer_service
        # 消息解析器：缺省惰性取 PDD 解析器（避免模块级硬依赖，确保启动时序与
        # 原有行为一致；注入非空 parser 时优先使用，供 TikTok 等平台复用全链路）。
        self._message_parser = message_parser
        self._log_writer = log_writer or self._default_log_writer
        self._risk_log_writer = risk_log_writer or self._default_risk_log_writer
        self._notifier = notifier
        self._default_reply_record_reader = (
            default_reply_record_reader or self._default_record_reader
        )
        self._default_reply_record_writer = (
            default_reply_record_writer or self._default_record_writer
        )
        self._chat_message_writer = (
            chat_message_writer or self._default_chat_message_writer
        )
        self._chat_history_reader = (
            chat_history_reader or self._default_chat_history_reader
        )
        # 全历史商品 / 订单上下文读取器：用于 AI 取最新商品背景（不受最近 N 条限制）。
        self._product_context_reader = (
            product_context_reader or self._default_product_context_reader
        )

        # 运行时回复频率计数：会话维度（按客户 uid）与店铺维度，供风控判定。
        self._session_reply_times: Dict[str, List[datetime]] = defaultdict(list)
        self._shop_reply_times: List[datetime] = []

        # 在途的实时推送转发任务集合：持有强引用，避免 asyncio.create_task 创建的
        # 任务在完成前被 GC 回收而静默丢失推送（fire-and-forget 安全登记）。
        self._forward_tasks: set = set()

    # ------------------------------------------------------------------
    # 入口：消费一条原始报文
    # ------------------------------------------------------------------
    async def consume_raw(self, raw_message: Any) -> ProcessOutcome:
        """解析并处理一条来自 WebSocket 的原始报文（消息队列消费回调）。

        单次解析后分流（避免重复解析）：
        - 客服发出的消息（mall_cs，含其它端 / 官方后台 / 本系统回推）：实时转发给
          前端展示（out 方向），不进入自动回复链路；
        - 客户发来的可自动回复类型消息（user + eligible type）：走端到端自动回复
          处理（process 内含 in 方向实时转发 + 落库）；
        - 其它（系统消息 / 非可处理类型）：忽略。

        Args:
            raw_message: 原始报文（JSON 字符串 / 字节 / 已解析字典 / Context）。

        Returns:
            处理结果 ProcessOutcome（非客户可处理消息时 handled=False）。
        """
        try:
            context = self._to_context(raw_message)
        except Exception as exc:  # noqa: BLE001 - 解析异常不中断后续消息
            logger.error("消息解析失败: shop_id=%s, %s", self.shop_id, exc)
            return ProcessOutcome(handled=False)

        if context is None:
            return ProcessOutcome(handled=False)

        from_user = context.kwargs.get("from_user")

        # 客服发出的消息：实时转发 out 展示（不落库、不走自动回复，需求 14）。
        if from_user == "mall_cs":
            customer_uid = context.kwargs.get("to_uid")
            if customer_uid is not None:
                text = context.content if isinstance(context.content, str) else None
                self._forward_chat_event(
                    context,
                    customer_uid,
                    self._resolve_log_content(context, text),
                    direction=_DIRECTION_OUT,
                )
            return ProcessOutcome(handled=False)

        # 仅处理客户（role=user）发来的可自动回复类型消息。
        if from_user is not None and from_user != "user":
            return ProcessOutcome(handled=False)
        if context.type not in _ELIGIBLE_TYPES:
            return ProcessOutcome(handled=False)
        return await self.process(context)

    def _to_context(self, raw_message: Any) -> Optional[Context]:
        """将原始报文解析为 Context（不做业务过滤），无法解析时返回 None。

        委托给注入的 ``message_parser``（TIK-008）：缺省惰性取 ``pdd_parse_raw``，
        保持与原 ``_to_context`` 逐字节等价的 PDD 默认行为。解析器契约
        （Context 直传 / 字节解码 / JSON 解析 / 非字典返回 None / 角色归一化）
        见 ``engine.message_parser``。

        Args:
            raw_message: 原始报文（Context / 字节 / JSON 字符串 / 字典）。

        Returns:
            解析得到的 Context；非字典 / 无法解析返回 None。
        """
        parser = self._message_parser or self._default_message_parser()
        return parser(raw_message)

    def _default_message_parser(self) -> "MessageParser":
        """惰性获取 PDD 默认消息解析器（避免模块级对 pdd_parse_raw 的硬依赖）。

        返回一个绑定了本店铺 ``shop_id`` 的解析器闭包，语义与原 ``_to_context``
        逐字节等价（角色归一化由 ``PDDChatMessage`` 完成）。

        Returns:
            一个 PDD 解析器（``Callable[[Any], Optional[Context]]``）。
        """
        from engine.message_parser import pdd_parse_raw

        shop_id = self.shop_id

        def _parse(raw_message: Any) -> Optional[Context]:
            return pdd_parse_raw(raw_message, shop_id=shop_id)

        return _parse

    # ------------------------------------------------------------------
    # 处理：转人工 → 决策链 → AI/降级 → 发送 → 落库
    # ------------------------------------------------------------------
    async def process(self, context: Context) -> ProcessOutcome:
        """对一条客户消息执行端到端处理（决策 + 发送 + 记日志）。

        Args:
            context: 客户消息上下文。

        Returns:
            处理结果 ProcessOutcome。
        """
        customer_uid = context.kwargs.get("from_uid")
        text = context.content if isinstance(context.content, str) else None
        # 落库用的消息内容：结构化消息（商品 / 订单）无纯文本时回退为可读摘要，
        # 供消息日志 message_content 与聊天记录 content 复用，避免内容为空。
        log_content = self._resolve_log_content(context, text)

        # 0) 落库：记录客户发来的这条消息到聊天记录（in 方向），并 upsert 会话、
        # 累加未读数。携带商品 / 订单上下文，供在线聊天展示与 AI 会话上下文使用。
        self._record_inbound_message(context, customer_uid, text)

        # 0.1) 实时推送：把客户新消息转发给 backend，由其广播给订阅了本店铺的浏览器
        # WebSocket（在线聊天实时显示，需求 14，方案 2）。fire-and-forget，失败不影响
        # 后续处理（转发器内部已兜底，不抛异常）。
        self._forward_chat_event(context, customer_uid, log_content)

        # 1) 加载店铺运行时配置与规则
        try:
            runtime = self._runtime_loader(self.shop_pk)
        except Exception as exc:  # noqa: BLE001 - 配置加载失败安全降级
            logger.error("加载店铺运行时配置失败: shop_id=%s, %s", self.shop_id, exc)
            return ProcessOutcome(handled=True, action=None)

        # 2) 转人工判定（命中关键词 / AI 判定需人工 → 转人工并暂停自动回复，需求 16.3）
        if self._should_transfer(text, runtime.transfer_keywords):
            return await self._do_transfer(customer_uid, text)

        # 3) 注入运行时回复频率计数（供风控判定，需求 13.2）
        runtime.shop_config.session_reply_times = list(
            self._session_reply_times.get(str(customer_uid), [])
        )
        runtime.shop_config.shop_reply_times = list(self._shop_reply_times)

        # 3.1) 注入默认回复「只回复一次」状态：仅在开启时查询该客户是否已收到过
        # 默认回复，避免无谓的数据库访问（需求 7.1）。
        if runtime.shop_config.default_reply_once and customer_uid is not None:
            runtime.shop_config.default_reply_already_sent = (
                self._has_default_reply_sent(customer_uid)
            )
        else:
            runtime.shop_config.default_reply_already_sent = False

        # 4) 决策链（黑名单/过滤 → 非营业 → 风控 → 关键词 → 商品专属 → AI → 默认 → 无匹配）
        decision = decide_reply(context, runtime.shop_config, runtime.rules)

        # 决策完成：打印命中的回复类型，便于排查每条消息触发了哪种处理（需求可观测性）。
        logger.info(
            "消息决策: shop_id=%s, 客户=%s, 消息=%s, 决策=%s(%s), 是否回复=%s",
            self.shop_id,
            customer_uid,
            text,
            _action_label(decision.action),
            decision.action,
            decision.should_reply or decision.action == ACTION_AI,
        )

        # 5) 触发风控：记风控日志并暂停回复（需求 13.2 / 18.3）
        if decision.risk_log is not None:
            logger.info(
                "触发风控，暂停回复: shop_id=%s, 客户=%s, 原因=%s",
                self.shop_id,
                customer_uid,
                decision.risk_log.trigger_reason or "触发风控",
            )
            self._write_risk_log(decision.risk_log.as_dict())
            self._notify("risk_triggered", decision.risk_log.trigger_reason or "触发风控")

        # 6) AI 分支：调用 AI 回复引擎生成内容（失败回退默认回复，需求 8.1/8.5）
        if decision.action == ACTION_AI:
            return await self._handle_ai(context, runtime, customer_uid, text)

        # 7) 商品专属回复且为商品卡片类型：按签名可用性决定发卡或降级文本（需求 13.3/26.3）
        if decision.action == ACTION_GOODS_SPECIFIC and decision.reply_type not in (
            REPLY_TEXT,
            REPLY_IMAGE,
        ):
            return await self._handle_goods_card(context, customer_uid, decision)

        # 8) 需发送回复（关键词 / 商品专属文本 / 默认回复）：发送并记日志
        if decision.should_reply and decision.content:
            sent = await self._send_reply(customer_uid, decision.reply_type, decision.content)
            if sent:
                self._track_reply(customer_uid)
                # 默认回复且开启「只回复一次」：成功发送后登记该客户已收到默认回复，
                # 后续消息不再发送默认回复（需求 7.1）。
                if (
                    decision.action == ACTION_DEFAULT
                    and runtime.shop_config.default_reply_once
                    and customer_uid is not None
                ):
                    self._record_default_reply_sent(customer_uid)
            self._write_message_log(
                customer_uid, log_content, decision.log_result, decision.content
            )
            return ProcessOutcome(
                handled=True,
                action=decision.action,
                replied=sent,
                log_result=decision.log_result,
                content=decision.content,
            )

        # 9) 不发送回复（黑名单 / 过滤 / 非营业 / 风控 / 无匹配）：仅记日志
        self._write_message_log(customer_uid, log_content, decision.log_result, None)
        return ProcessOutcome(
            handled=True,
            action=decision.action,
            replied=False,
            log_result=decision.log_result,
        )

    # ------------------------------------------------------------------
    # AI 分支
    # ------------------------------------------------------------------
    async def _handle_ai(
        self,
        context: Context,
        runtime: ShopRuntime,
        customer_uid: Any,
        text: Optional[str],
    ) -> ProcessOutcome:
        """调用 AI 回复引擎生成回复，失败回退默认回复（需求 8.1 / 8.5）。

        关键：商品咨询 / 商品规格 / 订单类消息的正文是结构化字典而非纯文本，
        若仅传 ``text`` 给 AI，AI 会因「没有商品信息」而无法作答。此处把商品 /
        订单上下文拼成可读文本一并作为 query 传入，使 AI 知道客户咨询的是哪个商品
        （需求 8 / 17.2）。
        """
        default_reply = runtime.shop_config.default_reply_content
        query = self._build_ai_query(context, text)
        # 读取该客户最近的会话历史（按时间正序，转为 OpenAI 风格 user/assistant），
        # 使 AI 能结合上文作答（如「先带商品卡片咨询、再发纯文本追问」场景）。
        history = self._load_ai_history(customer_uid)
        result = await ai_reply_engine.generate_reply(
            query,
            runtime.agent_config or AgentConfig(),
            shop_id=self.shop_pk,
            default_reply=default_reply,
            history=history,
        )
        logger.info(
            "AI 回复生成: shop_id=%s, 客户=%s, 处理结果=%s, 有内容=%s",
            self.shop_id,
            customer_uid,
            result.log_result,
            bool(result.content),
        )

        sent = False
        if result.content:
            sent = await self._send_reply(customer_uid, REPLY_TEXT, result.content)
            if sent:
                self._track_reply(customer_uid)
        self._write_message_log(
            customer_uid,
            self._resolve_log_content(context, text),
            result.log_result,
            result.content,
        )
        return ProcessOutcome(
            handled=True,
            action=ACTION_AI,
            replied=sent,
            log_result=result.log_result,
            content=result.content,
        )

    @staticmethod
    def _build_ai_query(context: Context, text: Optional[str]) -> str:
        """据消息上下文拼装发送给 AI 的查询文本（含商品 / 订单信息，需求 17.2）。

        - 纯文本消息：直接用文本内容；
        - 商品咨询 / 商品规格消息：附上商品名 / ID / 价格 / 规格，使 AI 知道在
          咨询哪个商品；
        - 订单消息：附上订单号 / 商品 / 售后状态等，便于 AI 结合订单作答。

        Args:
            context: 客户消息上下文。
            text: 已提取的纯文本内容（结构化消息时为 None）。

        Returns:
            供 AI 使用的查询文本（尽量非空，便于模型理解客户意图）。
        """
        parts: List[str] = []

        # 商品上下文（商品咨询 / 商品规格）：拼成「客户正在咨询的商品」描述。
        goods = context.goods_context or {}
        if goods:
            goods_desc = []
            if goods.get("goods_name"):
                goods_desc.append(f"商品名称：{goods['goods_name']}")
            if goods.get("goods_id"):
                goods_desc.append(f"商品ID：{goods['goods_id']}")
            if goods.get("goods_price") is not None:
                # 拼多多价格单位为「分」，换算为元展示，便于 AI 与客户沟通。
                price = MessageConsumer._format_price(goods.get("goods_price"))
                if price:
                    goods_desc.append(f"价格：{price}元")
            if goods.get("goods_spec"):
                goods_desc.append(f"规格：{goods['goods_spec']}")
            if goods_desc:
                parts.append("【客户正在咨询的商品】" + "，".join(goods_desc))

        # 订单上下文：拼成「客户关联的订单」描述。
        order = context.order_context or {}
        if order:
            order_desc = []
            if order.get("order_id"):
                order_desc.append(f"订单号：{order['order_id']}")
            if order.get("goods_name"):
                order_desc.append(f"商品名称：{order['goods_name']}")
            if order.get("goods_id"):
                order_desc.append(f"商品ID：{order['goods_id']}")
            if order.get("spec"):
                order_desc.append(f"规格：{order['spec']}")
            if order_desc:
                parts.append("【客户关联的订单】" + "，".join(order_desc))

        # 文本内容：客户实际输入的问题（结构化消息可能为空）。
        if text and text.strip():
            parts.append(text.strip())
        elif goods or order:
            # 结构化消息无文本时，给出明确的咨询意图，引导 AI 主动介绍该商品。
            parts.append("客户咨询了上述商品/订单，请结合商品信息主动为客户解答。")

        return "\n".join(parts) if parts else (text or "")

    @staticmethod
    def _format_price(price: Any) -> Optional[str]:
        """将拼多多「分」为单位的价格换算为「元」字符串（无法解析返回 None）。

        Args:
            price: 原始价格（通常为整数分）。

        Returns:
            形如 "244.03" 的元价格字符串；无法解析返回 None。
        """
        try:
            return f"{int(price) / 100:.2f}"
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # 商品卡片发送 / 签名降级
    # ------------------------------------------------------------------
    async def _handle_goods_card(
        self, context: Context, customer_uid: Any, decision: ReplyDecision
    ) -> ProcessOutcome:
        """发送商品卡片；签名缺失 / 接口失败时降级为文本回复商品信息（需求 13.3 / 26.3）。

        说明：anti-content 签名是否可用，依赖店铺真实 Cookie，运行时只有
        ``TransferService.send_goods_card``（底层 ``SendMessage``）持有。早期实现
        在此用「无 Cookie 的 ``resolve_goods_card_reply``」预判签名，因恒判定为
        「不可用」而使发卡分支变成死代码、商品卡片永远降级为文本。这里改为：
        只要能解析出 goods_id 就先尝试发卡，由底层按真实 Cookie 校验签名；发卡
        失败（含签名缺失 ``downgrade=True`` 或接口失败）再降级为文本（需求 26.3）。
        """
        goods_info = context.goods_context or {}
        # 复用降级判定提取 goods_id 与降级文本（不依赖 Cookie，二者始终可得）。
        fallback = resolve_goods_card_reply(goods_info)

        if fallback.goods_id:
            # 先尝试发卡：底层按店铺真实 Cookie 校验 anti-content 签名，签名缺失 /
            # 失效或接口失败时返回 success=False（downgrade=True），随后降级为文本。
            result = await asyncio.to_thread(
                self._get_transfer_service().send_goods_card,
                customer_uid,
                fallback.goods_id,
            )
            if result.success:
                self._track_reply(customer_uid)
                # 商品卡片发送不经 _send_reply，需在此显式补记 out 方向聊天记录，
                # 否则在线聊天与 AI 历史会缺这条回复。
                self._record_outbound_message(customer_uid, REPLY_TEXT, "[商品卡片]")
                self._write_message_log(
                    customer_uid,
                    self._resolve_log_content(context, None),
                    decision.log_result,
                    "[商品卡片]",
                )
                logger.info(
                    "发送商品卡片: shop_id=%s, 客户=%s, goods_id=%s, 成功=True",
                    self.shop_id,
                    customer_uid,
                    fallback.goods_id,
                )
                return ProcessOutcome(
                    handled=True,
                    action=ACTION_GOODS_SPECIFIC,
                    replied=True,
                    log_result=decision.log_result,
                    content="[商品卡片]",
                )
            # 接口失败 / 签名缺失降级：落到文本降级
            logger.info(
                "商品卡片发送失败，降级为文本回复: shop_id=%s, 客户=%s, goods_id=%s, 原因=%s",
                self.shop_id,
                customer_uid,
                fallback.goods_id,
                result.message,
            )

        # 降级为文本回复商品信息（需求 26.3 / 26.5）
        text_content = fallback.text_content or build_goods_text(goods_info)
        sent = await self._send_reply(customer_uid, REPLY_TEXT, text_content)
        if sent:
            self._track_reply(customer_uid)
        self._write_message_log(
            customer_uid,
            self._resolve_log_content(context, None),
            decision.log_result,
            text_content,
        )
        return ProcessOutcome(
            handled=True,
            action=ACTION_GOODS_SPECIFIC,
            replied=sent,
            downgraded=True,
            log_result=decision.log_result,
            content=text_content,
        )

    # ------------------------------------------------------------------
    # 转人工
    # ------------------------------------------------------------------
    def _should_transfer(
        self, text: Optional[str], transfer_keywords: List[str]
    ) -> bool:
        """判定是否命中转人工关键词（需求 16.3，纯文本包含匹配）。"""
        content = (text or "").strip()
        if not content:
            return False
        return any(kw and kw.strip() and kw.strip() in content for kw in transfer_keywords)

    async def _do_transfer(self, customer_uid: Any, text: Optional[str]) -> ProcessOutcome:
        """发起转人工并暂停自动回复（需求 16.2 / 16.3）。

        转人工底层为同步 HTTP，丢入线程池执行，避免阻塞事件循环。
        """
        logger.info(
            "命中转人工关键词，转人工处理: shop_id=%s, 客户=%s, 消息=%s",
            self.shop_id,
            customer_uid,
            text,
        )
        result = await asyncio.to_thread(
            lambda: self._get_transfer_service().transfer_to_human(
                customer_uid, message_content=text
            )
        )
        logger.info(
            "转人工结果: shop_id=%s, 客户=%s, 成功=%s",
            self.shop_id,
            customer_uid,
            getattr(result, "success", False),
        )
        # transfer_to_human 内部已记消息日志；此处仅汇报结果
        return ProcessOutcome(
            handled=True,
            action=RESULT_TRANSFERRED,
            transferred=True,
            replied=False,
            log_result=RESULT_TRANSFERRED if result.success else None,
        )

    # ------------------------------------------------------------------
    # 发送回复
    # ------------------------------------------------------------------
    async def _send_reply(
        self, recipient_uid: Any, reply_type: Optional[str], content: str
    ) -> bool:
        """经拼多多消息发送接口下发文本 / 图片回复（需求 5.6）。

        发送底层为同步 HTTP（requests），为避免阻塞事件循环（卡住其它店铺的收发
        与心跳），统一经 ``asyncio.to_thread`` 丢入线程池执行。

        Args:
            recipient_uid: 客户 UID。
            reply_type: 回复类型（text / image）。
            content: 回复内容（文本或图片地址）。

        Returns:
            发送成功返回 True；失败返回 False（失败不抛异常，需求 16.5）。
        """
        try:
            sender = self._get_sender()
            if reply_type == REPLY_IMAGE:
                result = await asyncio.to_thread(sender.send_image, recipient_uid, content)
            else:
                result = await asyncio.to_thread(sender.send_text, recipient_uid, content)
            sent = result is not None
            # 发送结果日志：记录回复类型与发送成败，便于核对自动回复是否真正下发。
            preview = content if len(str(content)) <= 200 else str(content)[:200] + "...(已截断)"
            logger.info(
                "发送回复: shop_id=%s, 客户=%s, 类型=%s, 成功=%s, 内容=%s",
                self.shop_id,
                recipient_uid,
                reply_type or REPLY_TEXT,
                sent,
                preview,
            )
            # 发送成功：把本次回复落库为聊天记录（out 方向），供在线聊天展示与
            # AI 会话上下文使用（失败不落库，避免记录未真正送达的回复）。
            if sent:
                self._record_outbound_message(recipient_uid, reply_type, content)
            return sent
        except Exception as exc:  # noqa: BLE001 - 发送失败记日志不中断
            logger.error("发送回复失败: shop_id=%s, %s", self.shop_id, exc)
            return False

    def _track_reply(self, customer_uid: Any) -> None:
        """记录一次成功回复时刻，供后续风控频率判定（需求 13.2）。

        同时裁剪超过保留期（``_REPLY_RETENTION_SECONDS``）的历史时刻，并清理变空的
        客户键，避免长时间运行下回复时间序列与字典只增不减导致内存泄漏、且使风控
        窗口统计随列表膨胀而变慢。保留期远大于任何合理的风控统计窗口，不影响判定。
        """
        moment = now_beijing()
        uid = str(customer_uid)
        horizon = moment - timedelta(seconds=_REPLY_RETENTION_SECONDS)

        # 会话维度：追加并裁剪该客户的过期时刻；裁剪后为空则移除该键。
        session_times = self._session_reply_times[uid]
        session_times.append(moment)
        self._session_reply_times[uid] = [t for t in session_times if t >= horizon]

        # 店铺维度：追加并裁剪过期时刻。
        self._shop_reply_times.append(moment)
        self._shop_reply_times = [t for t in self._shop_reply_times if t >= horizon]

        # 清理其它已不活跃且裁剪后为空的客户键，防止字典无界增长。
        empty_uids = [
            key for key, times in self._session_reply_times.items() if not times
        ]
        for key in empty_uids:
            del self._session_reply_times[key]

    # ------------------------------------------------------------------
    # 默认回复「只回复一次」记录读写（需求 7.1）
    # ------------------------------------------------------------------
    def _has_default_reply_sent(self, customer_uid: Any) -> bool:
        """查询某客户在本店铺是否已收到过默认回复（失败时按未发送处理）。"""
        try:
            return bool(
                self._default_reply_record_reader(self.shop_pk, str(customer_uid))
            )
        except Exception as exc:  # noqa: BLE001 - 查询失败不中断主流程
            logger.error("查询默认回复记录失败: shop_id=%s, %s", self.shop_id, exc)
            return False

    def _record_default_reply_sent(self, customer_uid: Any) -> None:
        """登记某客户在本店铺已收到默认回复（失败不中断主流程）。"""
        try:
            self._default_reply_record_writer(self.shop_pk, str(customer_uid))
        except Exception as exc:  # noqa: BLE001 - 写记录失败不中断主流程
            logger.error("写入默认回复记录失败: shop_id=%s, %s", self.shop_id, exc)

    # ------------------------------------------------------------------
    # 延迟构造的外部依赖
    # ------------------------------------------------------------------
    def _get_sender(self) -> "SendMessage":
        """获取（或惰性构造）拼多多消息发送器。"""
        if self._sender is None:
            from channel_pdd.api.send_message import SendMessage

            self._sender = SendMessage(
                shop_id=self.shop_id,
                user_id=self.user_id,
                channel_name=self.channel_name,
            )
        return self._sender

    def _get_transfer_service(self) -> "TransferService":
        """获取（或惰性构造）转人工 / 商品卡片服务。"""
        if self._transfer_service is None:
            from channel_pdd.transfer_service import TransferService

            self._transfer_service = TransferService(
                shop_id=self.shop_id,
                user_id=self.user_id,
                channel_name=self.channel_name,
            )
        return self._transfer_service

    # ------------------------------------------------------------------
    # 日志与通知（经 common 落库；通知尽力而为调 backend）
    # ------------------------------------------------------------------
    def _write_message_log(
        self,
        customer_uid: Any,
        message_content: Optional[str],
        process_result: Optional[str],
        reply_content: Optional[str],
    ) -> None:
        """写入一条消息处理日志（需求 19.1，经 common 落库，禁止物理删除）。"""
        try:
            self._log_writer(
                {
                    "shop_pk": self.shop_pk,
                    "customer_uid": str(customer_uid) if customer_uid is not None else None,
                    "message_content": message_content,
                    "process_result": process_result,
                    "reply_content": reply_content,
                    "log_time": now_beijing_naive(),
                    "created_by": self.user_id,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 写日志失败不中断主流程
            logger.error("写入消息日志失败: shop_id=%s, %s", self.shop_id, exc)

    # ------------------------------------------------------------------
    # 聊天记录落库与会话历史读取（需求 14 / 17：在线聊天与 AI 会话上下文）
    # ------------------------------------------------------------------
    def _record_inbound_message(
        self, context: Context, customer_uid: Any, text: Optional[str]
    ) -> None:
        """记录客户发来的一条消息到聊天记录（in 方向），并 upsert 会话、累加未读。

        消息内容优先取纯文本；结构化消息（商品 / 订单）无纯文本时，落库一条可读的
        占位内容（如「[商品咨询] 商品名」），并把 goods / order 上下文以 JSON 存储，
        供在线聊天展示与 AI 会话上下文复用。失败不中断主流程（需求 26）。

        Args:
            context: 客户消息上下文。
            customer_uid: 客户唯一标识。
            text: 已提取的纯文本内容（结构化消息时为 None）。
        """
        if customer_uid is None:
            return
        try:
            content = self._resolve_log_content(context, text)
            self._chat_message_writer(
                {
                    "shop_pk": self.shop_pk,
                    "customer_uid": str(customer_uid),
                    "msg_id": context.kwargs.get("msg_id"),
                    "direction": _DIRECTION_IN,
                    "msg_type": str(context.type) if context.type is not None else None,
                    "content": content,
                    "order_context": context.order_context or None,
                    "goods_context": context.goods_context or None,
                    "nickname": context.kwargs.get("nickname"),
                    "msg_time": now_beijing_naive(),
                    "increment_unread": True,
                    "created_by": self.user_id,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败不中断主流程
            logger.error("记录客户消息到聊天记录失败: shop_id=%s, %s", self.shop_id, exc)

    def _forward_chat_event(
        self,
        context: Context,
        customer_uid: Any,
        content: Optional[str],
        direction: str = _DIRECTION_IN,
    ) -> None:
        """将一条消息转发给 backend 实时推送（fire-and-forget，失败不影响主流程）。

        组装与历史记录接口一致口径的消息字典（direction/msg_type/content/ts/msg_at），
        以独立任务异步推送，不阻塞消息处理；无 customer_uid 时跳过。

        Args:
            context: 消息上下文。
            customer_uid: 客户唯一标识。
            content: 已规整的消息文本内容。
            direction: 消息方向（in=客户发来 / out=客服发出），默认 in。
        """
        if customer_uid is None:
            return
        ts_raw = context.kwargs.get("timestamp")
        try:
            ts_int = int(ts_raw) if ts_raw is not None else None
        except (TypeError, ValueError):
            ts_int = None
        self._push_realtime_message(
            customer_uid=customer_uid,
            direction=direction,
            msg_type=str(context.type) if context.type is not None else None,
            content=content,
            msg_id=context.kwargs.get("msg_id"),
            order_context=context.order_context or None,
            goods_context=context.goods_context or None,
            nickname=context.kwargs.get("nickname"),
            ts=ts_int,
        )

    def _push_realtime_message(
        self,
        *,
        customer_uid: Any,
        direction: str,
        msg_type: Optional[str],
        content: Optional[str],
        msg_id: Any = None,
        order_context: Any = None,
        goods_context: Any = None,
        nickname: Optional[str] = None,
        ts: Optional[int] = None,
    ) -> None:
        """组装并以独立任务异步推送一条聊天事件到 backend（fire-and-forget，不抛异常）。

        统一的实时推送底层：组装与历史记录接口一致口径的消息字典，登记任务持有强引用
        避免被 GC 提前回收。无 customer_uid 时跳过。

        Args:
            customer_uid: 客户唯一标识。
            direction: 消息方向（in=客户发来 / out=客服发出）。
            msg_type: 消息类型。
            content: 文本内容。
            msg_id: 拼多多消息 ID（本端主动发出时可能为 None）。
            order_context / goods_context: 订单 / 商品上下文。
            nickname: 昵称。
            ts: 秒级时间戳（缺失为 None）。
        """
        if customer_uid is None:
            return
        try:
            message = {
                "msg_id": msg_id,
                "direction": direction,
                "msg_type": msg_type,
                "content": content,
                "order_context": order_context,
                "goods_context": goods_context,
                "nickname": nickname,
                "ts": ts,
                "msg_at": now_beijing_naive().isoformat(),
            }
            # 以独立任务异步推送，不阻塞当前消息处理链路。登记到任务集合并在完成
            # 后移除，持有强引用避免任务被 GC 提前回收（fire-and-forget 安全）。
            task = asyncio.create_task(
                forward_new_message(self.shop_pk, str(customer_uid), message)
            )
            self._forward_tasks.add(task)
            task.add_done_callback(self._forward_tasks.discard)
        except Exception as exc:  # noqa: BLE001 - 转发组装失败不影响主流程
            logger.warning("组装聊天事件转发失败: shop_id=%s, %s", self.shop_id, exc)

    def _record_outbound_message(
        self, customer_uid: Any, reply_type: Optional[str], content: str
    ) -> None:
        """记录一条已发送的回复到聊天记录（out 方向），并刷新会话最近消息时间。

        out 方向不累加未读（未读仅针对客户来消息）。失败不中断主流程（需求 26）。

        Args:
            customer_uid: 客户唯一标识。
            reply_type: 回复类型（text / image）。
            content: 已发送的回复内容。
        """
        if customer_uid is None:
            return
        try:
            self._chat_message_writer(
                {
                    "shop_pk": self.shop_pk,
                    "customer_uid": str(customer_uid),
                    "direction": _DIRECTION_OUT,
                    "msg_type": reply_type or REPLY_TEXT,
                    "content": content,
                    "order_context": None,
                    "goods_context": None,
                    "nickname": None,
                    "msg_time": now_beijing_naive(),
                    "increment_unread": False,
                    "created_by": self.user_id,
                }
            )
            # 实时推送给在线聊天界面（out 方向）。本系统自动回复 / 商品卡片等本端发出
            # 的消息，拼多多不会经 m-ws 回推，故在此主动推送，保证界面实时显示。
            self._push_realtime_message(
                customer_uid=customer_uid,
                direction=_DIRECTION_OUT,
                msg_type=reply_type or REPLY_TEXT,
                content=content,
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败不中断主流程
            logger.error("记录回复到聊天记录失败: shop_id=%s, %s", self.shop_id, exc)

    def _load_ai_history(self, customer_uid: Any) -> List[Dict[str, Any]]:
        """读取该客户最近的会话历史并转为 OpenAI 风格 messages（按时间正序）。

        in 方向映射为 user、out 方向映射为 assistant；空内容消息跳过。读取失败时
        返回空列表（AI 退化为无上下文回复，不中断主流程）。

        Args:
            customer_uid: 客户唯一标识。

        Returns:
            形如 [{"role": "user"/"assistant", "content": str}, ...] 的历史列表。
        """
        if customer_uid is None:
            return []
        try:
            # 多取一条：process() 开头已把当前这条客户消息落库，它会作为最新一条出现在
            # 历史里；而 generate_reply 会另把当前 query 追加为最后一条 user 消息，故此处
            # 丢弃最新一条（当前消息），避免当前问题在发送给 AI 时重复出现。
            rows = self._chat_history_reader(
                self.shop_pk, str(customer_uid), _AI_HISTORY_MAX_MESSAGES + 1
            )
        except Exception as exc:  # noqa: BLE001 - 读取失败退化为无历史
            logger.error("读取会话历史失败: shop_id=%s, %s", self.shop_id, exc)
            return []

        # rows 按时间正序（旧→新），最后一条为当前刚落库的客户消息，丢弃之。
        if rows:
            rows = rows[:-1]
        history: List[Dict[str, Any]] = []
        for row in rows:
            content = row.get("content")
            if not content or not str(content).strip():
                continue
            role = "assistant" if row.get("direction") == _DIRECTION_OUT else "user"
            history.append({"role": role, "content": str(content)})

        # 商品背景：从「全历史」（不受最近 N 条限制）取最新的商品 / 订单上下文，作为
        # 背景信息注入到历史最前。这样即使客户最初的商品咨询已超出最近 N 条窗口，
        # AI 仍能知道客户在咨询哪个商品 / 关联哪个订单（需求 8 / 17.2）。
        background = self._load_history_product_background(customer_uid)
        if background:
            history.insert(0, {"role": "user", "content": background})
        return history

    def _load_history_product_background(self, customer_uid: Any) -> Optional[str]:
        """从全历史取最新的商品 / 订单上下文，拼成可读背景文本（无则返回 None）。

        不受 AI 历史最近 N 条限制：扫描该客户全部聊天记录，取最近一条携带商品 /
        订单上下文的消息，供 AI 始终知晓客户咨询的商品 / 关联的订单。读取 / 解析失败
        时返回 None（AI 退化为无商品背景，不中断主流程）。

        Args:
            customer_uid: 客户唯一标识。

        Returns:
            形如「【客户正在咨询的商品】商品名称：xxx，...」的背景文本；无则 None。
        """
        if customer_uid is None:
            return None
        try:
            ctx = self._product_context_reader(self.shop_pk, str(customer_uid))
        except Exception as exc:  # noqa: BLE001 - 读取失败退化为无商品背景
            logger.error("读取历史商品上下文失败: shop_id=%s, %s", self.shop_id, exc)
            return None
        if not ctx:
            return None
        desc = self._format_goods_order_desc(
            ctx.get("goods_context"), ctx.get("order_context")
        )
        return ("[商品背景] " + desc) if desc else None

    @staticmethod
    def _format_goods_order_desc(
        goods: Optional[Dict[str, Any]], order: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """将商品 / 订单上下文拼成可读描述（供 AI 商品背景 / 查询复用）。

        Args:
            goods: 商品上下文字典（goods_name/goods_id/goods_price/goods_spec）。
            order: 订单上下文字典（order_id/goods_name/goods_id/spec）。

        Returns:
            可读描述文本；均为空时返回 None。
        """
        parts: List[str] = []
        goods = goods or {}
        if goods:
            goods_desc: List[str] = []
            if goods.get("goods_name"):
                goods_desc.append(f"商品名称：{goods['goods_name']}")
            if goods.get("goods_id"):
                goods_desc.append(f"商品ID：{goods['goods_id']}")
            if goods.get("goods_price") is not None:
                price = MessageConsumer._format_price(goods.get("goods_price"))
                if price:
                    goods_desc.append(f"价格：{price}元")
            if goods.get("goods_spec"):
                goods_desc.append(f"规格：{goods['goods_spec']}")
            if goods_desc:
                parts.append("【客户正在咨询的商品】" + "，".join(goods_desc))

        order = order or {}
        if order:
            order_desc: List[str] = []
            if order.get("order_id"):
                order_desc.append(f"订单号：{order['order_id']}")
            if order.get("goods_name"):
                order_desc.append(f"商品名称：{order['goods_name']}")
            if order.get("goods_id"):
                order_desc.append(f"商品ID：{order['goods_id']}")
            if order.get("spec"):
                order_desc.append(f"规格：{order['spec']}")
            if order_desc:
                parts.append("【客户关联的订单】" + "，".join(order_desc))

        return "；".join(parts) if parts else None

    @staticmethod
    def _summarize_content(context: Context) -> Optional[str]:
        """为结构化消息生成可读的落库内容（商品 / 订单摘要）。

        Args:
            context: 客户消息上下文。

        Returns:
            可读的占位内容；无法生成时返回 None。
        """
        goods = context.goods_context or {}
        if goods and goods.get("goods_name"):
            return f"[商品咨询] {goods['goods_name']}"
        order = context.order_context or {}
        if order and order.get("goods_name"):
            return f"[订单] {order['goods_name']}"
        # 其它类型（图片 / 视频 / 表情等）以类型名占位，避免落库空内容。
        return f"[{context.type}]" if context.type is not None else None

    @staticmethod
    def _resolve_log_content(context: Context, text: Optional[str]) -> Optional[str]:
        """计算落库用的消息内容：优先纯文本，结构化消息回退为可读摘要。

        用于消息日志 message_content 与聊天记录 content：纯文本消息直接用文本；
        商品咨询 / 商品规格 / 订单等结构化消息（content 为字典、text 为 None）回退
        为「[商品咨询] 商品名」等可读摘要，避免日志 / 记录中消息内容为空。

        Args:
            context: 客户消息上下文。
            text: 已提取的纯文本内容（结构化消息时为 None）。

        Returns:
            可读的落库内容；无法生成时返回 None。
        """
        if text and text.strip():
            return text
        return MessageConsumer._summarize_content(context)

    def _write_risk_log(self, risk_data: Dict[str, Any]) -> None:
        """写入一条风控日志（需求 13.2 / 19.2，经 common 落库）。"""
        try:
            self._risk_log_writer(
                {
                    "shop_pk": risk_data.get("shop_pk", self.shop_pk),
                    "risk_type": risk_data.get("risk_type"),
                    "trigger_reason": risk_data.get("trigger_reason"),
                    "log_time": now_beijing_naive(),
                    "created_by": self.user_id,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 写日志失败不中断主流程
            logger.error("写入风控日志失败: shop_id=%s, %s", self.shop_id, exc)

    def _notify(self, event_type: str, content: str) -> None:
        """推送系统事件通知（尽力而为，失败不影响主流程，需求 18.3 / 18.4）。"""
        if self._notifier is None:
            return
        try:
            self._notifier(event_type, content)
        except Exception as exc:  # noqa: BLE001 - 通知失败不中断主流程
            logger.error("推送系统事件通知失败: shop_id=%s, %s", self.shop_id, exc)

    # ------------------------------------------------------------------
    # 默认实现：经 common 仓储读写
    # ------------------------------------------------------------------
    @staticmethod
    def _default_runtime_loader(shop_pk: int) -> ShopRuntime:
        """默认运行时加载器：经 common 仓储在独立会话中读取规则与配置。"""
        return run_in_session(lambda session: load_shop_runtime(shop_pk, session))

    @staticmethod
    def _default_log_writer(values: Dict[str, Any]) -> None:
        """默认消息日志写入器：经 common 仓储落库。"""
        run_in_session(lambda session: Repository(MessageLog, session).create(**values))

    @staticmethod
    def _default_risk_log_writer(values: Dict[str, Any]) -> None:
        """默认风控日志写入器：经 common 仓储落库。"""
        run_in_session(lambda session: Repository(RiskLog, session).create(**values))

    @staticmethod
    def _default_record_reader(shop_pk: int, customer_uid: str) -> bool:
        """默认「只回复一次」记录读取器：经 common 仓储查询是否已存在发送记录。"""
        return run_in_session(
            lambda session: Repository(DefaultReplyRecord, session).get_by(
                shop_pk=shop_pk, customer_uid=customer_uid
            )
            is not None
        )

    @staticmethod
    def _default_record_writer(shop_pk: int, customer_uid: str) -> None:
        """默认「只回复一次」记录写入器：经 common 仓储 upsert 登记已回复客户。

        以 (shop_pk, customer_uid) 为业务键 upsert，保证同一客户记录恒为一条，
        重复登记不会新增多余记录（幂等）。
        """
        run_in_session(
            lambda session: Repository(DefaultReplyRecord, session).upsert(
                biz_keys={"shop_pk": shop_pk, "customer_uid": customer_uid},
                values={},
            )
        )

    @staticmethod
    def _default_chat_message_writer(values: Dict[str, Any]) -> None:
        """默认聊天消息写入器：upsert 会话 + 新增聊天消息（经 common 仓储落库）。

        在同一事务内：
        1. 按 (shop_pk, customer_uid) upsert 会话 conversation，刷新最近消息时间，
           in 方向时累加未读数、补充昵称；
        2. 新增一条 chat_message，商品 / 订单上下文以 JSON 文本存储（与 backend
           chat_context_service 的存储口径一致）。

        Args:
            values: 由 _record_inbound_message / _record_outbound_message 组装的字段字典。
        """
        shop_pk = values["shop_pk"]
        customer_uid = values["customer_uid"]
        msg_time = values.get("msg_time") or now_beijing_naive()
        increment_unread = bool(values.get("increment_unread"))
        nickname = values.get("nickname")
        order_context = values.get("order_context")
        goods_context = values.get("goods_context")

        def _do(session: Any) -> None:
            conv_repo = Repository(Conversation, session)
            conversation = conv_repo.get_by(shop_pk=shop_pk, customer_uid=customer_uid)
            if conversation is None:
                # 新建会话：未读数据据方向初始化。
                conv_repo.create(
                    shop_pk=shop_pk,
                    customer_uid=customer_uid,
                    nickname=nickname,
                    last_msg_at=msg_time,
                    unread_count=1 if increment_unread else 0,
                )
            else:
                update_values: Dict[str, Any] = {"last_msg_at": msg_time}
                if nickname:
                    update_values["nickname"] = nickname
                if increment_unread:
                    update_values["unread_count"] = int(conversation.unread_count or 0) + 1
                conv_repo.update(conversation.id, **update_values)

            Repository(ChatMessage, session).create(
                shop_pk=shop_pk,
                customer_uid=customer_uid,
                msg_id=values.get("msg_id"),
                direction=values["direction"],
                msg_type=values.get("msg_type"),
                content=values.get("content"),
                order_context=(
                    json.dumps(order_context, ensure_ascii=False)
                    if order_context
                    else None
                ),
                goods_context=(
                    json.dumps(goods_context, ensure_ascii=False)
                    if goods_context
                    else None
                ),
                msg_time=msg_time,
                created_by=values.get("created_by"),
            )

        run_in_session(_do)

    @staticmethod
    def _default_chat_history_reader(
        shop_pk: int, customer_uid: str, limit: int
    ) -> List[Dict[str, Any]]:
        """默认会话历史读取器：取该客户最近 limit 条聊天消息，按时间正序返回。

        先按消息时间倒序取最近 limit 条，再反转为正序（旧→新），符合对话历史的
        自然顺序。仅返回 AI 上下文所需的 direction / content 字段。

        Args:
            shop_pk: 店铺主键。
            customer_uid: 客户唯一标识。
            limit: 最多返回的历史条数。

        Returns:
            按时间正序的历史消息字典列表（含 direction / content）。
        """
        def _do(session: Any) -> List[Dict[str, Any]]:
            stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.shop_pk == shop_pk,
                    ChatMessage.customer_uid == customer_uid,
                )
                .order_by(ChatMessage.msg_time.desc(), ChatMessage.id.desc())
                .limit(limit)
            )
            rows = list(session.execute(stmt).scalars().all())
            rows.reverse()  # 倒序取最近 N 条后反转为正序（旧→新）
            return [
                {"direction": row.direction, "content": row.content} for row in rows
            ]

        return run_in_session(_do)

    @staticmethod
    def _default_product_context_reader(
        shop_pk: int, customer_uid: str
    ) -> Optional[Dict[str, Any]]:
        """默认全历史商品/订单上下文读取器：取该客户最近一条携带上下文的消息并解析。

        不受最近 N 条限制：在该客户全部聊天记录中，按时间倒序取第一条
        ``goods_context`` 或 ``order_context`` 非空的消息，解析其 JSON 文本返回。
        供 AI 始终获知客户咨询的商品 / 关联的订单（需求 8 / 17.2）。

        Args:
            shop_pk: 店铺主键。
            customer_uid: 客户唯一标识。

        Returns:
            ``{"goods_context": dict|None, "order_context": dict|None}``；
            无任何带上下文的历史消息时返回 None。
        """
        def _do(session: Any) -> Optional[Dict[str, Any]]:
            stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.shop_pk == shop_pk,
                    ChatMessage.customer_uid == customer_uid,
                    or_(
                        ChatMessage.goods_context.isnot(None),
                        ChatMessage.order_context.isnot(None),
                    ),
                )
                .order_by(ChatMessage.msg_time.desc(), ChatMessage.id.desc())
                .limit(1)
            )
            row = session.execute(stmt).scalars().first()
            if row is None:
                return None

            def _loads(raw: Optional[str]) -> Optional[Dict[str, Any]]:
                if not raw:
                    return None
                try:
                    value = json.loads(raw)
                except (ValueError, TypeError):
                    return None
                return value if isinstance(value, dict) else None

            return {
                "goods_context": _loads(row.goods_context),
                "order_context": _loads(row.order_context),
            }

        return run_in_session(_do)


def build_notifier(
    shop_pk: Optional[int] = None,
    event_url_path: str = "/api/v1/notify/events",
) -> Notifier:
    """构造一个经 backend HTTP 接口推送系统事件通知的通知器（尽力而为）。

    地址经环境变量配置（禁止写死 localhost，规范 21 / 需求 25.4）；调用失败由
    service_client 内部规整为失败响应，不抛异常（需求 18.4 / 26）。

    通知渠道为店铺级（方案 A）：将 shop_pk 一并上报，backend 仅推送该店铺的
    已启用渠道。

    Args:
        shop_pk: 事件归属店铺主键；随事件上报，供 backend 按店铺筛选渠道。
        event_url_path: backend 系统事件推送接口相对路径。

    Returns:
        ``notifier(event_type, content)`` 可调用对象。
    """
    from common.services import service_client

    def _notify(event_type: str, content: str) -> None:
        service_client.post_json(
            service_client.backend_base_url(),
            event_url_path,
            {"event_type": event_type, "content": content, "shop_pk": shop_pk},
        )

    return _notify


__all__ = [
    "ShopRuntime",
    "ProcessOutcome",
    "MessageConsumer",
    "load_shop_runtime",
    "build_notifier",
    "RESULT_TRANSFERRED",
]
