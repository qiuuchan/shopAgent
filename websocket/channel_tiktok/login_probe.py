# -*- coding: utf-8 -*-
"""
channel_tiktok.login_probe —— TikTok 登录态周期巡检（只读探测，纯逻辑可注入）
============================================================================
本文件用途：为 TIK-023「登录态过期周期观测」提供**只读**的登录态探测能力，供
scheduler 的 cookie_refresh 周期任务在 TikTok 侧作为「巡检触发点」调用（TIK-016
约定：TikTok 登录态常驻浏览器目录，该任务跳过 PDD 刷新、仅作巡检打点）。

为何需要本模块（TIK-023 要解决的观测缺口）：
- TikTok 登录态既不在库里（无 cookies_enc），也没有任何「登录生效时刻」的持久化
  字段，仅凭告警记录只有「已过期」的时间点，推不出「登录 → 过期」的时长。
- cookie_refresh 每周期对每店打一次巡检点，串起来就是「何时仍正常 / 何时已失效」
  的完整时间线，据此才能得出真实的过期周期结论。

设计要点：
- **只读、无副作用**：不改动通道状态、不触发告警、不重开页面。探测异常一律记为
  ``unknown``（不误报），判定权交给周期任务的日志与人工复核。
- **纯逻辑可注入**：页面存在性 / 存活探测 / 主站过期 / IM 过期四类外部依赖全部以
  回调注入，单测用桩即可覆盖全部分支，不真实操作浏览器（硬约束）。
- **取值即服务间契约**：返回值经 websocket ``/cookies/refresh`` 的 ``data.login_probe``
  透传给 scheduler；scheduler 侧不导入本模块，仅按字符串取值比对（跨服务解耦）。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、导入置顶（51）、
日志禁用 debug（38）、无副作用可注入（便于纯逻辑测试）。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger("channel_tiktok.login_probe")

# ----------------------------------------------------------------------
# 登录态巡检结果取值（服务间契约，改动需同步 scheduler 侧注释）
# ----------------------------------------------------------------------
# 登录态有效。
LOGIN_PROBE_OK: str = "ok"
# 主站跳登录页（page.url 命中 LOGIN_PAGE_MARKERS）。
LOGIN_PROBE_EXPIRED: str = "login_expired"
# 主站未跳转，但 IM 子系统会话过期弹窗（.p-modal，TIK-018 实测）。
LOGIN_PROBE_IM_EXPIRED: str = "im_expired"
# 页面存活探测失败：浏览器进程已退出或页面崩溃（与 connection_disconnected 同源）。
LOGIN_PROBE_PAGE_DEAD: str = "page_dead"
# 该店铺无活跃页面（未连接 / 营业时间窗外 / 通道未启动），非故障。
LOGIN_PROBE_NO_CHANNEL: str = "channel_absent"
# 探测过程异常：不误报，留待人工复核。
LOGIN_PROBE_UNKNOWN: str = "unknown"

# 巡检结果 → 中文说明（scheduler 侧落执行日志、观测 CLI 出报告时使用）。
LOGIN_PROBE_LABELS: dict[str, str] = {
    LOGIN_PROBE_OK: "登录态有效",
    LOGIN_PROBE_EXPIRED: "主站登录态失效（跳登录页）",
    LOGIN_PROBE_IM_EXPIRED: "IM 会话过期弹窗",
    LOGIN_PROBE_PAGE_DEAD: "页面存活探测失败（浏览器已退出）",
    LOGIN_PROBE_NO_CHANNEL: "无活跃页面（未连接或窗口外）",
    LOGIN_PROBE_UNKNOWN: "探测异常（不误报，待复核）",
}

# 类型别名：页面存在性判定回调（无参返回 bool，True=存在可探测页面）。
HasPage = Callable[[], bool]
# 页面存活探测回调（无参协程；False=浏览器已死）。
PageAlive = Callable[[], Awaitable[bool]]
# 主站登录态判定回调（无参返回 bool，True=已失效）。
IsLoginExpired = Callable[[], bool]
# IM 会话过期判定回调（无参协程；True=已失效）。
IsImExpired = Callable[[], Awaitable[bool]]


async def probe_login_state(
    *,
    has_page: HasPage,
    page_alive: PageAlive,
    is_login_expired: IsLoginExpired,
    is_im_expired: IsImExpired,
) -> str:
    """只读探测当前登录态，返回巡检结果取值（纯逻辑，可注入桩测试）。

    判定顺序（先轻后重、先死活后过期）：无页面 → 页面存活 → 主站过期 → IM 过期。
    与 ``TikTokChannel._monitor_loop`` 的每轮检测口径一致，但**只判定不处置**：
    不置状态、不发告警、不重开页面，避免巡检反过来干扰主链路。

    Args:
        has_page: 页面存在性回调（通道未启动 / 窗口外时无页面）。
        page_alive: 页面存活探测回调（浏览器退出或页面崩溃时 False）。
        is_login_expired: 主站登录态判定回调（True=已失效）。
        is_im_expired: IM 会话过期判定回调（True=已失效）。

    Returns:
        巡检结果取值字符串（见模块级常量）；探测异常返回 ``unknown``。
    """
    if not has_page():
        return LOGIN_PROBE_NO_CHANNEL
    try:
        if not await page_alive():
            return LOGIN_PROBE_PAGE_DEAD
        if is_login_expired():
            return LOGIN_PROBE_EXPIRED
        if await is_im_expired():
            return LOGIN_PROBE_IM_EXPIRED
    except Exception as exc:  # noqa: BLE001 - 巡检只观测，异常不误报为过期
        logger.warning("TikTok 登录态巡检异常（记为 unknown，不误报）: %s", exc)
        return LOGIN_PROBE_UNKNOWN
    return LOGIN_PROBE_OK


def describe(probe: str) -> str:
    """把巡检结果取值转中文说明（未知取值原样返回，便于排障）。

    Args:
        probe: 巡检结果取值字符串。

    Returns:
        对应中文说明；未登记取值原样返回。
    """
    return LOGIN_PROBE_LABELS.get(probe, probe)


__all__ = [
    "LOGIN_PROBE_OK",
    "LOGIN_PROBE_EXPIRED",
    "LOGIN_PROBE_IM_EXPIRED",
    "LOGIN_PROBE_PAGE_DEAD",
    "LOGIN_PROBE_NO_CHANNEL",
    "LOGIN_PROBE_UNKNOWN",
    "LOGIN_PROBE_LABELS",
    "HasPage",
    "PageAlive",
    "IsLoginExpired",
    "IsImExpired",
    "probe_login_state",
    "describe",
]
