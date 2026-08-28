# -*- coding: utf-8 -*-
"""
spike.tiktok._common —— TikTok spike 探查公共辅助（独立脚本，不进服务代码）
============================================================================
本文件用途：为 spike/tiktok 下 s1~s5 探查脚本提供公共工具：日志初始化、
浏览器持久化上下文启动（含 Singleton 锁清理与重试）、URL 常量与等待辅助。

设计说明（对齐 PLAN_TIKTOK.md §2.1）：
- spike 脚本独立运行、不打包、不 import 服务代码（websocket/login 等），
  避免探查结论反向污染主代码；浏览器启动逻辑与 ``websocket/login/browser_launcher``
  思路一致（规范 52 复用），但此处为 spike 自包含实现，方便单独分发。
- 美区固定基址 ``seller-us.tiktok.com`` 允许写死并注释（与 PDD_WEBSOCKET_BASE_URL
  同等待遇，规范 21）。
- 日志统一 info/warning/error，禁用 debug（规范 38）。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from playwright.async_api import async_playwright

# TikTok Shop 商家中心基址（泰国站，生产目标；允许写死并注释说明见模块 docstring）。
# 注意：与 PLAN 原假设 seller-us.tiktok.com（美区）不同，2026-08-27 实测确认
# 泰国站后台域名为 seller.tiktokshopglobalselling.com（多语言国际站，可中文界面）。
TIKTOK_SELLER_URL: str = "https://seller.tiktokshopglobalselling.com"

# 客服聊天页路径（用户提供的后台实际路径，spike 实测后确认）。
TIKTOK_CHAT_PATH: str = "/chat/inbox/current"

# 本文件所在目录（spike/tiktok），默认数据目录基于它定位，避免运行位置不同而错位。
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))

# 默认的 spike 持久化用户数据目录（与正式服务的 browser_data 分离，避免互相污染）。
DEFAULT_SPIKE_DATA_DIR: str = os.path.join(_SCRIPT_DIR, "_data")

# 统一的 Chromium 启动参数（禁用自动化特征 / 通知等，提升登录成功率与稳定性）。
_CHROMIUM_ARGS: list[str] = [
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-notifications",
    "--disable-web-security",
    "--disable-features=VizDisplayCompositor",
]

# Chrome 持久化目录残留的 Singleton 锁文件名（上次未干净退出时残留）。
_SINGLETON_LOCK_FILES: tuple[str, ...] = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
)


def setup_logging() -> logging.Logger:
    """初始化根日志（INFO 级别、北京时间格式），返回 spike 根 logger。

    Returns:
        spike 根 logger（各脚本通过 ``logging.getLogger("spike.tiktok.xxx")`` 复用）。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("spike.tiktok")


def clean_singleton_lock_files(user_data_dir: str) -> None:
    """清理持久化目录中残留的 Chrome Singleton 锁文件（避免 PROFILE_IN_USE）。

    Args:
        user_data_dir: 用户数据目录（不存在则直接返回）。
    """
    try:
        if not user_data_dir or not os.path.isdir(user_data_dir):
            return
        for fname in _SINGLETON_LOCK_FILES:
            fpath = os.path.join(user_data_dir, fname)
            if not (os.path.exists(fpath) or os.path.islink(fpath)):
                continue
            try:
                if os.path.islink(fpath):
                    os.unlink(fpath)
                else:
                    os.remove(fpath)
                logging.getLogger("spike.tiktok._common").warning(
                    "已清理残留 Chrome 锁文件: %s", fpath
                )
            except Exception:  # noqa: BLE001 - 清理失败可忽略
                pass
    except Exception:  # noqa: BLE001 - 清理整体异常不应打断主流程
        pass


async def launch_context(
    user_data_dir: str,
    *,
    headless: bool = True,
    proxy_server: Optional[str] = None,
    retries: int = 2,
):
    """启动 Playwright 持久化浏览器上下文（失败时清锁重试）。

    Args:
        user_data_dir: 持久化用户数据目录（不存在则自动创建）。
        headless: 是否无头模式。
        proxy_server: 可选代理地址（如 ``http://127.0.0.1:7890``）。
        retries: 启动重试次数（默认 2：首次失败清锁重试一次）。

    Returns:
        (playwright, context) 元组；调用方负责在 finally 中关闭。
    """
    os.makedirs(user_data_dir, exist_ok=True)
    clean_singleton_lock_files(user_data_dir)

    playwright = await async_playwright().start()
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                args=list(_CHROMIUM_ARGS),
                proxy={"server": proxy_server} if proxy_server else None,
            )
            return playwright, context
        except Exception as exc:  # noqa: BLE001 - 失败清锁重试
            last_error = exc
            logging.getLogger("spike.tiktok._common").warning(
                "第 %s/%s 次启动浏览器失败: %s", attempt, retries, exc
            )
            if attempt < retries:
                clean_singleton_lock_files(user_data_dir)
                time.sleep(1)
    if last_error:
        raise last_error
    raise RuntimeError("浏览器上下文创建失败")


async def wait_for_condition(
    page: Any,
    expression: str,
    timeout_ms: int,
    *,
    interval_ms: int = 500,
    description: str = "",
) -> bool:
    """轮询等待页面 JS 条件成立（比 wait_for_function 更宽容，可打日志）。

    Args:
        page: Playwright 页面对象。
        expression: JS 布尔表达式（在页面上下文中求值）。
        timeout_ms: 总超时（毫秒）。
        interval_ms: 轮询间隔（毫秒）。
        description: 等待描述（仅用于日志）。

    Returns:
        条件成立返回 True，超时返回 False。
    """
    logger = logging.getLogger("spike.tiktok._common")
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if await page.evaluate(f"!!({expression})"):
                return True
        except Exception:  # noqa: BLE001 - 求值失败视为未成立
            pass
        await page.wait_for_timeout(interval_ms)
    logger.warning("等待超时%s: %s", f"[{description}]" if description else "", expression)
    return False
