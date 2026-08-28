# -*- coding: utf-8 -*-
"""
login.browser_launcher —— 公共浏览器启动逻辑（被各平台登录复用）
================================================================
本文件用途：从 ``playwright_login.py`` 提取与「拼多多 / TikTok 等平台无关」的
Chromium 启动公共逻辑，供 ``login`` 包内多平台登录模块复用，避免重复实现
（开发规范 52：复用既有组件，不做重复实现）。

提取范围（对齐 TIK-009）：
- ``_CHROMIUM_ARGS``：统一的 Chromium 启动参数（禁用自动化特征 / 通知等）。
- ``_clean_singleton_lock_files``：清理残留 Singleton 锁，避免 PROFILE_IN_USE。
- ``_launch_persistent_context_with_retry``：启动持久化上下文，失败时清锁重试一次。

行为约束：本文件只负责「启动浏览器上下文」这一纯交互，不涉及任何平台登录
流程、数据库或店铺信息抓取；不改动原逻辑语义，仅做位置迁移。

实现约束（开发规范）：
- 地址 / 并发参数经环境变量配置，禁止写死（规范 21）。
- 导入置顶、中文注释、日志禁用 debug（规范 38）、文件名用下划线、单文件 ≤500 行。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, List, Optional

logger = logging.getLogger("login.browser_launcher")

# 启动 Chromium 的统一参数（禁用自动化特征 / 通知等，提升登录成功率）。
_CHROMIUM_ARGS: List[str] = [
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-notifications",
    "--disable-web-security",
    "--disable-features=VizDisplayCompositor",
]

# Chrome 持久化目录中可能残留的 Singleton 锁文件名（上次未干净退出时残留）。
_SINGLETON_LOCK_FILES: tuple[str, ...] = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
)


def get_chromium_args() -> List[str]:
    """返回统一的 Chromium 启动参数副本。

    Returns:
        当前生效的 Chromium 启动参数列表（调用方不应修改原列表）。
    """
    return list(_CHROMIUM_ARGS)


def clean_singleton_lock_files(user_data_dir: str, name: str) -> None:
    """清理持久化目录中残留的 Chrome Singleton 锁文件（避免 PROFILE_IN_USE）。

    Chrome 启动时会在用户数据目录创建 ``SingletonLock/SingletonCookie/SingletonSocket``
    三个锁（Windows 为普通文件，Linux 为符号链接）。若上次浏览器进程未干净退出，
    这些文件会残留，导致下次 ``launch_persistent_context`` 直接以 exit code 21
    （PROFILE_IN_USE）失败。本系统同一账号目录受登录信号量串行保护，发现的残留锁
    文件均为孤儿，可安全删除（参照 xianyu-auto-reply-wangpan 的 slider_stealth）。

    Args:
        user_data_dir: 账号专属的用户数据目录。
        name: 登录账号名（仅用于日志）。
    """
    try:
        if not user_data_dir or not os.path.isdir(user_data_dir):
            return
        for fname in _SINGLETON_LOCK_FILES:
            fpath = os.path.join(user_data_dir, fname)
            # os.path.exists 对已断开的符号链接返回 False，故同时判断 islink。
            if not (os.path.exists(fpath) or os.path.islink(fpath)):
                continue
            try:
                if os.path.islink(fpath):
                    os.unlink(fpath)
                else:
                    os.remove(fpath)
                logger.warning("账号 '%s' 已清理残留 Chrome 锁文件: %s", name, fpath)
            except Exception as inner_exc:  # noqa: BLE001 - 清理失败可忽略，不影响主流程
                logger.warning(
                    "账号 '%s' 清理锁文件 %s 失败（可忽略）: %s", name, fname, inner_exc
                )
    except Exception as exc:  # noqa: BLE001 - 清理整体异常不应打断登录流程
        logger.warning("账号 '%s' 清理 Singleton 锁文件时出错（可忽略）: %s", name, exc)


async def launch_persistent_context_with_retry(
    playwright: Any,
    user_data_dir: str,
    name: str,
    *,
    headless: bool,
    chromium_args: Optional[List[str]] = None,
):
    """启动持久化上下文，失败时清理锁文件后重试一次（对齐参照项目健壮性）。

    首次启动若因 ``PROFILE_IN_USE`` 等原因失败，清理残留 Singleton 锁文件并短暂
    等待 Chrome 子进程彻底退出后再重试一次；仍失败则抛出最后一次异常。

    Args:
        playwright: 已启动的 Playwright 实例。
        user_data_dir: 账号专属的用户数据目录。
        name: 登录账号名（仅用于日志）。
        headless: 是否无头模式。
        chromium_args: 自定义启动参数；缺省使用 ``get_chromium_args()`` 统一参数。

    Returns:
        启动成功的浏览器持久化上下文。
    """
    args = chromium_args if chromium_args is not None else get_chromium_args()

    # 启动前先清理一次残留锁文件（同一账号目录已被信号量串行保护，清理安全）。
    clean_singleton_lock_files(user_data_dir, name)

    last_error: Optional[Exception] = None
    for attempt in range(1, 3):
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                args=args,
            )
            if attempt > 1:
                logger.info("账号 '%s' 第 %s 次尝试启动浏览器成功", name, attempt)
            return context
        except Exception as exc:  # noqa: BLE001 - 首次失败清锁重试，末次失败向上抛
            last_error = exc
            logger.warning(
                "账号 '%s' 第 %s/2 次启动浏览器失败: %s", name, attempt, exc
            )
            if attempt < 2:
                clean_singleton_lock_files(user_data_dir, name)
                # 短暂等待 Chrome 子进程彻底退出，避免文件占用导致清理 / 启动再次失败。
                time.sleep(1)

    raise last_error if last_error else RuntimeError("浏览器上下文创建失败")


__all__ = [
    "get_chromium_args",
    "clean_singleton_lock_files",
    "launch_persistent_context_with_retry",
]
