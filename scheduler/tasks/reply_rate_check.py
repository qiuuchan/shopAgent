# -*- coding: utf-8 -*-
"""
scheduler.tasks.reply_rate_check —— 回复率跌破阈值巡检与企微告警（TIK-026）
============================================================================
本文件用途：为 scheduler 服务定义「回复率巡检」任务（``reply_rate_check``），
周期检查各 TikTok 店铺近 24 小时回复率，跌破阈值时经既有通知链路企微告警，
恢复后解除静默可再告警（对齐 PLAN §8 Phase 3 出口「24h 回复率 ≥85%」）。

告警链路（零新增服务，TIK-026 验收）：
- 回复率口径与看板 / 对账工具**共用同一实现** ``common.utils.latency.reply_rate``
  （TIK-025 上移的纯函数），保证「看板看到的」与「告警判的」是同一个数；
- 去重防抖**复用 TIK-005 组件语义**：``common.utils.alert_dedup.AlertDedup``
  按 ``(shop_pk, event_type)`` 维度静默去重（默认 30 分钟），恢复（回复率回到
  阈值之上）时 ``resolve`` 清除静默，使再次跌破可立即再告警；
- 发送经 ``service_client.trigger_notify_event`` 调 backend 内部接口
  ``/api/v1/internal/notify-events``（携带 ``X-Internal-Token`` 鉴权），由 backend
  按「店铺 + 已启用通知渠道」推送（企微群机器人等）并落 ``pdd_notify_record``。

判定语义（对齐 TIK-025 口径）：
- 回复率 = (已回复周期数 - 首响超时周期数) / (已回复周期数 + 待回复周期数)；
- 跌破阈值 → 尝试告警（静默期内去重跳过）；回到阈值之上 → resolve；
- 无任何周期（无数据）→ 既不告警也不 resolve（无数据不算跌破、不干扰状态）。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、文件名用下划线（40）、
单文件 ≤500 行（35）、参数化查询（16）、时间统一北京时间（17）。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.db.repository import Repository, run_with_retry
from common.models.log_models import ChatMessage
from common.models.shop_models import Shop
from common.utils.alert_dedup import AlertDedup, DEFAULT_SILENCE_SECONDS
from common.utils.latency import (
    FIRST_RESPONSE_THRESHOLD_SECONDS,
    compute_cycles,
    first_response_stats,
    reply_rate,
)
from common.utils.time_utils import now_beijing

from tasks import service_client, task_run_log
from tasks.constants import RESULT_SUCCESS, TASK_REPLY_RATE_CHECK

# 模块级日志记录器（禁用 debug 级别 —— 规范 38）。
logger = logging.getLogger("scheduler.reply_rate_check")

# 回复率告警阈值（Phase 3 出口「24h 回复率 ≥85%」，TIK-026；与 PLAN §8 一致）。
REPLY_RATE_THRESHOLD: float = 0.85

# 巡检窗口（小时）：滚动 24 小时，对齐 Phase 3 出口口径「24h 回复率」。
REPLY_RATE_WINDOW_HOURS: float = 24.0

# 告警事件类型（须与 backend notify_service.EVENT_TYPE_LABELS 已注册键一致，
# 否则被白名单拒绝；取值改动须同步两侧）。
EVENT_REPLY_RATE_BELOW_THRESHOLD: str = "reply_rate_below_threshold"

# 店铺「启用」状态值（与 shop_models.Shop.status 约定一致：1=启用）。
_SHOP_STATUS_ENABLED: int = 1

# 告警文案模板：跌破阈值时推送给企微的内容（含店铺、实测回复率与阈值）。
_ALERT_TEMPLATE: str = (
    "【回复率告警】店铺[{shop_name}]（shop_pk={shop_pk}）近 24 小时回复率 "
    "{rate_percent:.2f}%，低于阈值 {threshold_percent:.0f}%，请及时关注。"
)

# 本任务去重器：进程内单例（scheduler 单进程常驻模型），复用 TIK-005 组件语义。
# 以 (shop_pk, event_type) 为维度静默去重；测试可经 ``clear()`` 重置。
_reply_rate_dedup: AlertDedup = AlertDedup(silence_seconds=DEFAULT_SILENCE_SECONDS)


# ----------------------------------------------------------------------
# 纯逻辑：按回复率与阈值得出动作
# ----------------------------------------------------------------------
def plan_reply_rate_actions(
    shop_rates: Dict[int, Tuple[str, Optional[float]]],
    threshold: float = REPLY_RATE_THRESHOLD,
) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]], List[Tuple[int, str]]]:
    """按每店回复率与阈值给出动作候选（纯函数，便于单测）。

    Args:
        shop_rates: 店铺回复率映射 {shop_pk: (shop_id, rate)}；rate 为 None
            表示无数据（无任何回复周期）。
        threshold: 告警阈值（0~1）；跌破（rate < threshold）判告警。

    Returns:
        ``(alert_candidates, resolve_candidates, no_data)`` 三元组，每项均为
        ``[(shop_pk, shop_id), ...]`` 列表：
        - alert_candidates：回复率跌破阈值的店铺（拟告警，最终是否发送由
          去重器决定）；
        - resolve_candidates：回复率回到阈值之上或持平的店铺（拟解除静默，
          使再次跌破可立即再告警）；
        - no_data：无任何回复周期的店铺（不告警也不 resolve，无数据不干扰状态）。
    """
    alert_candidates: List[Tuple[int, str]] = []
    resolve_candidates: List[Tuple[int, str]] = []
    no_data: List[Tuple[int, str]] = []
    for shop_pk, (shop_id, rate) in shop_rates.items():
        if rate is None:
            no_data.append((shop_pk, shop_id))
        elif rate < threshold:
            alert_candidates.append((shop_pk, shop_id))
        else:
            resolve_candidates.append((shop_pk, shop_id))
    return alert_candidates, resolve_candidates, no_data


# ----------------------------------------------------------------------
# 数据层：启用 TikTok 店铺 + 窗口内消息（参数化查询，规范 16）
# ----------------------------------------------------------------------
def _list_enabled_tiktok_shops(
    session: Session,
) -> List[Tuple[int, str, str, Optional[int]]]:
    """查询全部启用且属于 TikTok 平台的店铺。

    Args:
        session: 事务性会话（由 run_with_retry 管理）。

    Returns:
        ``[(shop_pk, shop_id, shop_name, owner_user_id), ...]`` 列表。
    """
    shops = Repository(Shop, session).list(
        filters={"status": _SHOP_STATUS_ENABLED, "platform": "tiktok"}
    )
    return [
        (shop.id, shop.shop_id, shop.shop_name or shop.shop_id, shop.owner_user_id)
        for shop in shops
    ]


def _query_window_messages(
    session: Session,
    shop_pks: List[int],
    start: datetime,
    end_exclusive: datetime,
) -> List[Tuple[int, str, str, datetime]]:
    """查询窗口内全部 TikTok 店铺的聊天消息（按店铺 / 客户 / 时间升序）。

    仅取统计所需四列，避免把消息正文等大字段读进内存；msg_time 为空的行不参与
    统计（与 backend first_response_service 查询口径一致）。

    Args:
        session: 数据库会话。
        shop_pks: 店铺主键列表。
        start: 窗口下界（含），北京时间。
        end_exclusive: 窗口上界（不含），北京时间。

    Returns:
        ``[(shop_pk, customer_uid, direction, msg_time), ...]`` 列表。
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


def _aggregate_reply_rate(
    messages: List[Tuple[int, str, str, datetime]],
) -> Dict[int, Optional[float]]:
    """把窗口内消息按「店铺 + 客户」切分周期，返回每店回复率（纯聚合，无 I/O）。

    与 backend first_response_service 同款聚合（复用 common.utils.latency）：
    周期切分 → 首响统计（responded / 超时 / pending）→ reply_rate；无任何周期
    的店铺返回 None（无数据）。

    Args:
        messages: ``(shop_pk, customer_uid, direction, msg_time)`` 列表，须已按
            店铺 / 客户 / 时间升序。

    Returns:
        ``{shop_pk: rate}``；rate 为 None 表示该店窗口内无任何回复周期。
    """
    by_conversation: Dict[Tuple[int, str], List[Tuple[str, datetime]]] = defaultdict(list)
    for shop_pk, customer_uid, direction, msg_time in messages:
        by_conversation[(shop_pk, customer_uid)].append((direction, msg_time))

    accumulator: Dict[int, Dict[str, Any]] = {}
    for (shop_pk, _customer_uid), conversation in by_conversation.items():
        cycles, pending = compute_cycles(conversation)
        entry = accumulator.setdefault(shop_pk, {"cycles": [], "pending": []})
        entry["cycles"].extend(cycles)
        entry["pending"].extend(pending)

    result: Dict[int, Optional[float]] = {}
    for shop_pk, entry in accumulator.items():
        stats = first_response_stats(
            entry["cycles"],
            entry["pending"],
            threshold_seconds=FIRST_RESPONSE_THRESHOLD_SECONDS,
        )
        result[shop_pk] = reply_rate(
            stats["responded_cycles"],
            stats["over_threshold_count"],
            stats["pending_cycles"],
        )
    return result


# ----------------------------------------------------------------------
# 执行体：周期巡检 + 去重 + 告警
# ----------------------------------------------------------------------
def run_reply_rate_check(
    now: Optional[datetime] = None,
    threshold: float = REPLY_RATE_THRESHOLD,
) -> None:
    """回复率巡检任务执行体（TIK-026，周期建议 1 小时）。

    1. 取全部启用且 ``platform='tiktok'`` 的店铺；
    2. 以北京时间计算滚动 24 小时窗口，逐店聚合回复率（与看板 / 对账同一口径）；
    3. 跌破阈值 → 经 ``AlertDedup`` 静默去重后调 backend 内部通知接口企微告警
       （静默期内跳过，防刷屏）；回到阈值之上 → ``resolve`` 解除静默，使再次
       跌破可立即再告警；无数据店铺不动作；
    4. 汇总后写一条执行日志；告警推送失败只记失败日志不中断整体巡检。

    PDD 店铺不受影响（按 platform 过滤，不触碰）。

    Args:
        now: 巡检参考时刻（窗口上界）；默认取当前北京时间（None）。仅供测试
            注入固定时刻，调度器调用无需传参。
        threshold: 告警阈值（0~1），默认 0.85；供测试注入边界值。
    """
    try:
        shops = run_with_retry(_list_enabled_tiktok_shops)
    except Exception as exc:  # noqa: BLE001 —— 取店铺失败：记失败日志后返回
        logger.error("回复率巡检任务取 TikTok 店铺列表失败：%s", exc)
        task_run_log.write_failed(
            TASK_REPLY_RATE_CHECK, f"取 TikTok 店铺列表失败：{exc}"
        )
        return

    if not shops:
        task_run_log.write_success(TASK_REPLY_RATE_CHECK, "无启用 TikTok 店铺，跳过回复率巡检")
        return

    # 滚动 24 小时窗口（北京时间口径，规范 17）。
    now = now or now_beijing()
    start = now - timedelta(hours=REPLY_RATE_WINDOW_HOURS)

    shop_rates: Dict[int, Tuple[str, Optional[float]]] = {}
    try:
        messages = run_with_retry(
            lambda s, pks=[s[0] for s in shops], st=start, en=now: _query_window_messages(
                s, pks, st, en
            )
        )
        rates = _aggregate_reply_rate(messages)
    except Exception as exc:  # noqa: BLE001 —— 查询失败：记失败日志后返回
        logger.error("回复率巡检任务查询消息失败：%s", exc)
        task_run_log.write_failed(TASK_REPLY_RATE_CHECK, f"查询窗口消息失败：{exc}")
        return

    for shop_pk, shop_id, _shop_name, _owner_user_id in shops:
        shop_rates[shop_pk] = (shop_id, rates.get(shop_pk))

    alert_candidates, resolve_candidates, no_data = plan_reply_rate_actions(
        shop_rates, threshold=threshold
    )

    # 逐店处置：告警（去重后发送）/ 恢复（解除静默）/ 无数据（不动）。
    name_by_pk = {pk: name for pk, _sid, name, _oid in shops}
    alert_sent = 0
    alert_deduped = 0
    alert_failed = 0
    alert_detail: List[str] = []
    for shop_pk, shop_id in alert_candidates:
        if not _reply_rate_dedup.should_send(shop_pk, EVENT_REPLY_RATE_BELOW_THRESHOLD):
            alert_deduped += 1
            continue
        # 先记静默再发送：与 TIK-005 语义一致，发送失败也防抖，避免重试风暴。
        _reply_rate_dedup.mark_sent(shop_pk, EVENT_REPLY_RATE_BELOW_THRESHOLD)
        rate = shop_rates[shop_pk][1] or 0.0
        content = _ALERT_TEMPLATE.format(
            shop_name=name_by_pk.get(shop_pk, shop_id),
            shop_pk=shop_pk,
            rate_percent=rate * 100.0,
            threshold_percent=threshold * 100.0,
        )
        result = service_client.trigger_notify_event(
            EVENT_REPLY_RATE_BELOW_THRESHOLD, content, shop_pk
        )
        if result.ok:
            alert_sent += 1
            logger.warning(
                "回复率告警已推送：店铺[%s]（shop_pk=%s）回复率 %.2f%% 低于阈值",
                shop_id, shop_pk, rate * 100.0,
            )
        else:
            alert_failed += 1
            logger.warning(
                "回复率告警推送失败：店铺[%s]（shop_pk=%s）：%s",
                shop_id, shop_pk, result.message,
            )
        alert_detail.append(
            f"店铺[{shop_id}] 回复率 {rate * 100.0:.2f}%"
            f"（{'已推送' if result.ok else '推送失败'}）"
        )

    for shop_pk, _shop_id in resolve_candidates:
        _reply_rate_dedup.resolve(shop_pk, EVENT_REPLY_RATE_BELOW_THRESHOLD)

    message = (
        f"回复率巡检完成：检查 {len(shops)} 店，跌破阈值 {len(alert_candidates)} 店"
        f"（已推送 {alert_sent}，静默去重 {alert_deduped}，失败 {alert_failed}），"
        f"恢复 {len(resolve_candidates)} 店，无数据 {len(no_data)} 店"
    )
    if alert_detail:
        message += "；" + "；".join(alert_detail)
    # 告警推送失败视为本次巡检失败，便于运维感知；静默去重与无数据属正常状态。
    if alert_failed > 0:
        task_run_log.write_failed(TASK_REPLY_RATE_CHECK, message)
    else:
        task_run_log.write_success(TASK_REPLY_RATE_CHECK, message)


__all__ = [
    "REPLY_RATE_THRESHOLD",
    "REPLY_RATE_WINDOW_HOURS",
    "EVENT_REPLY_RATE_BELOW_THRESHOLD",
    "plan_reply_rate_actions",
    "run_reply_rate_check",
]


# ----------------------------------------------------------------------
# 手动触发入口（演练 / 人工巡检）：python -m scheduler.tasks.reply_rate_check
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # 直接执行一次巡检（读系统配置的数据库，与调度器调用同路径）。
    run_reply_rate_check()
