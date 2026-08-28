# -*- coding: utf-8 -*-
"""
channel_tiktok.login_recovery —— TikTok 登录失效恢复探测（纯逻辑，可注入）
=========================================================================
本文件用途：实现「TikTok 登录态失效后，周期探测登录态是否恢复（人工重登）并自动
续接监控」的恢复闭环（Phase 2 登录态保活策略，PLAN §7）。TikTok 登录态常驻浏览器
用户数据目录，人工在浏览器窗口重登后 ``page.url`` 离开登录页即可恢复；本模块在
``_monitor_loop`` 检测到 ``login_expired`` 后替代「直接退出」，以可配置周期反复探测，
恢复则重开聊天页续接监控，超时仍未恢复才退出（彻底停止待人工重连）。

设计要点：
- 纯逻辑、无副作用：所有外部依赖（登录态判定 / 重开聊天页 / 睡眠 / 停止信号）均以
  回调注入，便于单测以桩验证探测序与恢复/超时分支，不真实操作浏览器（硬约束）。
- 探测语义：每次先等一个 ``interval``（期间可响应停止信号），再查登录态；一旦不再
  失效即重开聊天页并返回 True（恢复）；达 ``max_tries`` 次仍失效返回 False（放弃）。
- 对齐监控循环口径：登录态判定复用通道的 ``_is_login_expired``（``page.url`` 命中
  ``LOGIN_PAGE_MARKERS``）；睡眠复用 ``_sleep_or_stop``（停止信号优先，探测不阻塞
  ``stop()``）。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、导入置顶（51）、
日志禁用 debug（38）、无副作用可注入（便于纯逻辑测试）。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger("channel_tiktok.login_recovery")

# 类型别名：登录态判定回调（无参返回 bool，True=已失效）。
IsExpired = Callable[[], bool]
# 重开聊天页回调（无参，协程；恢复登录后刷新页面续接监控）。
ReopenChat = Callable[[], Awaitable[None]]
# 睡眠回调（秒，协程；期间响应停止信号）。
SleepFn = Callable[[float], Awaitable[None]]
# 停止信号判定回调（无参返回 bool，True=收到停止信号）。
IsStopped = Callable[[], bool]


async def wait_login_recovery(
    *,
    is_expired: IsExpired,
    reopen_chat: ReopenChat,
    sleep: SleepFn,
    is_stopped: IsStopped,
    interval: float,
    max_tries: int,
) -> bool:
    """登录失效后周期探测登录态恢复（纯逻辑，可注入桩测试）。

    语义（Phase 2 保活）：检测到登录失效后，每 ``interval`` 秒探测一次登录态；
    若登录态恢复（人工重登完成，``is_expired`` 返回 False）则调用 ``reopen_chat``
    重开聊天页并返回 True（恢复成功，调用方续接监控）；达到 ``max_tries`` 次仍未
    恢复返回 False（调用方退出监控循环，彻底停止等待人工重连）。探测期间响应停止
    信号（``is_stopped``），保证 ``stop()`` 不被探测循环阻塞。

    Args:
        is_expired: 登录态判定回调（True=仍失效）。
        reopen_chat: 恢复后重开聊天页的协程回调。
        sleep: 探测间隔睡眠协程回调（可注入 ``_sleep_or_stop`` 响应停止）。
        is_stopped: 停止信号判定回调（True=已停止，立即返回 False）。
        interval: 探测间隔（秒）。
        max_tries: 最大探测次数（超过仍未恢复即放弃）。

    Returns:
        登录态恢复返回 True；超时未恢复 / 收到停止信号返回 False。
    """
    for _ in range(max(0, max_tries)):
        await sleep(interval)
        if is_stopped():
            logger.info("探测期间收到停止信号，放弃登录恢复等待")
            return False
        if not is_expired():
            try:
                await reopen_chat()
            except Exception as exc:  # noqa: BLE001 - 重开聊天页失败继续探测
                logger.warning("登录恢复后重开聊天页失败（继续探测）: %s", exc)
                continue
            logger.info("TikTok 登录态已恢复，续接监控")
            return True
    logger.warning("TikTok 登录态持续失效，停止监控待人工重连")
    return False


__all__ = ["wait_login_recovery"]
