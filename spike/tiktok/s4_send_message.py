# -*- coding: utf-8 -*-
"""
spike.tiktok.s4_send_message —— 探查 4：DOM 发送全链路验证
====================================================================
本文件用途（对齐 PLAN_TIKTOK.md §2.1 s4_send_message.py 通过标准）：
- 定位会话列表项 → 点击进入会话 → 输入框输入 → 点击发送 → 检测己方气泡；
- 连发 ``--count`` 条（默认 10）全部成功且可检测，测量单条端到端耗时；
- 输出发送可行性结论（DOM 发送 vs 网络重放），供 Phase 1 决策。

设计说明：
- 依赖 s2 产出的 ``spike/tiktok/selectors.json``（stable 选择器）；
  若尚未测绘，可经 ``--selectors`` 手工指定或先跑 s2_dom_map.py；
- 支持 ``--dry-run``：只做「定位会话 + 定位输入框/发送按钮」演练，
  不真正发送（用于无真实买家会话时验证 DOM 可达性）；
- 发送成功判定：点击发送后等待消息流新增己方气泡；若选择器缺失则退化为
  「输入框内容被清空」作为弱成功信号（记录并提示人工核对）。

运行方式：``python s4_send_message.py [--user-data-dir ...] [--selectors ...]``
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page

from _common import launch_context, setup_logging

logger = logging.getLogger("spike.tiktok.s4_send_message")

# 本文件所在目录（spike/tiktok），默认路径基于它定位。
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))

# 默认发送内容模板（仅合规引导话术，Phase 1 口径）。
DEFAULT_TEXT: str = "您好，您的问题已收到，我们将尽快为您处理。谢谢！"

# 发送成功检测超时（毫秒）。
_SEND_SUCCESS_TIMEOUT_MS: int = 15_000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TikTok DOM 发送链路探查 s4")
    parser.add_argument(
        "--user-data-dir",
        default=os.path.join(_SCRIPT_DIR, "_data", "user_test1"),
        help="持久化用户数据目录（复用 s1 登录态）",
    )
    parser.add_argument(
        "--selectors",
        default=os.path.join(_SCRIPT_DIR, "selectors.json"),
        help="s2 产出的选择器清单 JSON 路径",
    )
    parser.add_argument("--chat-url", default=None, help="聊天页 URL（缺省用 s2 记录值）")
    parser.add_argument("--count", type=int, default=10, help="连发条数（默认 10）")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="发送内容模板")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只演练 DOM 定位，不真正发送",
    )
    return parser.parse_args()


def _load_selectors(path: str) -> Dict[str, str]:
    """加载 s2 产出的稳定选择器清单（stable 分组）。

    Args:
        path: selectors.json 路径。

    Returns:
        语义分组 -> 选择器 映射；缺失或格式错误返回空字典。
    """
    if not os.path.exists(path):
        logger.warning("选择器清单不存在: %s（请先运行 s2_dom_map.py）", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("stable", {})
    except Exception as exc:  # noqa: BLE001 - 解析失败返回空
        logger.warning("选择器清单解析失败: %s", exc)
        return {}


async def _locate_first_conversation(page: Page, sel: str) -> bool:
    """点击第一个会话列表项进入会话。

    Args:
        page: 聊天页。
        sel: 会话列表项选择器。

    Returns:
        成功进入会话返回 True。
    """
    try:
        loc = page.locator(sel).first
        await loc.wait_for(state="visible", timeout=10_000)
        await loc.click(timeout=10_000)
        logger.info("已点击第一个会话项: %s", sel)
        await page.wait_for_timeout(1500)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("定位会话列表项失败（%s）: %s", sel, exc)
        return False


async def _type_and_send(
    page: Page,
    input_sel: str,
    send_sel: str,
    bubble_sel: str,
    text: str,
) -> Tuple[bool, float]:
    """执行一次「输入 → 发送 → 成功检测」，返回 (是否成功, 单条耗时秒)。

    Args:
        page: 已进入会话的聊天页。
        input_sel: 消息输入框选择器。
        send_sel: 发送按钮选择器。
        bubble_sel: 己方消息气泡选择器（可为空串表示未测绘到）。
        text: 发送内容。

    Returns:
        成功标志与耗时。
    """
    start = time.monotonic()
    try:
        input_loc = page.locator(input_sel)
        await input_loc.wait_for(state="visible", timeout=10_000)
        before_bubble_count = (
            await page.locator(bubble_sel).count() if bubble_sel else -1
        )
        await input_loc.fill(text)
        await page.wait_for_timeout(200)

        # 优先点击发送按钮；未测绘到则退化为回车发送。
        if send_sel:
            await page.locator(send_sel).first.click(timeout=10_000)
        else:
            await input_loc.press("Enter")

        # 成功检测：气泡数增加（弱信号：输入框内容被清空）。
        ok = False
        deadline = time.monotonic() + _SEND_SUCCESS_TIMEOUT_MS / 1000.0
        while time.monotonic() < deadline:
            if bubble_sel:
                cnt = await page.locator(bubble_sel).count()
                if cnt > before_bubble_count:
                    ok = True
                    break
            else:
                value = await input_loc.input_value()
                if not value:
                    ok = True
                    break
            await page.wait_for_timeout(500)
        elapsed = time.monotonic() - start
        return ok, elapsed
    except Exception as exc:  # noqa: BLE001 - 单次失败不算
        logger.warning("发送失败: %s", exc)
        return False, time.monotonic() - start


async def main() -> int:
    args = _parse_args()
    setup_logging()

    selectors = _load_selectors(args.selectors)
    conv_sel = selectors.get("conversation_item", "")
    input_sel = selectors.get("message_input", "")
    send_sel = selectors.get("send_button", "")
    bubble_sel = selectors.get("my_message_bubble", "")

    if not (conv_sel and input_sel):
        logger.error("缺少会话列表项 / 输入框选择器，无法执行发送演练")
        return 1

    logger.info(
        "开始 DOM 发送探查（s4），选择器: 会话=%s 输入=%s 发送=%s 己方气泡=%s",
        conv_sel,
        input_sel,
        send_sel,
        bubble_sel or "(未测绘)",
    )

    playwright = None
    context = None
    try:
        playwright, context = await launch_context(args.user_data_dir, headless=False)
        page = await context.new_page()
        chat_url = args.chat_url or _chat_url_from_selectors(args.selectors)
        if not chat_url:
            logger.error("缺少聊天页 URL（--chat-url 或 selectors.json 的 chat_url）")
            return 2
        await page.goto(chat_url, wait_until="domcontentloaded", timeout=60_000)
        logger.info("已打开聊天页: %s", chat_url)

        if not await _locate_first_conversation(page, conv_sel):
            logger.error("无法进入任何会话，发送演练中止")
            return 3

        if args.dry_run:
            input_ok = await page.locator(input_sel).first.is_visible()
            send_ok = await page.locator(send_sel).first.is_visible() if send_sel else True
            logger.info(
                "dry-run 演练完成: 输入框可见=%s 发送按钮可见=%s",
                input_ok,
                send_ok,
            )
            return 0 if (input_ok and send_ok) else 4

        results: List[bool] = []
        durations: List[float] = []
        for i in range(1, args.count + 1):
            ok, elapsed = await _type_and_send(
                page, input_sel, send_sel, bubble_sel, args.text
            )
            results.append(ok)
            durations.append(elapsed)
            logger.info("第 %s/%s 条: 成功=%s 耗时=%.2fs", i, args.count, ok, elapsed)
            if not ok:
                logger.warning("第 %s 条发送未检测到成功，继续尝试", i)
            await page.wait_for_timeout(1500)

        success_cnt = sum(results)
        avg = sum(durations) / len(durations) if durations else 0.0
        logger.info(
            "s4 汇总: 成功 %s/%s，平均单条耗时 %.2fs",
            success_cnt,
            args.count,
            avg,
        )
        return 0 if success_cnt == args.count else 5
    except Exception as exc:  # noqa: BLE001 - 探查异常降级为失败
        logger.error("s4 发送探查失败: %s", exc)
        return 6
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


def _chat_url_from_selectors(path: str) -> Optional[str]:
    """从 selectors.json 读取记录的 chat_url。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("chat_url")
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
