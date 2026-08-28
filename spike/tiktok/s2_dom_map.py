# -*- coding: utf-8 -*-
"""
spike.tiktok.s2_dom_map —— 探查 2：聊天页 DOM 测绘与选择器稳定性验证
====================================================================
本文件用途（对齐 PLAN_TIKTOK.md §2.1 s2_dom_map.py 通过标准）：
- 打开 TikTok 卖家中心聊天页，测绘会话列表 / 未读标记 / 消息流 / 输入框 /
  发送按钮的稳定选择器；
- 每个候选选择器经 3 次刷新后仍命中才写入 ``spike/tiktok/selectors.md``。

设计说明：
- 复用 s1 登录态（--user-data-dir 指向 s1 目录），headed 打开便于人工确认聊天页；
- TikTok 卖家中心页面结构未知，脚本采用「候选 URL 探测 + 启发式元素收集 +
  稳定性验证」三步：先尝试常见聊天路径，找不到则提示人工导航到聊天页；
- 启发式收集：按特征关键词（会话列表 / 未读 / 输入框 / 发送按钮 / 消息气泡）
  从当前页面收集候选元素，生成 CSS 选择器（优先 id / data-testid / class）；
- 稳定性验证：收集到候选后自动刷新 3 次，记录每次是否命中，全部命中才写入
  selectors.md；选择器以 JSON 结构落盘，供后续 s4 与 channel_tiktok/selectors.py 使用。

运行方式：``python s2_dom_map.py [--user-data-dir ...] [--chat-url ...]``
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page

from _common import TIKTOK_SELLER_URL, launch_context, setup_logging

logger = logging.getLogger("spike.tiktok.s2_dom_map")

# 本文件所在目录（spike/tiktok），默认路径基于它定位，避免运行位置不同而错位。
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))

# 候选聊天页路径（泰国站后台实际入口为 /chat/inbox/current，实测确认；其余保留候选）。
CANDIDATE_CHAT_PATHS: tuple[str, ...] = (
    "/chat/inbox/current",
    "/chat",
    "/message",
    "/message/chat",
    "/inbox",
    "/customer-service",
)

# 稳定度验证刷新次数（对齐 PLAN 通过标准：3 次刷新仍命中）。
REFRESH_ROUNDS: int = 3

# 特征关键词 -> 语义分组（用于启发式元素分类与 selectors.md 命名）。
FEATURE_KEYWORDS: Dict[str, List[str]] = {
    "conversation_item": ["conversation", "thread", "chat-item", "dialog-list"],
    "unread_badge": ["unread", "badge", "dot"],
    "message_input": ["textarea", "message-input", "chat-input", "composer"],
    "send_button": ["send", "btn-send", "submit"],
    "message_list": ["message-list", "msg-list", "chat-content"],
    "my_message_bubble": ["my-message", "outgoing", "self", "owner"],
}

# 优先使用的属性键（命中即采用，未命中再退化为 class / 标签+位置）。
_PREFERRED_ATTRS: tuple[str, ...] = ("data-testid", "data-test-id", "id", "data-e2e")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TikTok 聊天页 DOM 测绘探查 s2")
    parser.add_argument(
        "--user-data-dir",
        default=os.path.join(_SCRIPT_DIR, "_data", "user_test1"),
        help="持久化用户数据目录（复用 s1 登录态）",
    )
    parser.add_argument("--chat-url", default=None, help="聊天页完整 URL（缺省自动探测）")
    parser.add_argument(
        "--out",
        default=os.path.join(_SCRIPT_DIR, "selectors.json"),
        help="选择器清单 JSON 输出路径",
    )
    return parser.parse_args()


async def _try_open_chat_page(page: Page, chat_url: Optional[str]) -> bool:
    """打开聊天页：优先使用给定 URL，否则依次尝试候选路径。

    Args:
        page: Playwright 页面对象。
        chat_url: 用户显式指定的聊天页 URL（可为 None）。

    Returns:
        成功打开返回 True。
    """
    if chat_url:
        await page.goto(chat_url, wait_until="domcontentloaded", timeout=60_000)
        logger.info("已打开指定聊天页: %s", chat_url)
        return True
    for path in CANDIDATE_CHAT_PATHS:
        url = TIKTOK_SELLER_URL + path
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            final_url = await page.evaluate("location.href")
            logger.info("尝试 %s -> 落点 %s", path, final_url)
            # 未跳回登录页且页面有内容即视为候选命中。
            if "login" not in final_url.lower():
                return True
        except Exception:  # noqa: BLE001 - 单路径失败继续尝试
            logger.warning("候选路径打开失败: %s", path)
    return False


async def _collect_candidate_selectors(page: Page) -> Dict[str, List[str]]:
    """启发式收集页面中符合特征关键词的候选 CSS 选择器。

    对每个语义分组，收集页面中 class / id / data 属性包含特征关键词的元素，
    生成 CSS 选择器（优先 data-testid / id，退化到 class）。仅收集可见元素。

    Args:
        page: 已打开聊天页的 Playwright 页面对象。

    Returns:
        语义分组 -> 候选 CSS 选择器列表。
    """
    result: Dict[str, List[str]] = {}
    # 一次注入 JS 收集所有候选（避免逐个 evaluate 往返）。
    js = """
    (featureKeywords) => {
        const groups = {};
        for (const [group, keywords] of Object.entries(featureKeywords)) {
            const selectors = new Set();
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (!el.offsetParent) continue;  // 不可见元素跳过
                const attrs = [];
                for (const a of el.attributes) {
                    const n = a.name.toLowerCase();
                    const v = (a.value || '');
                    if (['class','id'].includes(n) || n.startsWith('data-')) {
                        attrs.push(n + '=' + v);
                    }
                }
                const hay = attrs.join(' ').toLowerCase();
                for (const kw of keywords) {
                    if (hay.includes(kw)) {
                        let sel = null;
                        if (el.id) sel = '#' + CSS.escape(el.id);
                        else if (el.dataset && (el.dataset.testid || el.dataset.testId)) {
                            sel = '[data-testid="' + el.dataset.testid + '"]';
                        } else if (el.className && typeof el.className === 'string') {
                            const cls = el.className.trim().split(/\\s+/)
                                .filter(c => c.toLowerCase().includes(kw))
                                .map(c => CSS.escape(c)).join('.');
                            if (cls) sel = '.' + cls;
                        }
                        if (sel) selectors.add(sel);
                        break;
                    }
                }
            }
            groups[group] = [...selectors].slice(0, 20);
        }
        return groups;
    }
    """
    result = await page.evaluate(js, FEATURE_KEYWORDS)
    logger.info("启发式收集结果: %s", json.dumps(result, ensure_ascii=False)[:500])
    return result


async def _verify_stable(page: Page, selector: str, rounds: int = REFRESH_ROUNDS) -> bool:
    """验证选择器在多次刷新后仍命中（刷新后等待 1.5s 再查询）。

    Args:
        page: Playwright 页面对象（聊天页）。
        selector: 待验证的 CSS 选择器。
        rounds: 验证刷新次数。

    Returns:
        全部轮次均命中返回 True。
    """
    hit_rounds = 0
    for rnd in range(1, rounds + 1):
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(1500)
            cnt = await page.locator(selector).count()
            if cnt > 0:
                hit_rounds += 1
                logger.info("  选择器 %s 第 %s 次刷新命中", selector, rnd)
            else:
                logger.warning("  选择器 %s 第 %s 次刷新未命中", selector, rnd)
        except Exception as exc:  # noqa: BLE001 - 单轮失败视为未命中
            logger.warning("  选择器 %s 第 %s 次刷新异常: %s", selector, rnd, exc)
    return hit_rounds == rounds


async def _write_selectors_md(stable: Dict[str, str], chat_url: str) -> None:
    """把稳定性验证通过的选择器与关键站点事实写入 selectors.md（spike 产出物）。

    Args:
        stable: 语义分组 -> 稳定选择器 映射。
        chat_url: 实测聊天页 URL（含 oec_seller_id 等参数）。
    """
    md_path = os.path.join(_SCRIPT_DIR, "selectors.md")
    lines = [
        "# TikTok 卖家中心聊天页选择器清单（spike 实测）",
        "",
        "> 本文件由 ``s2_dom_map.py`` 生成：候选选择器经 3 次刷新验证稳定后收录。",
        "> 站点：泰国站 ``seller.tiktokshopglobalselling.com``（生产目标，中文界面）。",
        "",
        "## 已确认的入口与页面事实（s1/s2 实测）",
        "",
        "| 语义 | 选择器 / URL | 说明 |",
        "| --- | --- | --- |",
        "| 聊天入口（侧边栏） | ``div.ub-navItem-b6df03:has-text('客户消息')`` | 首页导航，点击新开标签页 |",
        "| 聊天入口（IM 按钮） | ``div.ub-IMButton-d003ce`` | 首页 IM 悬浮按钮（备用） |",
        "| 聊天页 URL 模板 | ``{base}/chat/inbox/current?oec_seller_id={id}&shop_region=TH&lang=en&cb_shop_region=TH&from=seller_center_navigation_im`` | 必须带 oec_seller_id |",
        "| 实测聊天页 URL | ``%s`` | s2 使用的完整 URL |" % chat_url,
        "| 登录页 URL | ``/account/login`` | title「TikTok Shop Seller Log In | Cross Border」 |",
        "",
        "## 当前有效选择器",
        "",
    ]
    if stable:
        lines.append("| 语义分组 | 选择器 | 验证 |")
        lines.append("| --- | --- | --- |")
        for group, sel in stable.items():
            lines.append(f"| {group} | ``{sel}`` | 3/3 次刷新命中 |")
    else:
        lines.append("（暂无稳定选择器，需人工导航聊天页后重跑）")
    lines += [
        "",
        "## 待确认项（需真实买家会话出现后补测）",
        "",
        "- ``conversation_item``：会话列表条目（当前无会话，未测绘到）；",
        "- ``message_input`` / ``send_button``：消息输入框与发送按钮（无活跃会话时不渲染）；",
        "- ``message_list`` / ``my_message_bubble``：消息流与己方气泡；",
        "- ``LOGIN_PAGE_MARKERS``：登录失效判定标记（登录页 URL 为 ``/account/login``）。",
    ]
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info("selectors.md 已生成: %s", md_path)


async def main() -> int:
    args = _parse_args()
    setup_logging()
    logger.info("开始聊天页 DOM 测绘（s2）")

    playwright = None
    context = None
    try:
        playwright, context = await launch_context(args.user_data_dir, headless=False)
        page = await context.new_page()

        if not await _try_open_chat_page(page, args.chat_url):
            logger.error(
                "候选聊天路径均未命中，请手动在浏览器中导航到聊天页后重跑 "
                "（可传 --chat-url 指定 URL）"
            )
            return 1

        final_url = await page.evaluate("location.href")
        logger.info("聊天页落点: %s", final_url)

        # 首次收集候选选择器。
        candidates = await _collect_candidate_selectors(page)
        if not any(candidates.values()):
            logger.warning(
                "未收集到候选选择器：请确认当前页面确为聊天页，且会话列表 / 输入框可见"
            )
            return 2

        # 每个分组的候选选择器逐一做稳定性验证，取首个通过者。
        stable: Dict[str, str] = {}
        for group, sels in candidates.items():
            if not sels:
                continue
            for sel in sels:
                if await _verify_stable(page, sel):
                    stable[group] = sel
                    logger.info("分组 %s 采用稳定选择器: %s", group, sel)
                    break
                else:
                    logger.warning("分组 %s 候选 %s 未通过稳定性验证", group, sel)

        await _write_selectors_md(stable, final_url)

        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(
                {"chat_url": final_url, "stable": stable, "candidates": candidates},
                fh,
                ensure_ascii=False,
                indent=2,
            )
        logger.info("选择器清单已写出: %s", args.out)
        logger.info(
            "s2 汇总：共 %s 个稳定选择器；chat_url=%s",
            len(stable),
            final_url,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - 探查异常降级为失败
        logger.error("s2 测绘失败: %s", exc)
        return 3
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
