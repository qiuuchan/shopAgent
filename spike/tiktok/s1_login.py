# -*- coding: utf-8 -*-
"""
spike.tiktok.s1_login —— 探查 1：TikTok 卖家中心登录与登录态持久化
====================================================================
本文件用途（对齐 PLAN_TIKTOK.md §2.1 s1_login.py 通过标准）：
- 使用 Playwright 打开 seller-us.tiktok.com（headed 非无头，便于人工登录）；
- 首次运行需人工完成登录（扫码 / 账密 / 验证码），登录态持久化到独立
  user-data-dir（不碰日常浏览器）；
- 验证二次免登：关闭浏览器后重新以同目录打开，无需再次人工登录；
- 导出 Cookie JSON 供后续 spike 脚本与调试使用。

设计说明：
- 登录成功检测：由于 spike 阶段对 TikTok 卖家中心 DOM 尚不掌握，采用
  「URL 跳离登录页 + 页面主体文本」的宽松判据，并打印当前 URL 便于人工确认；
  s2_dom_map 测绘完成后可替换为精确选择器。
- 用户数据目录默认 ``spike/tiktok/_data/user_test1``（与正式服务 browser_data 分离）。
- 超时（默认 120s，可经 --wait-ms 调整）未登录则退出并返回非零码。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from playwright.async_api import Page

from _common import (
    TIKTOK_CHAT_PATH,
    TIKTOK_SELLER_URL,
    launch_context,
    setup_logging,
)

logger = logging.getLogger("spike.tiktok.s1_login")

# 默认登录等待超时（毫秒）：预留人工完成扫码 / 验证码时间（对齐需求 4.2/4.5 模式）。
DEFAULT_WAIT_MS: int = 180_000

# 本文件所在目录（spike/tiktok），默认数据目录基于它定位，避免因运行位置不同而错位。
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))

# 登录成功后卖家中心首页的候选路径片段（spike 实测后补充）。
_HOME_PATH_MARKERS: tuple[str, ...] = ("/", "/compass", "/order", "/chat", "/chat/inbox")

# 明确「未登录 / 登录流程中」的路径片段（命中即视为未登录）。
_NOT_LOGIN_PATH_MARKERS: tuple[str, ...] = (
    "login",
    "signin",
    "sign-in",
    "auth",
    "register",
    "account/register",
    "signup",
    "sign-up",
    "passport",
    "oidc",
)

# 卖家后台侧边栏菜单特征词（中英双语，泰国站后台语言实测后收敛；命中 2 个及以上视为已登录）。
_SELLER_BACKEND_MARKERS: tuple[str, ...] = (
    "orders",
    "products",
    "dashboard",
    "customer service",
    "finance",
    "fulfillment",
    "marketing",
    "订单",
    "商品",
    "客服",
    "财务",
    "店铺",
    "聊天",
    "數據",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TikTok 卖家中心登录探查 s1")
    parser.add_argument(
        "--user-data-dir",
        default=os.path.join(_SCRIPT_DIR, "_data", "user_test1"),
        help="持久化用户数据目录（默认 <脚本目录>/_data/user_test1）",
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=DEFAULT_WAIT_MS,
        help="登录等待超时毫秒（默认 180000）",
    )
    parser.add_argument(
        "--cookie-out",
        default=os.path.join(_SCRIPT_DIR, "_data", "cookies_test1.json"),
        help="Cookie JSON 导出路径",
    )
    return parser.parse_args()


async def _detect_login(page: Page) -> bool:
    """检测当前页面是否处于已登录的卖家后台。

    判定逻辑（spike 阶段临时，s2 测绘后替换），按优先级：
    1. URL 命中登录 / 注册路径（_NOT_LOGIN_PATH_MARKERS）→ 未登录；
    2. URL 含 ``/chat/``（聊天页可达）→ 已登录（未登录访问聊天页会被踢回登录页）；
    3. 页面主体文本命中 2 个及以上卖家后台特征词（中英双语）→ 已登录。

    Returns:
        已登录返回 True，否则 False。
    """
    try:
        low_url = (await page.evaluate("location.href")).lower()
        if any(m in low_url for m in _NOT_LOGIN_PATH_MARKERS):
            return False
        if "/chat/" in low_url:
            return True
        text = await page.evaluate(
            "document.body ? (document.body.innerText || '').toLowerCase() : ''"
        )
        hit = sum(1 for m in _SELLER_BACKEND_MARKERS if m in text)
        return hit >= 2
    except Exception:  # noqa: BLE001 - 求值失败视为未登录
        return False


async def _wait_login_done(page: Page, wait_ms: int) -> bool:
    """轮询等待登录完成（复用 _detect_login 判定），期间打印页面状态。

    Args:
        page: Playwright 页面对象。
        wait_ms: 总超时（毫秒）。

    Returns:
        登录完成返回 True，超时返回 False。
    """
    deadline = time.monotonic() + wait_ms / 1000.0
    while time.monotonic() < deadline:
        if await _detect_login(page):
            return True
        await page.wait_for_timeout(2000)
        await _print_page_state(page, "等待登录中")
    return False


async def _dump_cookies(context: Any, out_path: str) -> Dict[str, str]:
    """导出当前上下文 Cookie 为 name->value 字典 JSON 并落盘。

    Args:
        context: Playwright 浏览器上下文。
        out_path: 导出文件路径。

    Returns:
        Cookie 字典（name->value）。
    """
    cookies = await context.cookies()
    cookie_map: Dict[str, str] = {
        item.get("name", ""): item.get("value", "")
        for item in cookies
        if item.get("name")
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(cookie_map, fh, ensure_ascii=False, indent=2)
    logger.info("已导出 %s 个 Cookie 到 %s", len(cookie_map), out_path)
    return cookie_map


async def _print_page_state(page: Page, tag: str) -> None:
    """打印当前 URL / title，便于人工确认（spike 阶段无精确选择器）。"""
    try:
        url = await page.evaluate("location.href")
        title = await page.title()
        logger.info("[%s] URL=%s title=%s", tag, url, title)
    except Exception:  # noqa: BLE001 - 打印失败不影响主流程
        pass


async def _login_once(user_data_dir: str, wait_ms: int, cookie_out: str) -> Optional[str]:
    """执行一次登录（headed），成功返回 Cookie JSON 字符串，失败返回 None。

    Args:
        user_data_dir: 持久化用户数据目录。
        wait_ms: 登录等待超时（毫秒）。
        cookie_out: Cookie JSON 导出路径。

    Returns:
        成功返回 Cookie JSON（name->value 字典）；失败 / 超时返回 None。
    """
    playwright = None
    context = None
    try:
        playwright, context = await launch_context(user_data_dir, headless=False)
        page = await context.new_page()
        # 直接打开客服聊天页（泰国站后台路径，未登录会跳转登录页）。
        await page.goto(
            TIKTOK_SELLER_URL + TIKTOK_CHAT_PATH,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await _print_page_state(page, "打开客服聊天页")

        # 已登录则直接导出 Cookie（二次运行免登路径）。
        if await _detect_login(page):
            logger.info("检测到已登录（复用现有登录态）")
        else:
            logger.info(
                "未检测到登录态：请在浏览器中完成登录（扫码 / 账密 / 验证码），"
                "脚本将自动检测并继续，最长等待 %s 秒",
                wait_ms // 1000,
            )
            ok = await _wait_login_done(page, wait_ms)
            if not ok:
                logger.error("登录等待超时，未检测到登录态")
                return None
            logger.info("检测到登录完成")
            await _print_page_state(page, "登录后")

        cookie_map = await _dump_cookies(context, cookie_out)
        return json.dumps(cookie_map, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - 探查脚本异常统一降级为失败
        logger.error("登录探查失败: %s", exc)
        return None
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001
                pass


async def _verify_second_login(user_data_dir: str) -> bool:
    """验证二次免登：以同目录无头打开卖家中心，检测登录态是否保留。

    判定逻辑：打开首页后等待 SPA 跳转稳定（最多 12 秒），只要最终 URL
    不在登录 / 注册路径（含 homepage）即视为免登成功——泰国站实测登录后
    落到 ``/homepage``，且直接访问 ``/chat/inbox/current`` 缺 ``oec_seller_id``
    会跳回 homepage，因此 homepage 即为「已登录」标志。

    Args:
        user_data_dir: 持久化用户数据目录。

    Returns:
        免登成功返回 True，否则 False。
    """
    playwright = None
    context = None
    try:
        playwright, context = await launch_context(user_data_dir, headless=True)
        page = await context.new_page()
        await page.goto(TIKTOK_SELLER_URL, wait_until="domcontentloaded", timeout=60_000)
        # 等待 SPA 跳转稳定（首页 / 登录页 / homepage 三者之一），期间打印状态。
        last_url = ""
        for _ in range(12):
            await page.wait_for_timeout(1000)
            try:
                url = page.url
            except Exception:  # noqa: BLE001 - 导航瞬间读取失败则跳过本轮
                continue
            if url != last_url:
                logger.info("[二次免登] URL=%s", url)
                last_url = url
        await _print_page_state(page, "二次免登验证")
        low_url = last_url.lower()
        not_logged_in = any(m in low_url for m in _NOT_LOGIN_PATH_MARKERS)
        ok = not not_logged_in
        logger.info("二次免登验证: %s", "成功" if ok else "失败（仍需登录）")
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.error("二次免登验证失败: %s", exc)
        return False
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001
                pass


async def main() -> int:
    args = _parse_args()
    setup_logging()
    logger.info("开始 TikTok 登录探查（s1），user-data-dir=%s", args.user_data_dir)

    cookies_json = await _login_once(args.user_data_dir, args.wait_ms, args.cookie_out)
    if cookies_json is None:
        logger.error("s1 登录失败：未获得 Cookie")
        return 1

    ok = await _verify_second_login(args.user_data_dir)
    if not ok:
        logger.error("s1 二次免登验证失败")
        return 2

    logger.info("s1 通过：登录成功且二次免登有效，Cookie 已导出")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
