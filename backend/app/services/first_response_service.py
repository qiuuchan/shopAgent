# -*- coding: utf-8 -*-
"""
backend.app.services.first_response_service —— 首响时长统计业务服务
==================================================================
本文件用途：实现 backend 的「买家首响时长统计」业务逻辑（TIK-025，对齐 PLAN §8
Phase 3 出口「24h 回复率 ≥85%」），供 dashboard 路由复用：

- 按「平台 / 店铺 / 日期范围」统计周期维度的首响时长分布与超 5 分钟占比；
- 输出汇总指标（均值 / P50 / P90 / 最长 / 超时占比 / 回复率）与分店铺明细，
  供前端 dashboard 展示（需求 20.x 数据分析扩展）；
- 回复率口径与 TIK-026「跌破 85% 告警」共用 ``common.utils.latency.reply_rate``，
  保证「看板看到的」与「告警判的」是同一个数。

口径说明（与 TIK-018 验收对账工具 tools/tiktok_acceptance/reconcile.py 完全一致）：
- 周期（cycle）= 一段买家连续消息 + 其后本店首次回复；首响时长 = 回复时间 - 该段
  第一条买家消息时间（多买家消息静默聚合，起点不重置）；
- 窗口结束时仍无回复的买家消息段 = 待回复（pending）；
- 超时：首响时长 > 300 秒（5 分钟首响标准）；
- 回复率 = (已回复 - 超时) / (已回复 + 待回复)，待回复与超时均计未达标；
- 纯计算全部委托 ``common.utils.latency``（无 I/O 纯函数，便于单测与前端对齐）。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3）。
- 所有数据访问经 SQLAlchemy 表达式构造参数化查询，禁止字符串拼接 SQL（规范 16）。
- 数据范围隔离：非管理员仅统计本人 / 被授权店铺的数据，复用
  app.services.dashboard_service.visible_shop_ids 与 app.core.data_scope（需求 3.7）。
- 时间统一北京时间（规范 17）；日期解析复用 dashboard_service 的公共函数（规范 52）。
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.business_codes import CODE_PARAM_ERROR
from app.services.account_service import VALID_PLATFORMS
from app.services.dashboard_service import (
    DATE_FORMAT,
    resolve_trend_range,
    visible_shop_ids,
)
from common.models.log_models import ChatMessage
from common.models.shop_models import Shop
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.utils.latency import (
    FIRST_RESPONSE_THRESHOLD_SECONDS,
    Cycle,
    PendingCycle,
    compute_cycles,
    first_response_stats,
    latency_distribution,
    reply_rate,
)

# 首响统计时间范围上限（天）：消息按行读入内存后聚合，范围须比趋势统计更保守。
MAX_STATS_DAYS: int = 92


# ----------------------------------------------------------------------
# 店铺范围解析：数据范围 + 平台 / 店铺筛选
# ----------------------------------------------------------------------
def _resolve_shops(
    session: Session,
    user: SysUser,
    platform: Optional[str],
    shop_pk: Optional[int],
) -> List[Tuple[int, str, str]]:
    """解析参与统计的店铺清单（数据范围内 + 平台 / 店铺筛选）。

    Args:
        session: 数据库会话。
        user: 当前登录用户（用于数据范围隔离）。
        platform: 平台标识（'pdd' / 'tiktok'）；None 表示全部平台。
        shop_pk: 指定店铺主键；None 表示不按店铺过滤。

    Returns:
        ``[(shop_pk, shop_name, platform), ...]`` 列表，按店铺主键升序。
        无可见店铺 / 筛选后无店铺时返回空列表。
    """
    scope_ids = visible_shop_ids(session, user)
    stmt = select(Shop.id, Shop.shop_name, Shop.platform)
    # 管理员（scope_ids 为 None）不附加归属条件；非管理员限定其可见店铺。
    if scope_ids is not None:
        stmt = stmt.where(Shop.id.in_(scope_ids))
    if platform is not None:
        stmt = stmt.where(Shop.platform == platform)
    if shop_pk is not None:
        stmt = stmt.where(Shop.id == shop_pk)

    rows = session.execute(stmt.order_by(Shop.id)).all()
    return [(int(pk), name or "", plat or "") for pk, name, plat in rows]


def _query_messages(
    session: Session, shop_pks: List[int], start: datetime, end_exclusive: datetime
) -> List[Tuple[int, str, str, datetime]]:
    """查询窗口内的聊天消息（按店铺 / 客户 / 时间升序），msg_time 为空的行不参与统计。

    仅取统计所需四列，避免把消息正文等大字段读进内存。

    Args:
        session: 数据库会话。
        shop_pks: 参与统计的店铺主键列表。
        start: 窗口下界（含），北京时间零点。
        end_exclusive: 窗口上界（不含），北京时间零点。

    Returns:
        ``[(shop_pk, customer_uid, direction, msg_time), ...]`` 列表，已按
        店铺 / 客户 / 时间升序。
    """
    if not shop_pks:
        return []
    stmt = (
        select(
            ChatMessage.shop_pk,
            ChatMessage.customer_uid,
            ChatMessage.direction,
            ChatMessage.msg_time,
        )
        .where(
            ChatMessage.shop_pk.in_(shop_pks),
            ChatMessage.msg_time.is_not(None),
            ChatMessage.msg_time >= start,
            ChatMessage.msg_time < end_exclusive,
        )
        .order_by(
            ChatMessage.shop_pk,
            ChatMessage.customer_uid,
            ChatMessage.msg_time,
            ChatMessage.id,
        )
    )
    return [
        (int(pk), customer_uid, direction, msg_time)
        for pk, customer_uid, direction, msg_time in session.execute(stmt).all()
    ]


# ----------------------------------------------------------------------
# 周期聚合（纯逻辑，便于单测）
# ----------------------------------------------------------------------
def _aggregate_by_shop(
    messages: List[Tuple[int, str, str, datetime]],
) -> Dict[int, Tuple[List[Cycle], List[PendingCycle], int]]:
    """按「店铺 + 客户」切分周期，返回每店铺的周期与待回复统计。

    Args:
        messages: ``(shop_pk, customer_uid, direction, msg_time)`` 列表，须已按
            店铺 / 客户 / 时间升序。

    Returns:
        ``{shop_pk: (cycles, pending_cycles, pending_conversations)}``；
        ``pending_conversations`` 为该店铺下存在待回复周期的会话数。
    """
    by_conversation: Dict[Tuple[int, str], List[Tuple[str, datetime]]] = defaultdict(list)
    for shop_pk, customer_uid, direction, msg_time in messages:
        by_conversation[(shop_pk, customer_uid)].append((direction, msg_time))

    accumulator: Dict[int, Dict[str, Any]] = {}
    for (shop_pk, _customer_uid), conversation in by_conversation.items():
        cycles, pending = compute_cycles(conversation)
        entry = accumulator.setdefault(
            shop_pk, {"cycles": [], "pending": [], "conversations": 0}
        )
        entry["cycles"].extend(cycles)
        entry["pending"].extend(pending)
        # 待回复会话数：存在 pending 周期的会话计 1 个（与对账工具口径一致）。
        if pending:
            entry["conversations"] += 1

    return {
        shop_pk: (item["cycles"], item["pending"], item["conversations"])
        for shop_pk, item in accumulator.items()
    }


def _shop_stats(
    shop_pk: int,
    shop_name: str,
    platform: str,
    cycles: List[Cycle],
    pending: List[PendingCycle],
    pending_conversations: int,
    threshold_seconds: float,
) -> Dict[str, Any]:
    """汇总单店铺的首响指标（在 first_response_stats 之上补回复率）。"""
    stats = first_response_stats(
        cycles, pending, pending_conversations, threshold_seconds=threshold_seconds
    )
    stats.update(
        {
            "shop_pk": shop_pk,
            "shop_name": shop_name,
            "platform": platform,
            "reply_rate": reply_rate(
                stats["responded_cycles"],
                stats["over_threshold_count"],
                stats["pending_cycles"],
            ),
        }
    )
    return stats


# ----------------------------------------------------------------------
# 首响统计主入口
# ----------------------------------------------------------------------
def get_first_response_stats(
    session: Session,
    user: SysUser,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    platform: Optional[str] = None,
    shop_pk: Optional[int] = None,
) -> ApiResponse:
    """返回窗口内首响时长分布、超阈值占比与回复率（北京时间口径）。

    Args:
        session: 数据库会话。
        user: 当前登录用户（数据范围隔离，需求 3.7）。
        start_date: 起始日期（YYYY-MM-DD）；None 时默认最近 7 天起。
        end_date: 结束日期（YYYY-MM-DD）；None 时默认今日。
        platform: 平台筛选（'pdd' / 'tiktok'）；None 表示全部平台。
        shop_pk: 店铺主键筛选；None 表示全部可见店铺。

    Returns:
        统一响应体：data 含 {start_date, end_date, threshold_seconds, platform,
        shop_pk, shop_count, summary, distribution, shops}。参数非法时返回
        success=false（业务码 CODE_PARAM_ERROR）。
    """
    # 平台枚举校验放在最前：非法平台直接拒绝，不做任何查询。
    if platform is not None and platform not in VALID_PLATFORMS:
        return error_response(
            CODE_PARAM_ERROR, f"非法的平台标识：{platform!r}（仅允许 {VALID_PLATFORMS}）"
        )

    error, start_dt, end_dt = resolve_trend_range(start_date, end_date)
    if error is not None:
        return error

    span_days = (end_dt - start_dt).days + 1
    if span_days > MAX_STATS_DAYS:
        return error_response(
            CODE_PARAM_ERROR, f"查询时间范围不能超过 {MAX_STATS_DAYS} 天"
        )

    # 指定店铺须落在数据范围内，否则视为参数错误（不泄漏店铺是否存在）。
    shops = _resolve_shops(session, user, platform, shop_pk)
    if shop_pk is not None and not shops:
        return error_response(CODE_PARAM_ERROR, "店铺不存在或无访问权限")

    shop_pks = [pk for pk, _name, _plat in shops]
    messages = _query_messages(
        session, shop_pks, start_dt, end_dt + timedelta(days=1)
    )

    per_shop = _aggregate_by_shop(messages)
    shop_rows: List[Dict[str, Any]] = []
    all_cycles: List[Cycle] = []
    all_pending: List[PendingCycle] = []
    all_pending_conversations = 0

    for pk, name, plat in shops:
        cycles, pending, pending_conversations = per_shop.get(pk, ([], [], 0))
        all_cycles.extend(cycles)
        all_pending.extend(pending)
        all_pending_conversations += pending_conversations
        shop_rows.append(
            _shop_stats(
                pk,
                name,
                plat,
                cycles,
                pending,
                pending_conversations,
                FIRST_RESPONSE_THRESHOLD_SECONDS,
            )
        )

    summary = first_response_stats(all_cycles, all_pending, all_pending_conversations)
    summary["reply_rate"] = reply_rate(
        summary["responded_cycles"],
        summary["over_threshold_count"],
        summary["pending_cycles"],
    )

    data = {
        "start_date": start_dt.strftime(DATE_FORMAT),
        "end_date": end_dt.strftime(DATE_FORMAT),
        "threshold_seconds": summary["threshold_seconds"],
        "platform": platform,
        "shop_pk": shop_pk,
        "shop_count": len(shop_rows),
        "summary": summary,
        "distribution": latency_distribution(all_cycles),
        "shops": shop_rows,
    }
    return success_response(data=data, message="查询成功")


__all__ = [
    "MAX_STATS_DAYS",
    "get_first_response_stats",
]
