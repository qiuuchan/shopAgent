# -*- coding: utf-8 -*-
"""
spike.tiktok.s6_login_form_map —— 探查 6：登录页表单元素测绘（TIK-018 前置）
==============================================================================
本文件用途：实测登录页（/account/login）的表单结构，产出账号输入框 / 密码输入框 /
登录按钮的稳定选择器，回填 ``websocket/channel_tiktok/selectors.py`` 与
``tiktok_login.py`` 的真实登录实现（原实现为占位，2026-08-28 实测暴露）。

输出：打印全部 input / button / iframe 的关键属性（type/name/id/class/placeholder/
aria-label/text），供人工确认选择器；不执行任何登录动作。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from playwright.async_api import Page

from _common import TIKTOK_SELLER_URL, launch_context, setup_logging

logger = logging.getLogger("spike.tiktok.s6_login_form_map")

_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))


async def _describe_element(page: Page, handle: Any) -> Dict[str, str]:
    """读取单个 DOM 元素的可描述属性（容错：任一属性缺失不影响输出）。"""
    result: Dict[str, str] = {}
    for prop in ("tagName", "type", "name", "id", "placeholder", "aria-label", "class"):
        try:
            value = await handle.get_attribute(prop) if prop != "tagName" else await handle.evaluate("el => el.tagName")
            if value:
                result[prop] = value
        except Exception:  # noqa: BLE001 - 探查容错
            pass
    return result


async def _dump_elements(page: Page, selector: str, tag: str) -> List[Dict[str, str]]:
    """按选择器批量描述元素。"""
    handles = await page.query_selector_all(selector)
    items = []
    for handle in handles:
        item = await _describe_element(page, handle)
        if selector == "button":
            try:
                text = (await handle.inner_text()).strip()
                if text:
                    item["text"] = text[:40]
            except Exception:  # noqa: BLE001 - 探查容错
                pass
        items.append(item)
    return items


async def main() -> int:
    setup_logging()
    data_dir = os.path.join(_SCRIPT_DIR, "_data", "login_form_probe")
    playwright = None
    context = None
    try:
        playwright, context = await launch_context(data_dir, headless=False)
        page = await context.new_page()
        url = f"{TIKTOK_SELLER_URL}/account/login"
        logger.info("打开登录页: %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # 等待 SPA 表单渲染（登录表单通常为动态渲染）。
        await page.wait_for_timeout(8000)
        logger.info("当前 URL=%s title=%s", page.url, await page.title())

        inputs = await _dump_elements(page, "input", "input")
        buttons = await _dump_elements(page, "button", "button")
        frames = await page.query_selector_all("iframe")
        frame_info = []
        for frame in frames:
            src = await frame.get_attribute("src") if frame else None
            frame_info.append(src or "")

        # 附加探查：各 input 可见性 + 区号输入框当前值 + tab 切换入口（文本含 手机/邮箱 的元素）。
        visibility = {}
        for handle in await page.query_selector_all("input"):
            try:
                eid = await handle.get_attribute("id")
                if eid:
                    visibility[eid] = await handle.is_visible()
            except Exception:  # noqa: BLE001 - 探查容错
                pass
        area_value = ""
        try:
            area_value = await page.input_value("#TikTok_Ads_SSO_Login_Area_Select_Input")
        except Exception:  # noqa: BLE001 - 探查容错
            pass
        tab_hints = []
        for text in ("手机号", "邮箱", "账号登录", "验证码登录"):
            handles = await page.query_selector_all(f"text={text}")
            for handle in handles[:5]:
                try:
                    tag = await handle.evaluate("el => el.tagName")
                    cls = await handle.get_attribute("class") or ""
                    tab_hints.append(f"{tag} class={cls[:60]} text={text}")
                except Exception:  # noqa: BLE001 - 探查容错
                    pass

        report = {
            "url": page.url,
            "inputs": inputs,
            "buttons": buttons,
            "iframes": frame_info,
            "visibility": visibility,
            "area_select_value": area_value,
            "tab_hints": tab_hints,
        }
        out_path = os.path.join(_SCRIPT_DIR, "login_form_report.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        logger.info("表单测绘完成：%d 个 input / %d 个 button / %d 个 iframe", len(inputs), len(buttons), len(frame_info))
        logger.info("区号输入框当前值=%r 可见性=%s", area_value, visibility)
        logger.info("tab 入口候选: %s", tab_hints[:10])
        for item in inputs:
            logger.info("INPUT %s", item)
        for item in buttons:
            logger.info("BUTTON %s", item)
        return 0
    except Exception as exc:  # noqa: BLE001 - 探查异常降级
        logger.error("登录页测绘失败: %s", exc)
        return 1
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
