# -*- coding: utf-8 -*-
"""
login.playwright_login —— 基于 Playwright 的拼多多账号密码登录与 Cookie 刷新
============================================================================
本文件用途：复用改造参照项目 Customer-Agent-1.2.0 ``Channel/pinduoduo/pdd_login.py``
中 ``PDDLogin.login()`` / ``refresh_cookies()`` 的 Playwright 浏览器自动化部分，
仅负责「启动 Chromium、完成账号密码登录 / 刷新、导出 Cookie」这一纯浏览器交互，
不涉及数据库与店铺 / 用户信息抓取（后者由 channel_pdd.pdd_login 编排）。

满足需求：
- 4.1：通过 Playwright 启动浏览器调用拼多多登录流程获取 Cookie。
- 4.2：登录需人工通过验证码 / 滑块时，以非无头浏览器呈现验证界面并等待用户完成。
- 4.5：未能在超时时间内完成（含用户未完成人工验证）时返回 None（失败）。
- 4.6：使用已保存的用户数据目录无头刷新 Cookie。

并发与无头控制（需求 4，运行于 websocket 服务）：
- ``BROWSER_HEADLESS``：是否无头模式（账号密码登录默认非无头以便人工验证）。
- ``MAX_CAPTCHA_CONCURRENT``：人工验证 / 登录浏览器会话的最大并发数，经异步信号量
  限流，避免同时弹出过多浏览器窗口。

实现约束（开发规范）：
- 地址 / 并发参数经环境变量配置，禁止写死（规范 21）：headless / 并发数经 common 配置
  （环境变量 BROWSER_HEADLESS / MAX_CAPTCHA_CONCURRENT）；用户数据目录与登录等待超时
  经环境变量 PLAYWRIGHT_USER_DATA_DIR / PDD_LOGIN_WAIT_TIMEOUT_MS 配置。
- 导入置顶、中文注释、日志禁用 debug（规范 38）、文件名用下划线、单文件 ≤500 行。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright

from common.core.config import get_settings
from login.browser_launcher import (
    clean_singleton_lock_files,
    launch_persistent_context_with_retry,
)

logger = logging.getLogger("login.playwright_login")

# 拼多多登录页与首页地址。
PDD_LOGIN_URL: str = "https://mms.pinduoduo.com/login"
PDD_HOME_URL: str = "https://mms.pinduoduo.com/home/"

# 登录成功后页面 title 的候选集合（参照 Customer-Agent 实测）。
_SUCCESS_TITLES: tuple[str, ...] = ("拼多多 商家后台", "首页", "订单查询")

# 登录等待超时默认值（毫秒）：预留人工完成验证码 / 滑块的时间（需求 4.2 / 4.5）。
_DEFAULT_LOGIN_WAIT_TIMEOUT_MS: int = 120_000

# 刷新时判定登录态失效的等待超时（毫秒）：跳转 login 页即视为失效（需求 4.6 / 4.7）。
_REFRESH_REDIRECT_TIMEOUT_MS: int = 5_000

# 人工验证 / 登录浏览器会话并发信号量（懒加载，避免在导入期绑定事件循环）。
_login_semaphore: Optional[asyncio.Semaphore] = None


def _get_login_semaphore() -> asyncio.Semaphore:
    """获取登录 / 人工验证并发信号量（按 MAX_CAPTCHA_CONCURRENT 限流）。

    懒加载构造，绑定到首次调用时的事件循环；并发上限取自 common 配置
    （环境变量 MAX_CAPTCHA_CONCURRENT，缺省回退默认值，且至少为 1）。

    Returns:
        受配置约束的异步信号量实例。
    """
    global _login_semaphore
    if _login_semaphore is None:
        max_concurrent = max(1, int(get_settings().max_captcha_concurrent))
        _login_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info("初始化登录并发信号量，最大并发数=%s", max_concurrent)
    return _login_semaphore


def _resolve_user_data_dir(name: str) -> str:
    """计算指定账号的 Playwright 持久化用户数据目录（按账号隔离，避免多实例冲突）。

    基础目录经环境变量 ``PLAYWRIGHT_USER_DATA_DIR`` 配置，缺省回退到当前工作目录下的
    ``browser_data``（规范 21：经环境变量管理，避免写死绝对路径）。该缺省值与
    Docker 持久化卷挂载点（``/app/websocket/browser_data``）保持一致，确保容器重建
    后登录态不丢失；账号子目录命名为 ``user_{name}``，与禁用账号浏览器数据清理任务
    的目录约定一致（参照项目 xianyu-auto-reply-wangpan 的 browser_data/user_{id}）。

    Args:
        name: 登录账号名（作为子目录名隔离不同账号的浏览器数据）。

    Returns:
        该账号专属的用户数据目录绝对路径。
    """
    base_dir = os.environ.get("PLAYWRIGHT_USER_DATA_DIR") or os.path.join(
        os.getcwd(), "browser_data"
    )
    user_data_dir = os.path.join(base_dir, f"user_{name}")
    os.makedirs(user_data_dir, exist_ok=True)
    return user_data_dir


def _cookies_list_to_json(cookies_list: List[Dict]) -> str:
    """将 Playwright 的 Cookie 列表转换为「name->value」字典的 JSON 字符串。

    Args:
        cookies_list: ``context.cookies()`` 返回的 Cookie 字典列表。

    Returns:
        ``{name: value}`` 映射的 JSON 字符串（ensure_ascii=False，保留中文）。
    """
    cookies_dict = {
        item.get("name", ""): item.get("value", "")
        for item in cookies_list
        if item.get("name")
    }
    return json.dumps(cookies_dict, ensure_ascii=False)


def _login_wait_timeout_ms() -> int:
    """读取登录等待超时（毫秒），经环境变量配置，缺省回退默认值（需求 4.5）。"""
    raw = os.environ.get("PDD_LOGIN_WAIT_TIMEOUT_MS")
    if raw and raw.strip().isdigit():
        return int(raw.strip())
    return _DEFAULT_LOGIN_WAIT_TIMEOUT_MS


async def login_with_password(name: str, password: str) -> Optional[str]:
    """使用账号密码通过 Playwright 登录拼多多商家后台并导出 Cookie（需求 4.1 / 4.2 / 4.5）。

    以非无头模式（由 BROWSER_HEADLESS 配置，默认 False）启动持久化上下文，自动填写
    账号密码并提交；若触发验证码 / 滑块，浏览器窗口可见，等待用户人工完成，直至页面
    title 命中成功集合或超时。受 MAX_CAPTCHA_CONCURRENT 信号量限流。

    Args:
        name: 登录账号名。
        password: 账号密码。

    Returns:
        登录成功返回 Cookie 的 JSON 字符串；失败 / 超时返回 None。
    """
    settings = get_settings()
    headless = bool(settings.browser_headless)
    user_data_dir = _resolve_user_data_dir(name)
    wait_timeout = _login_wait_timeout_ms()

    async with _get_login_semaphore():
        playwright = None
        context = None
        try:
            playwright = await async_playwright().start()
            context = await launch_persistent_context_with_retry(
                playwright, user_data_dir, name, headless=headless
            )
            page = await context.new_page()

            # 打开登录页并切换到「账号登录」。
            await page.goto(PDD_LOGIN_URL)
            await page.click("div.Common_item__3diIn:has-text('账号登录')")

            # 等待输入框出现后填入账号密码并提交。
            await page.wait_for_selector("input[type='text']")
            await page.fill("input[type='text']", name)
            await page.fill("input[type='password']", password)
            await page.click("button:has-text('登录')")

            # 等待登录成功（title 命中成功集合）。期间用户可人工完成验证码 / 滑块
            # （需求 4.2）；超时（含未完成人工验证）则判定失败（需求 4.5）。
            title_condition = (
                "() => "
                + " || ".join(
                    f"document.title === '{title}'" for title in _SUCCESS_TITLES
                )
            )
            await page.wait_for_function(title_condition, timeout=wait_timeout)

            cookies_json = _cookies_list_to_json(await context.cookies())
            logger.info("账号 '%s' 账号密码登录成功，已导出 Cookie", name)
            return cookies_json
        except Exception as exc:  # noqa: BLE001 - 登录异常统一降级为失败返回
            logger.error("账号 '%s' 账号密码登录失败: %s", name, exc)
            return None
        finally:
            await _safe_close(context, playwright)


async def refresh_with_user_data(name: str) -> Optional[str]:
    """复用已保存的用户数据目录无头刷新 Cookie（需求 4.6 / 4.7）。

    使用与登录一致的持久化用户数据目录、以无头模式访问商家后台首页：
    - 若页面跳转到登录页（说明登录态已失效），返回 None（供上层标记「需重新登录」）；
    - 否则导出最新 Cookie 并返回。

    Args:
        name: 登录账号名（定位其用户数据目录）。

    Returns:
        刷新成功返回最新 Cookie 的 JSON 字符串；登录态失效 / 异常返回 None。
    """
    user_data_dir = _resolve_user_data_dir(name)
    # 用户数据目录不存在说明从未登录过，无法刷新。
    if not os.path.exists(user_data_dir) or not os.listdir(user_data_dir):
        logger.error("账号 '%s' 用户数据目录为空，请先完成账号密码登录", name)
        return None

    async with _get_login_semaphore():
        playwright = None
        context = None
        try:
            playwright = await async_playwright().start()
            # 刷新统一使用无头模式（无需人工交互）。
            context = await launch_persistent_context_with_retry(
                playwright, user_data_dir, name, headless=True
            )
            page = await context.new_page()
            await page.goto(PDD_HOME_URL)

            # 若跳转到登录页，说明登录态已失效（需求 4.7）。
            try:
                await page.wait_for_url(
                    "**/login**", timeout=_REFRESH_REDIRECT_TIMEOUT_MS
                )
                logger.warning("账号 '%s' 登录态已失效，需要重新登录", name)
                return None
            except Exception:  # noqa: BLE001 - 未跳转即超时，说明登录态有效
                pass

            cookies_json = _cookies_list_to_json(await context.cookies())
            logger.info("账号 '%s' Cookie 刷新成功", name)
            return cookies_json
        except Exception as exc:  # noqa: BLE001
            logger.error("账号 '%s' Cookie 刷新失败: %s", name, exc)
            return None
        finally:
            await _safe_close(context, playwright)


async def _safe_close(context, playwright) -> None:
    """安全关闭浏览器上下文与 Playwright 实例，吞掉关闭阶段的异常。

    Args:
        context: 浏览器持久化上下文（可能为 None）。
        playwright: Playwright 实例（可能为 None）。
    """
    if context is not None:
        try:
            await context.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
            pass
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "PDD_LOGIN_URL",
    "PDD_HOME_URL",
    "login_with_password",
    "refresh_with_user_data",
]
