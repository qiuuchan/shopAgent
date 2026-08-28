# -*- coding: utf-8 -*-
"""
spike.tiktok.s7_login_debug —— 探查 7：真实登录流程诊断（TIK-018 前置）
=========================================================================
本文件用途：真实执行「填手机号 + 密码 + 点登录」，并每 2 秒 dump 页面状态
（URL / 可见文本 / 错误提示 / 验证码输入框），定位 2026-08-28 登录超时根因
（候选：区号默认值不对、验证码弹窗、错误提示选择器未命中）。

不修改主代码；账号密码经命令行参数传入。登录成功时导出 Cookie 到独立文件。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from playwright.async_api import Page

from _common import TIKTOK_SELLER_URL, launch_context, setup_logging

logger = logging.getLogger("spike.tiktok.s7_login_debug")

_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))

# 与正式登录一致的登录表单选择器（s6 实测）。
_LOGIN_MOBILE = "#TikTok_Ads_SSO_Login_Mobile_Input"
_LOGIN_PWD = "#TikTok_Ads_SSO_Login_Pwd_Input"
_LOGIN_BTN = "#TikTok_Ads_SSO_Login_Btn"
_LOGIN_CODE = "#TikTok_Ads_SSO_Login_Code_Input"
_AREA_SELECT = "#TikTok_Ads_SSO_Login_Area_Select_Input"


async def _dump_state(page: Page, tag: str, round_no: int = 0) -> None:
    """打印当前页面 URL / 标题 / 可见文本片段 / 关键元素可见性。"""
    try:
        url = await page.evaluate("location.href")
        title = await page.title()
        text = await page.evaluate(
            "document.body ? (document.body.innerText || '').slice(0, 400) : ''"
        )
        code_visible = await page.is_visible(_LOGIN_CODE)
        logger.info(
            "[%s] URL=%s title=%s code_input_visible=%s\n  body=%r",
            tag, url, title, code_visible, text,
        )
    except Exception as exc:  # noqa: BLE001 - 诊断容错
        logger.error("[%s] dump 失败: %s", tag, exc)


async def main() -> int:
    parser = argparse.ArgumentParser(description="TikTok 登录流程诊断 s7")
    parser.add_argument("username", help="登录账号（手机号）")
    parser.add_argument("password", help="登录密码")
    parser.add_argument(
        "--user-data-dir",
        default=os.path.join(_SCRIPT_DIR, "_data", "login_debug"),
        help="持久化用户数据目录（独立，不污染正式登录）",
    )
    args = parser.parse_args()
    setup_logging()

    playwright = None
    context = None
    try:
        playwright, context = await launch_context(args.user_data_dir, headless=False)
        page = await context.new_page()
        url = f"{TIKTOK_SELLER_URL}/account/login"
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector(_LOGIN_MOBILE, timeout=30_000)
        await _dump_state(page, "登录页就绪")

        # 区号默认值（s6 显示 Area_Select 初始不可见，尝试点击后读取）。
        try:
            area_visible = await page.is_visible(_AREA_SELECT)
            logger.info("区号输入框可见=%s", area_visible)
            if area_visible:
                area_val = await page.input_value(_AREA_SELECT)
                logger.info("区号当前值=%r", area_val)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取区号失败: %s", exc)

        await page.fill(_LOGIN_MOBILE, args.username)
        await page.fill(_LOGIN_PWD, args.password)
        await page.click(_LOGIN_BTN)
        logger.info("已提交登录表单")

        # 轮询 dump 8 次（每 2 秒），观察提交后页面变化。
        deadline = 16
        for round_no in range(1, deadline + 1):
            await page.wait_for_timeout(2000)
            await _dump_state(page, f"提交后 {round_no * 2}s", round_no)
            try:
                low_url = (await page.evaluate("location.href")).lower()
                if not any(
                    m in low_url
                    for m in ("login", "signin", "auth", "passport", "oidc")
                ):
                    logger.info("检测到已跳离登录页（登录成功或跳转）")
                    cookies = await context.cookies()
                    out = os.path.join(_SCRIPT_DIR, "_data", "login_debug_cookies.json")
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, "w", encoding="utf-8") as fh:
                        json.dump(
                            {c.get("name"): c.get("value") for c in cookies if c.get("name")},
                            fh, ensure_ascii=False, indent=2,
                        )
                    logger.info("Cookie 已导出到 %s", out)
                    return 0
            except Exception:  # noqa: BLE001 - 求值失败继续等待
                pass
        logger.warning("诊断结束：16 秒内未跳离登录页")
        return 1
    except Exception as exc:  # noqa: BLE001 - 诊断异常降级
        logger.error("诊断失败: %s", exc)
        return 2
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


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
