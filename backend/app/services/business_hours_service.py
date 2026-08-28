# -*- coding: utf-8 -*-
"""
backend.app.services.business_hours_service —— 营业时间配置业务服务
==================================================================
本文件用途：实现 backend 服务的「营业时间配置」业务逻辑，供 business_hours
路由复用，满足需求 11.1：

- ``configure_business_hours(...)``：配置店铺营业时间的起止时刻并持久化
  （需求 11.1）。按店铺主键 ``shop_pk`` 作为业务键 upsert（同一店铺仅一条
  营业时间配置，重复配置覆盖更新，幂等），返回统一响应体。
- ``get_business_hours(...)``：查询某店铺的营业时间配置；未配置时返回空配置
  （业务侧默认全天营业，需求 11.4 由 websocket 引擎判定）。

营业时间模型说明（common.models.config_models.BusinessHours）：
- ``start_time`` / ``end_time`` 为 ``Time`` 字段，按北京时间口径存储起止时刻；
- 允许跨午夜区间（如 22:00~02:00），具体判定逻辑在 websocket 引擎实现；
- ``start_time`` / ``end_time`` 均可为空，表示未配置（默认全天，需求 11.4）；
- ``enabled`` 控制该配置是否启用。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3 / 需求 24.1）。
- 所有数据访问经 common.db.repository 的参数化查询，禁止拼接 SQL（规范 16）。
- 时间统一北京时间口径（规范 17 / 需求 24.8）。
- 禁止物理删除业务数据；配置变更经 upsert 覆盖更新（规范 11 / 需求 24.6）。
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.business_codes import CODE_PARAM_ERROR
from app.core.data_scope import ensure_shop_in_scope
from common.db.repository import Repository
from common.models.config_models import BusinessHours
from common.schemas.common import ApiResponse, error_response, success_response
from common.utils.weekdays import normalize_weekdays, validate_weekdays

# 支持解析的时刻字符串格式：优先 HH:MM:SS，其次 HH:MM（北京时间口径）。
_TIME_FORMATS: tuple[str, ...] = ("%H:%M:%S", "%H:%M")
# 对外序列化营业时刻使用的统一格式（HH:MM:SS）。
_TIME_OUTPUT_FORMAT: str = "%H:%M:%S"


def _parse_time(value: Any) -> tuple[bool, Optional[time]]:
    """将入参解析为 ``datetime.time``（北京时间口径的时刻）。

    支持「HH:MM」与「HH:MM:SS」两种字符串；None 或空串表示「不设置该时刻」
    （返回 (True, None)，业务侧视为未配置 → 默认全天，需求 11.4）。

    Args:
        value: 待解析的时刻值（字符串 / None）。

    Returns:
        二元组 (是否合法, 解析结果)。合法且为空时返回 (True, None)；合法且
        有值时返回 (True, time)；非法时返回 (False, None)。
    """
    # 未提供：视为合法的「不设置」，由业务侧按未配置处理（默认全天）。
    if value is None:
        return True, None
    if isinstance(value, time):
        return True, value
    if not isinstance(value, str):
        return False, None
    text = value.strip()
    if not text:
        return True, None
    # 逐一尝试受支持的格式，命中即返回。
    for fmt in _TIME_FORMATS:
        try:
            return True, datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return False, None


def _format_time(value: Optional[time]) -> Optional[str]:
    """将 ``datetime.time`` 序列化为统一的「HH:MM:SS」字符串（空值返回 None）。

    Args:
        value: 时刻值或 None。

    Returns:
        格式化后的时刻字符串；入参为空时返回 None。
    """
    if value is None:
        return None
    return value.strftime(_TIME_OUTPUT_FORMAT)


def serialize_business_hours(record: BusinessHours) -> Dict[str, Any]:
    """将营业时间配置模型序列化为对外字典。

    Args:
        record: 营业时间配置模型实例。

    Returns:
        营业时间配置信息字典（时刻以 HH:MM:SS 字符串表达，北京时间口径）。
    """
    return {
        "id": record.id,
        "shop_pk": record.shop_pk,
        "start_time": _format_time(record.start_time),
        "end_time": _format_time(record.end_time),
        "weekdays": record.weekdays or "",
        "enabled": bool(record.enabled),
    }


def configure_business_hours(
    session: Session,
    shop_pk: int,
    start_time: Any = None,
    end_time: Any = None,
    *,
    weekdays: Any = None,
    enabled: bool = True,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """配置并持久化店铺营业时间的起止时刻与星期维度（需求 11.1）。

    按店铺主键 ``shop_pk`` 作为业务键 upsert：同一店铺仅保留一条营业时间配置，
    重复配置覆盖更新（幂等）。起止时刻按「HH:MM」/「HH:MM:SS」解析（北京时间
    口径）；允许仅设置其一或均为空（均为空表示未配置，业务侧默认全天）。星期维度
    ``weekdays`` 为逗号分隔的星期序号（1=周一 … 7=周日），如 ``"1,2,3,4,5"`` 表示
    周一至周五；空 / None 表示「每天」（与旧数据兼容，写入归一为空串）。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        start_time: 营业开始时刻（字符串 HH:MM/HH:MM:SS，或为空表示不设置）。
        end_time: 营业结束时刻（字符串 HH:MM/HH:MM:SS，或为空表示不设置）。
        weekdays: 营业星期维度表达式（"1,2,3,4,5"），空 / None 表示每天。
        enabled: 该配置是否启用，默认启用。
        operator_id: 操作人用户 ID，作为创建人审计字段（仅新建时记录）。

    Returns:
        统一响应体：成功返回 data=营业时间配置；失败返回对应中文提示。
    """
    # 入参校验：店铺主键必填且为正整数。
    if shop_pk is None or not isinstance(shop_pk, int) or shop_pk <= 0:
        return error_response(CODE_PARAM_ERROR, "店铺主键不能为空")

    # 数据范围隔离：店铺需存在且在当前用户可见范围内（需求 3.7 / 规范 42a）。
    denied = ensure_shop_in_scope(session, shop_pk, operator_id)
    if denied is not None:
        return denied

    # 解析起止时刻（北京时间口径），任一非法即返回明确中文提示。
    start_ok, start_value = _parse_time(start_time)
    if not start_ok:
        return error_response(
            CODE_PARAM_ERROR, "营业开始时刻格式无效，应为 HH:MM 或 HH:MM:SS"
        )
    end_ok, end_value = _parse_time(end_time)
    if not end_ok:
        return error_response(
            CODE_PARAM_ERROR, "营业结束时刻格式无效，应为 HH:MM 或 HH:MM:SS"
        )

    # 星期维度格式校验：非法格式（非整数段 / 越界序号 / 空段）抛 BusinessError，
    # 由统一异常处理器兜底为 HTTP 200 + 失败响应体，不抛 4xx/5xx。
    try:
        validate_weekdays(weekdays)
    except ValueError as exc:
        return error_response(CODE_PARAM_ERROR, f"营业星期维度格式无效：{exc}")
    weekdays_value = normalize_weekdays(weekdays)

    repo = Repository(BusinessHours, session)
    # 按 shop_pk upsert：存在则更新起止时刻与启用状态，不存在则新建。
    existing = repo.get_by(shop_pk=shop_pk)
    if existing is None:
        record = repo.create(
            shop_pk=shop_pk,
            start_time=start_value,
            end_time=end_value,
            weekdays=weekdays_value,
            enabled=bool(enabled),
            created_by=operator_id,
        )
    else:
        record = repo.update(
            existing.id,
            start_time=start_value,
            end_time=end_value,
            weekdays=weekdays_value,
            enabled=bool(enabled),
        )

    return success_response(
        data=serialize_business_hours(record),
        message="营业时间已保存",
    )


def get_business_hours(
    session: Session, shop_pk: int, *, operator_id: Optional[int] = None
) -> ApiResponse:
    """查询某店铺的营业时间配置（需求 11.1 配套）。

    未配置时返回 ``data=None``（业务侧默认全天营业，需求 11.4 由 websocket
    引擎判定），便于前端区分「未配置」与「已配置」。非管理员仅可查看其可见
    范围内店铺的配置（数据范围隔离，需求 3.7 / 规范 42a）。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        operator_id: 当前操作用户 ID（数据范围校验）。

    Returns:
        统一响应体：已配置返回营业时间配置；未配置返回 data=None。
    """
    if shop_pk is None or not isinstance(shop_pk, int) or shop_pk <= 0:
        return error_response(CODE_PARAM_ERROR, "店铺主键不能为空")

    # 数据范围隔离：店铺需存在且在当前用户可见范围内（需求 3.7 / 规范 42a）。
    denied = ensure_shop_in_scope(session, shop_pk, operator_id)
    if denied is not None:
        return denied

    record = Repository(BusinessHours, session).get_by(shop_pk=shop_pk)
    if record is None:
        # 未配置：返回 None，业务侧按「默认全天」处理。
        return success_response(data=None, message="未配置营业时间")
    return success_response(
        data=serialize_business_hours(record),
        message="查询成功",
    )


__all__ = [
    "serialize_business_hours",
    "configure_business_hours",
    "get_business_hours",
]
