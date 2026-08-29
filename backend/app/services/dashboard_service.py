# -*- coding: utf-8 -*-
"""
backend.app.services.dashboard_service —— 仪表盘与数据分析业务服务
================================================================
本文件用途：实现 backend 服务的「仪表盘与数据分析」业务逻辑，供 dashboard 路由
复用，满足需求 20（仪表盘与数据分析）：

- ``get_overview(...)``：返回仪表盘关键指标（需求 20.1）——
  * 在线店铺数：启用状态（status=1）的店铺数；
  * 今日消息数：今日（北京时间口径）的消息处理日志条数；
  * 今日自动回复数：今日处理结果为「自动回复（auto_reply）」的消息日志条数；
  * AI 回复数：今日处理结果为「AI 回复（ai_reply）」的消息日志条数；
  * 风控触发数：今日风控日志条数。
- ``get_trend(...)``：返回指定时间范围内（北京时间口径）按天聚合的消息量与回复量
  趋势数据（需求 20.2）。

统计口径（需求 20.3）：所有「今日 / 时间范围」均以北京时间（Asia/Shanghai，UTC+8）
为口径。日志时间字段以北京时间朴素 datetime 存储，故按北京时间自然日切分。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3 / 需求 24.1）。
- 所有数据访问经 SQLAlchemy 表达式构造参数化查询，禁止字符串拼接 SQL（规范 16）。
- 数据范围隔离：非管理员仅统计其本人创建 / 被授权店铺的数据，复用
  app.core.data_scope（规范 42 集中判权 / 需求 3.7）。
- 时间统一北京时间（规范 17 / 需求 24.8）。
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.business_codes import CODE_PARAM_ERROR
from app.core.data_scope import build_data_scope, build_owner_condition
from common.models.log_models import MessageLog, RiskLog
from common.models.shop_models import Shop
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.utils.time_utils import now_beijing_naive

# 店铺「启用」状态值（与 Shop.status 约定一致：1=启用，0=停用）。
SHOP_STATUS_ENABLED: int = 1

# 消息处理结果键：自动回复 / AI 回复（与 dict_seed_data 的 process_result 一致）。
PROCESS_RESULT_AUTO_REPLY: str = "auto_reply"
PROCESS_RESULT_AI_REPLY: str = "ai_reply"

# 趋势查询时间范围上限（天）：防止超长范围导致一次性聚合过多数据。
MAX_TREND_DAYS: int = 366

# 趋势查询默认天数（未显式指定起止日期时，默认统计最近 7 天，含今日）。
DEFAULT_TREND_DAYS: int = 7

# 日期字符串格式（北京时间自然日）。
DATE_FORMAT: str = "%Y-%m-%d"


# ----------------------------------------------------------------------
# 数据范围辅助：解析当前用户可见店铺主键集合（需求 3.7）
# ----------------------------------------------------------------------
def visible_shop_ids(session: Session, user: SysUser) -> Optional[List[int]]:
    """解析当前用户数据范围内的店铺主键集合（需求 3.7）。

    管理员可见全部店铺，返回 None 表示「不附加店铺范围限制」；非管理员返回其本人
    创建或被授权店铺的主键列表（可能为空列表，表示无任何可见店铺）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。

    Returns:
        管理员返回 None（不限制）；非管理员返回可见店铺主键列表（可能为空）。
    """
    scope = build_data_scope(user, session=session)
    condition = build_owner_condition(scope, Shop.owner_user_id)
    # 管理员：condition 为 None，表示不附加任何归属限制（可见全部店铺）。
    if condition is None:
        return None
    stmt = select(Shop.id).where(condition)
    return [int(row) for row in session.execute(stmt).scalars().all()]


def _today_range() -> tuple[datetime, datetime]:
    """计算「今日」的北京时间自然日区间 [今日 00:00, 明日 00:00)。

    Returns:
        二元组 (今日零点, 明日零点)，均为北京时间朴素 datetime。
    """
    today_start = now_beijing_naive().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return today_start, today_start + timedelta(days=1)


# ----------------------------------------------------------------------
# 通用计数辅助（参数化查询 + 数据范围 + 时间区间）
# ----------------------------------------------------------------------
def _count_with_scope(
    session: Session,
    model: Any,
    shop_ids: Optional[List[int]],
    *,
    time_column: Any = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    extra_conditions: Optional[List[Any]] = None,
) -> int:
    """按数据范围与时间区间统计某模型记录数（参数化查询）。

    Args:
        session: 数据库会话。
        model: 目标模型（含 shop_pk 列）。
        shop_ids: 可见店铺主键列表；None 表示不限制（管理员）；空列表表示无可见
            店铺（计数恒为 0）。
        time_column: 时间过滤列；None 表示不按时间过滤。
        start: 时间区间下界（含）。
        end: 时间区间上界（不含）。
        extra_conditions: 附加的等值 / 比较条件列表。

    Returns:
        满足条件的记录数。
    """
    # 非管理员且无任何可见店铺：直接返回 0，避免无谓查询。
    if shop_ids is not None and len(shop_ids) == 0:
        return 0

    stmt = select(func.count()).select_from(model)
    if shop_ids is not None:
        stmt = stmt.where(model.shop_pk.in_(shop_ids))
    if time_column is not None and start is not None and end is not None:
        stmt = stmt.where(time_column >= start, time_column < end)
    for condition in extra_conditions or []:
        stmt = stmt.where(condition)
    return int(session.execute(stmt).scalar_one())


def _count_online_shops(
    session: Session, shop_ids: Optional[List[int]]
) -> int:
    """统计「启用状态」的店铺数（需求 20.1）。

    口径：店铺表 ``Shop.status == 1`` 即启用（停用为 0，停用即逻辑删除）。直接
    按店铺维度计数，受当前用户数据范围约束（非管理员仅统计本人 / 被授权店铺）。

    Args:
        session: 数据库会话。
        shop_ids: 可见店铺主键列表；None 表示不限制（管理员）。

    Returns:
        启用状态的店铺数量。
    """
    if shop_ids is not None and len(shop_ids) == 0:
        return 0
    stmt = select(func.count()).select_from(Shop).where(
        Shop.status == SHOP_STATUS_ENABLED
    )
    if shop_ids is not None:
        stmt = stmt.where(Shop.id.in_(shop_ids))
    return int(session.execute(stmt).scalar_one())


# ----------------------------------------------------------------------
# 仪表盘关键指标（需求 20.1 / 20.3）
# ----------------------------------------------------------------------
def get_overview(session: Session, user: SysUser) -> ApiResponse:
    """返回仪表盘关键指标（需求 20.1，按北京时间口径，需求 20.3）。

    指标：在线店铺数、今日消息数、今日自动回复数、AI 回复数、风控触发数。各项均
    受数据范围隔离约束（非管理员仅统计本人 / 被授权店铺，需求 3.7）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。

    Returns:
        统一响应体：data 为关键指标字典。
    """
    shop_ids = visible_shop_ids(session, user)
    today_start, tomorrow_start = _today_range()

    online_shops = _count_online_shops(session, shop_ids)
    today_messages = _count_with_scope(
        session,
        MessageLog,
        shop_ids,
        time_column=MessageLog.log_time,
        start=today_start,
        end=tomorrow_start,
    )
    today_auto_replies = _count_with_scope(
        session,
        MessageLog,
        shop_ids,
        time_column=MessageLog.log_time,
        start=today_start,
        end=tomorrow_start,
        extra_conditions=[MessageLog.process_result == PROCESS_RESULT_AUTO_REPLY],
    )
    today_ai_replies = _count_with_scope(
        session,
        MessageLog,
        shop_ids,
        time_column=MessageLog.log_time,
        start=today_start,
        end=tomorrow_start,
        extra_conditions=[MessageLog.process_result == PROCESS_RESULT_AI_REPLY],
    )
    today_risk_triggers = _count_with_scope(
        session,
        RiskLog,
        shop_ids,
        time_column=RiskLog.log_time,
        start=today_start,
        end=tomorrow_start,
    )

    data = {
        "online_shops": online_shops,
        "today_messages": today_messages,
        "today_auto_replies": today_auto_replies,
        "today_ai_replies": today_ai_replies,
        "today_risk_triggers": today_risk_triggers,
    }
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 数据分析趋势（需求 20.2 / 20.3）
# ----------------------------------------------------------------------
def parse_date(value: Optional[str]) -> tuple[bool, Optional[datetime]]:
    """将 ``YYYY-MM-DD`` 日期字符串解析为北京时间零点朴素 datetime。

    Args:
        value: 日期字符串；None 表示未提供。

    Returns:
        二元组 (是否合法, 解析结果)。未提供返回 (True, None)；格式非法返回
        (False, None)；合法返回 (True, 当日零点 datetime)。
    """
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    try:
        parsed = datetime.strptime(value.strip(), DATE_FORMAT)
    except (ValueError, TypeError):
        return False, None
    return True, parsed.replace(hour=0, minute=0, second=0, microsecond=0)


def resolve_trend_range(
    start_date: Optional[str], end_date: Optional[str]
) -> tuple[Optional[ApiResponse], Optional[datetime], Optional[datetime]]:
    """解析并校验趋势统计的起止日期区间（北京时间自然日）。

    规则：
    - 未提供则默认统计最近 ``DEFAULT_TREND_DAYS`` 天（含今日）；
    - 仅提供一端时，另一端按默认天数推算；
    - 起始日期不得晚于结束日期；
    - 跨度不得超过 ``MAX_TREND_DAYS`` 天。

    Args:
        start_date: 起始日期字符串（YYYY-MM-DD）或 None。
        end_date: 结束日期字符串（YYYY-MM-DD）或 None。

    Returns:
        三元组 (错误响应, 起始零点, 结束零点)。校验通过时第一项为 None。
    """
    start_ok, start_dt = parse_date(start_date)
    if not start_ok:
        return error_response(CODE_PARAM_ERROR, "起始日期格式应为 YYYY-MM-DD"), None, None
    end_ok, end_dt = parse_date(end_date)
    if not end_ok:
        return error_response(CODE_PARAM_ERROR, "结束日期格式应为 YYYY-MM-DD"), None, None

    today_start = now_beijing_naive().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # 结束日期缺省取今日；起始日期缺省按默认天数向前推算。
    if end_dt is None:
        end_dt = today_start
    if start_dt is None:
        start_dt = end_dt - timedelta(days=DEFAULT_TREND_DAYS - 1)

    if start_dt > end_dt:
        return error_response(CODE_PARAM_ERROR, "起始日期不能晚于结束日期"), None, None

    span_days = (end_dt - start_dt).days + 1
    if span_days > MAX_TREND_DAYS:
        return (
            error_response(
                CODE_PARAM_ERROR, f"查询时间范围不能超过 {MAX_TREND_DAYS} 天"
            ),
            None,
            None,
        )
    return None, start_dt, end_dt


def _daily_counts(
    session: Session,
    shop_ids: Optional[List[int]],
    start: datetime,
    end_exclusive: datetime,
    *,
    extra_conditions: Optional[List[Any]] = None,
) -> Dict[str, int]:
    """按天聚合统计 MessageLog 条数，返回「日期字符串 -> 条数」映射（参数化查询）。

    使用 ``func.date`` 对北京时间日志字段按自然日分组（MySQL / SQLite 均支持），
    避免逐日多次查询。

    Args:
        session: 数据库会话。
        shop_ids: 可见店铺主键列表；None 表示不限制；空列表返回空映射。
        start: 统计区间下界（含），北京时间零点。
        end_exclusive: 统计区间上界（不含），北京时间零点。
        extra_conditions: 附加条件（如处理结果筛选）。

    Returns:
        日期字符串（YYYY-MM-DD）到条数的映射，仅含有数据的日期。
    """
    if shop_ids is not None and len(shop_ids) == 0:
        return {}

    date_col = func.date(MessageLog.log_time)
    stmt = (
        select(date_col.label("day"), func.count().label("cnt"))
        .where(MessageLog.log_time >= start, MessageLog.log_time < end_exclusive)
        .group_by(date_col)
    )
    if shop_ids is not None:
        stmt = stmt.where(MessageLog.shop_pk.in_(shop_ids))
    for condition in extra_conditions or []:
        stmt = stmt.where(condition)

    counts: Dict[str, int] = {}
    for day, cnt in session.execute(stmt).all():
        counts[_normalize_day_key(day)] = int(cnt)
    return counts


def _normalize_day_key(day: Any) -> str:
    """将 ``func.date`` 返回的日期值规整为 ``YYYY-MM-DD`` 字符串。

    不同数据库后端 ``func.date`` 的返回类型不一（MySQL 返回 date，SQLite 返回 str），
    此处统一规整为日期字符串作为映射键。

    Args:
        day: ``func.date`` 返回的日期值（date / datetime / str）。

    Returns:
        ``YYYY-MM-DD`` 日期字符串。
    """
    if isinstance(day, datetime):
        return day.strftime(DATE_FORMAT)
    if isinstance(day, str):
        return day[:10]
    # date 对象或其它可格式化对象
    return day.strftime(DATE_FORMAT) if hasattr(day, "strftime") else str(day)[:10]


def get_trend(
    session: Session,
    user: SysUser,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> ApiResponse:
    """返回时间范围内按天聚合的消息量与回复量趋势（需求 20.2，北京时间口径 20.3）。

    回复量统计处理结果为「自动回复」或「AI 回复」的消息日志。结果按日期升序返回，
    范围内无数据的日期以 0 补齐，便于前端绘制连续折线。各项均受数据范围隔离约束
    （需求 3.7）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        start_date: 起始日期（YYYY-MM-DD）；None 时默认最近 7 天起。
        end_date: 结束日期（YYYY-MM-DD）；None 时默认今日。

    Returns:
        统一响应体：data 含 {start_date, end_date, points:[{date, messages, replies}]}。
    """
    error, start_dt, end_dt = resolve_trend_range(start_date, end_date)
    if error is not None:
        return error

    shop_ids = visible_shop_ids(session, user)
    # 聚合区间为 [start_dt, end_dt 次日 00:00)，确保包含结束日全天。
    end_exclusive = end_dt + timedelta(days=1)

    message_counts = _daily_counts(session, shop_ids, start_dt, end_exclusive)
    reply_counts = _daily_counts(
        session,
        shop_ids,
        start_dt,
        end_exclusive,
        extra_conditions=[
            MessageLog.process_result.in_(
                [PROCESS_RESULT_AUTO_REPLY, PROCESS_RESULT_AI_REPLY]
            )
        ],
    )

    # 按自然日逐日补齐，范围内无数据的日期以 0 填充。
    points: List[Dict[str, Any]] = []
    cursor = start_dt
    while cursor <= end_dt:
        key = cursor.strftime(DATE_FORMAT)
        points.append(
            {
                "date": key,
                "messages": message_counts.get(key, 0),
                "replies": reply_counts.get(key, 0),
            }
        )
        cursor += timedelta(days=1)

    data = {
        "start_date": start_dt.strftime(DATE_FORMAT),
        "end_date": end_dt.strftime(DATE_FORMAT),
        "points": points,
    }
    return success_response(data=data, message="查询成功")


__all__ = [
    "SHOP_STATUS_ENABLED",
    "PROCESS_RESULT_AUTO_REPLY",
    "PROCESS_RESULT_AI_REPLY",
    "MAX_TREND_DAYS",
    "DEFAULT_TREND_DAYS",
    "get_overview",
    "get_trend",
]
