# -*- coding: utf-8 -*-
"""
scheduler.tasks.task_runners —— 各定时任务执行体
================================================
本文件用途：为 scheduler 服务定义定时任务的「执行体」，被 SchedulerService
按 ``scheduled_task`` 配置注册到调度器并周期触发（需求 21.2 / 21.4）：

- ``run_cookie_refresh``：遍历启用店铺，经 HTTP 调用 websocket 服务刷新 Cookie；
- ``run_product_sync``：遍历启用店铺，经 HTTP 调用 backend 服务触发商品同步；
- ``run_log_file_cleanup``：按保留天数仅清理磁盘日志文件（数据库业务日志表禁止
  物理删除）；
- ``run_tiktok_window``：周期比对 TikTok 店铺营业时间期望态与连接实态，收敛
  connect / disconnect（需求 24.x，TIK-015）。

每个执行体均：
1. 完成各自业务；
2. 经 ``task_run_log`` 写一条 success / failed 执行日志（需求 21.2）；
3. 自身不向上抛异常（捕获后记失败日志），避免单次任务异常影响调度器存活。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）、参数化查询（规范 16）、连接失败重试
（规范 13）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from common.db.repository import Repository, run_with_retry
from common.models.config_models import BusinessHours
from common.models.shop_models import Shop
from common.utils.business_hours import is_within_business_hours

from tasks import log_cleanup, reply_rate_check, service_client, task_run_log
from tasks.constants import (
    TASK_COOKIE_REFRESH,
    TASK_LOG_FILE_CLEANUP,
    TASK_PRODUCT_SYNC,
    TASK_REPLY_RATE_CHECK,
    TASK_TIKTOK_WINDOW,
)

# 模块级日志记录器（禁用 debug 级别 —— 规范 38）。
logger = logging.getLogger("scheduler.task_runners")

# 店铺「启用」状态值（与 shop_models.Shop.status 约定一致：1=启用）。
_SHOP_STATUS_ENABLED: int = 1

# 登录态巡检结果取值（TIK-023，跨服务契约）：与 websocket 侧
# channel_tiktok.login_probe 的常量一一对应。scheduler 不导入 websocket 模块，
# 仅按字符串取值比对，避免跨服务耦合。取值改动须同步两侧。
_LOGIN_PROBE_OK: str = "ok"
_LOGIN_PROBE_LABELS: Dict[str, str] = {
    "ok": "登录态有效",
    "login_expired": "主站登录态失效（跳登录页）",
    "im_expired": "IM 会话过期弹窗",
    "page_dead": "页面存活探测失败（浏览器已退出）",
    "channel_absent": "无活跃页面（未连接或窗口外）",
    "unknown": "探测异常（不误报，待复核）",
}


def _list_enabled_shops(session: Session) -> List[Tuple[int, str, int | None]]:
    """查询全部启用状态的店铺（参数化查询）。

    Args:
        session: 事务性会话（由 run_with_retry 管理）。

    Returns:
        列表，每项为 (shop_pk, shop_id, owner_user_id) 三元组。
    """
    shops = Repository(Shop, session).list(filters={"status": _SHOP_STATUS_ENABLED})
    return [(shop.id, shop.shop_id, shop.owner_user_id) for shop in shops]


def _list_enabled_shops_with_platform(
    session: Session,
) -> List[Tuple[int, str, int | None, str]]:
    """查询全部启用店铺并带平台标识（Cookie 刷新任务专用，TIK-016 Phase 2 保活）。

    在 ``_list_enabled_shops`` 三元组基础上附加 ``platform``（缺省 'pdd'，向后兼容
    存量无平台字段的店铺），使 ``run_cookie_refresh`` 能按平台透传给 websocket 侧，
    避免 TikTok 店铺被误走 PDD 刷新路径（TikTok 登录态常驻浏览器目录，跳过刷新）。

    Args:
        session: 事务性会话（由 run_with_retry 管理）。

    Returns:
        列表，每项为 (shop_pk, shop_id, owner_user_id, platform) 四元组。
    """
    shops = Repository(Shop, session).list(filters={"status": _SHOP_STATUS_ENABLED})
    return [
        (
            shop.id,
            shop.shop_id,
            shop.owner_user_id,
            str(getattr(shop, "platform", None) or "pdd").strip(),
        )
        for shop in shops
    ]


def run_cookie_refresh() -> None:
    """Cookie 刷新任务执行体（需求 4.6 / 21.2）。

    遍历全部启用店铺（含 TikTok），按平台透传刷新请求：PDD 店铺经 websocket 侧
    ``refresh_pdd_cookies`` 实际刷新；TikTok 店铺登录态常驻浏览器目录，websocket 侧
    跳过刷新并回传登录态巡检结果（Phase 2 保活 + TIK-023 过期周期观测打点）。
    汇总成功 / 失败数量并写一条执行日志；TikTok 店铺巡检异常时另附明细，串起
    「何时仍正常 / 何时已失效」的时间线供登录态过期周期观测取数。
    任一店铺调用失败不中断整体遍历。
    """
    try:
        shops = run_with_retry(_list_enabled_shops_with_platform)
    except Exception as exc:  # noqa: BLE001 —— 取店铺失败：记失败日志后返回
        logger.error("Cookie 刷新任务取店铺列表失败：%s", exc)
        task_run_log.write_failed(TASK_COOKIE_REFRESH, f"取店铺列表失败：{exc}")
        return

    if not shops:
        task_run_log.write_success(TASK_COOKIE_REFRESH, "无启用店铺，跳过 Cookie 刷新")
        return

    success_count = 0
    failed_count = 0
    # TikTok 店铺登录态巡检异常明细（TIK-023 观测数据源）。
    probe_notes: List[str] = []
    for shop_pk, shop_id, owner_user_id, platform in shops:
        result = service_client.trigger_cookie_refresh(
            shop_pk, shop_id, owner_user_id, platform=platform
        )
        if result.ok:
            success_count += 1
            _collect_login_probe_note(result, shop_id, platform, probe_notes)
        else:
            failed_count += 1
            logger.warning("店铺[%s] Cookie 刷新失败：%s", shop_id, result.message)

    message = f"Cookie 刷新完成：成功 {success_count} 个，失败 {failed_count} 个"
    if probe_notes:
        message += "；" + "；".join(probe_notes)
    # 只要有失败店铺即记为 failed，便于运维感知（但任务本身已尽力执行全部店铺）。
    # 巡检异常不改变成败语义：巡检仅观测，异常由告警链路与主循环各自处置。
    if failed_count > 0:
        task_run_log.write_failed(TASK_COOKIE_REFRESH, message)
    else:
        task_run_log.write_success(TASK_COOKIE_REFRESH, message)


def _collect_login_probe_note(
    result: Any, shop_id: str, platform: str, probe_notes: List[str]
) -> None:
    """收集 TikTok 店铺的登录态巡检异常明细（TIK-023 观测，纯整理无 I/O）。

    仅 TikTok 店铺回传 ``data.login_probe``；取值为 ``ok`` 表示登录态有效不记录
    （避免执行日志被常态打点刷屏），其余取值按「店铺[xx] 巡检异常：中文说明」
    追加到 ``probe_notes``，随本次任务的执行日志一并落库。

    Args:
        result: websocket 侧返回的 ``CallResult``（含可选 data.login_probe）。
        shop_id: 店铺业务标识（仅用于日志定位）。
        platform: 平台标识（仅 tiktok 才可能有巡检结果）。
        probe_notes: 待追加的巡检明细列表（原地追加）。
    """
    if platform != "tiktok":
        return
    probe = (result.data or {}).get("login_probe")
    if not probe or probe == _LOGIN_PROBE_OK:
        return
    label = _LOGIN_PROBE_LABELS.get(probe, probe)
    logger.warning("店铺[%s] TikTok 登录态巡检异常：%s", shop_id, label)
    probe_notes.append(f"店铺[{shop_id}] 巡检异常：{label}")


def run_product_sync() -> None:
    """商品同步任务执行体（需求 15.2 / 21.2）。

    遍历全部启用店铺，逐个经 HTTP 调用 backend 服务触发商品同步；汇总成功 /
    失败数量并写一条执行日志。任一店铺调用失败不中断整体遍历。
    """
    try:
        shops = run_with_retry(_list_enabled_shops)
    except Exception as exc:  # noqa: BLE001 —— 取店铺失败：记失败日志后返回
        logger.error("商品同步任务取店铺列表失败：%s", exc)
        task_run_log.write_failed(TASK_PRODUCT_SYNC, f"取店铺列表失败：{exc}")
        return

    if not shops:
        task_run_log.write_success(TASK_PRODUCT_SYNC, "无启用店铺，跳过商品同步")
        return

    success_count = 0
    failed_count = 0
    for shop_pk, shop_id, _owner_user_id in shops:
        result = service_client.trigger_product_sync(shop_pk)
        if result.ok:
            success_count += 1
        else:
            failed_count += 1
            logger.warning("店铺[%s] 商品同步失败：%s", shop_id, result.message)

    message = f"商品同步完成：成功 {success_count} 个，失败 {failed_count} 个"
    if failed_count > 0:
        task_run_log.write_failed(TASK_PRODUCT_SYNC, message)
    else:
        task_run_log.write_success(TASK_PRODUCT_SYNC, message)


def run_log_file_cleanup() -> None:
    """文件日志清理任务执行体（需求 21.4）。

    按系统设置的「日志保留天数」**仅清理磁盘日志文件**；数据库业务日志表禁止
    物理删除（规范 11 / 需求 19.5）。执行结果写一条执行日志。
    """
    try:
        retention_days = log_cleanup.resolve_retention_days()
        removed = log_cleanup.cleanup_log_files(retention_days)
        message = (
            f"文件日志清理完成：保留 {retention_days} 天，删除磁盘日志文件 "
            f"{len(removed)} 个（数据库业务日志未做任何删除）"
        )
        task_run_log.write_success(TASK_LOG_FILE_CLEANUP, message)
    except Exception as exc:  # noqa: BLE001 —— 清理失败：记失败日志，不抛出
        logger.error("文件日志清理任务失败：%s", exc)
        task_run_log.write_failed(TASK_LOG_FILE_CLEANUP, f"文件日志清理失败：{exc}")


def _list_enabled_tiktok_shops(
    session: Session,
) -> List[Tuple[int, str, Optional[int], Optional[str]]]:
    """查询全部启用且属于 TikTok 平台的店铺（参数化查询）。

    Args:
        session: 事务性会话（由 run_with_retry 管理）。

    Returns:
        列表，每项为 (shop_pk, shop_id, owner_user_id, proxy_server) 四元组
        （proxy_server 为 Phase 2 前置店铺级出口代理，空串归一为 None）。
    """
    shops = Repository(Shop, session).list(
        filters={"status": _SHOP_STATUS_ENABLED, "platform": "tiktok"}
    )
    return [
        (shop.id, shop.shop_id, shop.owner_user_id, shop.proxy_server or None)
        for shop in shops
    ]


def _load_business_hours(session: Session, shop_pk: int) -> Optional[Dict[str, Any]]:
    """查询指定店铺的营业时间配置（含 weekdays 星期维度）。

    Args:
        session: 事务性会话（由 run_with_retry 管理）。
        shop_pk: 店铺主键（shop.id）。

    Returns:
        配置字典（含 ``start_time`` / ``end_time`` / ``enabled`` / ``weekdays``）；
        无配置时返回 None（判定侧视为全天营业）。
    """
    record = Repository(BusinessHours, session).get_by(shop_pk=shop_pk)
    if record is None:
        return None
    return {
        "start_time": record.start_time,
        "end_time": record.end_time,
        "enabled": bool(record.enabled),
        "weekdays": record.weekdays or "",
    }


def plan_window_actions(
    expected_states: List[Tuple[int, str, Optional[int], bool]],
    actual_statuses: Dict[str, bool],
) -> List[Tuple[str, int, str, Optional[int]]]:
    """比对「期望连接态」与「实际连接态」得出收敛动作（纯函数，便于属性测试）。

    语义（TIK-015）：期望连接而实际未连接 → ``connect``；期望断开而实际已连接 →
    ``disconnect``；两者一致时不产生动作。收敛方向幂等：重复执行不产生多余动作。

    Args:
        expected_states: 期望态列表，每项 (shop_pk, shop_id, owner_user_id,
            should_connect)。
        actual_statuses: 实际连接态映射 {shop_id: connected}（经 status-batch
            查询得到）。

    Returns:
        动作列表，每项 (action, shop_pk, shop_id, owner_user_id)，
        action 为 ``"connect"`` / ``"disconnect"``。
    """
    actions: List[Tuple[str, int, str, Optional[int]]] = []
    for shop_pk, shop_id, owner_user_id, should_connect in expected_states:
        actual = bool(actual_statuses.get(shop_id, False))
        if should_connect and not actual:
            actions.append(("connect", shop_pk, shop_id, owner_user_id))
        elif not should_connect and actual:
            actions.append(("disconnect", shop_pk, shop_id, owner_user_id))
    return actions


def run_tiktok_window(now: Optional[datetime] = None) -> None:
    """TikTok 营业时间窗控制任务执行体（需求 24.x，TIK-015）。

    周期（建议 interval 60 秒，由管理端建 scheduled_task 记录启用）比对 TikTok
    店铺的营业时间期望态与实际连接态并收敛：

    1. 取全部启用且 ``platform='tiktok'`` 的店铺及其 BusinessHours（含 weekdays）；
    2. 逐店以北京时间计算期望态 ``should_connect = is_within_business_hours(...)``
       （复用 ``common.utils.business_hours``，与 websocket 引擎第二道闸同款逻辑）；
    3. 经 HTTP ``status-batch`` 一次查询全部店铺实际连接态；
    4. 期望连接而未连接 → ``connect``（带 platform='tiktok'）；期望断开而已连接 →
       ``disconnect``；状态一致不动（幂等收敛）；
    5. 任一店铺调用失败不中断整体遍历，汇总后写一条执行日志。

    PDD 店铺不受影响（按 platform 过滤，不触碰）。

    Args:
        now: 期望态判定的参考时刻；默认取当前北京时间（None）。仅供测试注入
            固定时刻，调度器调用无需传参。
    """
    try:
        shops = run_with_retry(_list_enabled_tiktok_shops)
    except Exception as exc:  # noqa: BLE001 —— 取店铺失败：记失败日志后返回
        logger.error("TikTok 时间窗任务取店铺列表失败：%s", exc)
        task_run_log.write_failed(TASK_TIKTOK_WINDOW, f"取 TikTok 店铺列表失败：{exc}")
        return

    if not shops:
        task_run_log.write_success(TASK_TIKTOK_WINDOW, "无启用 TikTok 店铺，跳过时间窗控制")
        return

    # 店铺出口代理映射（Phase 2 前置）：connect 时透传给 websocket 建浏览器会话。
    proxy_by_pk: Dict[int, Optional[str]] = {
        shop_pk: proxy for shop_pk, _sid, _oid, proxy in shops
    }

    # 逐店计算期望态：营业时间读取失败按「全天营业」处理（不误断连接）。
    expected_states: List[Tuple[int, str, Optional[int], bool]] = []
    for shop_pk, shop_id, owner_user_id, _proxy in shops:
        should_connect = True
        try:
            bh = run_with_retry(
                lambda s, pk=shop_pk: _load_business_hours(s, pk)
            )
            if bh is not None:
                should_connect = is_within_business_hours(
                    bh["start_time"],
                    bh["end_time"],
                    enabled=bh["enabled"],
                    weekdays=bh["weekdays"],
                    now=now,
                )
        except Exception as exc:  # noqa: BLE001 —— 单店营业时间失败不中断整体
            logger.warning(
                "店铺[%s] 营业时间读取失败（按全天营业处理）：%s", shop_id, exc
            )
        expected_states.append((shop_pk, shop_id, owner_user_id, should_connect))

    # 一次 status-batch 查询全部实际连接态；查询失败仅记日志，不做任何收敛。
    status_result = service_client.query_status_batch(
        [{"shop_id": shop_id, "owner_user_id": owner_user_id}
         for _shop_pk, shop_id, owner_user_id, _proxy in shops]
    )
    if not status_result.ok:
        logger.warning("TikTok 时间窗任务查询连接状态失败：%s", status_result.message)
        task_run_log.write_failed(
            TASK_TIKTOK_WINDOW, f"查询连接状态失败：{status_result.message}"
        )
        return

    raw_statuses = (status_result.data or {}).get("statuses", []) or []
    actual_statuses: Dict[str, bool] = {
        str(item.get("shop_id")): bool(item.get("connected", False))
        for item in raw_statuses
    }

    # 期望态与实态差异 → 收敛动作，逐店执行（失败不中断整体遍历）。
    actions = plan_window_actions(expected_states, actual_statuses)
    success_count = 0
    failed_count = 0
    for action, shop_pk, shop_id, owner_user_id in actions:
        if action == "connect":
            result = service_client.trigger_connect(
                shop_pk,
                shop_id,
                owner_user_id,
                platform="tiktok",
                proxy_server=proxy_by_pk.get(shop_pk),
            )
        else:
            result = service_client.trigger_disconnect(shop_pk, shop_id, owner_user_id)
        if result.ok:
            success_count += 1
            logger.info("TikTok 时间窗：店铺[%s] 已执行 %s", shop_id, action)
        else:
            failed_count += 1
            logger.warning(
                "TikTok 时间窗：店铺[%s] %s 失败：%s", shop_id, action, result.message
            )

    message = (
        f"TikTok 时间窗收敛完成：收敛动作 {len(actions)} 个"
        f"（成功 {success_count} 个，失败 {failed_count} 个）"
    )
    if failed_count > 0:
        task_run_log.write_failed(TASK_TIKTOK_WINDOW, message)
    else:
        task_run_log.write_success(TASK_TIKTOK_WINDOW, message)


# 任务键 -> 执行体 的映射，供 SchedulerService 按 scheduled_task.task_key 注册。
TASK_RUNNERS = {
    TASK_COOKIE_REFRESH: run_cookie_refresh,
    TASK_PRODUCT_SYNC: run_product_sync,
    TASK_LOG_FILE_CLEANUP: run_log_file_cleanup,
    TASK_TIKTOK_WINDOW: run_tiktok_window,
    TASK_REPLY_RATE_CHECK: reply_rate_check.run_reply_rate_check,
}


__all__ = [
    "run_cookie_refresh",
    "run_product_sync",
    "run_log_file_cleanup",
    "run_tiktok_window",
    "plan_window_actions",
    "TASK_RUNNERS",
]
