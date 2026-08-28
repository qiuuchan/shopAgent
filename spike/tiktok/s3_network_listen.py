# -*- coding: utf-8 -*-
"""
spike.tiktok.s3_network_listen —— 探查 3：收消息协议被动监听（CDP）
====================================================================
本文件用途（对齐 PLAN_TIKTOK.md §2.1 s3_network_listen.py 通过标准）：
- 复用 s1 登录态打开聊天页，被动监听 ``page.on("websocket")`` 与
  ``page.on("response")``，判定 TikTok 卖家中心收消息走什么协议；
- 尝试从报文中解析 ``(conversation_id, sender_role, content)`` 三元组；
- **只听不重放**：本脚本不发送任何消息、不重放任何网络请求。

设计说明：
- 监听期默认 60 秒（--listen-seconds），期间保持聊天页打开、不做任何写操作；
  若卖家中心有轮询 / 长连接推送，静置即可抓到报文；必要时可人工在页面内
  点击会话（只读操作）触发流量。
- 报文落盘到 ``spike/tiktok/_data/network_dump/``，供离线分析报文结构；
- 关键输出：① 是否存在 WebSocket 连接及其 URL/帧类型；② 聊天相关 XHR/
  Fetch 响应 URL 与响应体片段；③ 尝试提取三元组的成功率。

运行方式：``python s3_network_listen.py [--user-data-dir ...] [--listen-seconds 60]``
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from playwright.async_api import Page

from _common import TIKTOK_CHAT_PATH, TIKTOK_SELLER_URL, launch_context, setup_logging

logger = logging.getLogger("spike.tiktok.s3_network_listen")

# 本文件所在目录（spike/tiktok），默认路径基于它定位。
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))

# 判定为「聊天相关」的关键词（URL / 帧文本命中即记录）。
_CHAT_KEYWORDS: tuple[str, ...] = (
    "conversation",
    "message",
    "chat",
    "thread",
    "inbox",
    "dialog",
    "msg",
)

# 报文落盘目录。
_DUMP_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_data", "network_dump")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TikTok 收消息协议监听探查 s3")
    parser.add_argument(
        "--user-data-dir",
        default=os.path.join(_SCRIPT_DIR, "_data", "user_test1"),
        help="持久化用户数据目录（复用 s1 登录态）",
    )
    parser.add_argument("--listen-seconds", type=int, default=60, help="监听时长（秒）")
    parser.add_argument(
        "--chat-url",
        default=TIKTOK_SELLER_URL + TIKTOK_CHAT_PATH,
        help="聊天页 URL",
    )
    return parser.parse_args()


def _is_chat_related(text: str) -> bool:
    """粗略判定文本是否与聊天相关（命中任一关键词）。"""
    low = text.lower()
    return any(kw in low for kw in _CHAT_KEYWORDS)


def _dump_payload(kind: str, name: str, data: str) -> None:
    """把报文落盘（按类型分目录，文件名带序号与时间戳）。同步写入（报文通常较小）。"""
    os.makedirs(os.path.join(_DUMP_DIR, kind), exist_ok=True)
    fname = f"{name}_{time.time():.0f}.txt"
    try:
        with open(os.path.join(_DUMP_DIR, kind, fname), "w", encoding="utf-8") as fh:
            fh.write(data)
    except Exception as exc:  # noqa: BLE001 - 落盘失败仅告警，不影响监听
        logger.warning("报文落盘失败: %s", exc)


async def _try_extract_triplet(payload: str) -> Optional[Dict[str, Any]]:
    """尝试从 JSON 报文中提取 (conversation_id, sender_role, content) 三元组。

    仅作启发式探测：遍历 JSON 键，寻找常见字段名（conversationId / sender /
    role / content / message / text 等），成功返回字典，失败返回 None。

    Args:
        payload: 原始报文文本（尽量为 JSON）。

    Returns:
        提取成功返回字段字典，否则 None。
    """
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001 - 非 JSON 报文跳过
        return None
    fields: Dict[str, Any] = {}
    _KEY_CANDIDATES: Dict[str, tuple[str, ...]] = {
        "conversation_id": ("conversationid", "conversation_id", "threadid", "dialogid"),
        "sender_role": ("senderrole", "sender_role", "from", "role", "sender"),
        "content": ("content", "text", "message", "body"),
    }

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                for field, keys in _KEY_CANDIDATES.items():
                    if field not in fields and any(kk in kl for kk in keys):
                        if isinstance(v, (str, int, float, bool)):
                            fields[field] = v
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    # 至少提取到 content 且 conversation_id 才视为「可解析」。
    if "content" in fields and "conversation_id" in fields:
        fields["_extractable"] = True
    else:
        fields["_extractable"] = False
    return fields


async def _listen(page: Page, seconds: int, ws_urls: Set[str]) -> Dict[str, Any]:
    """被动监听 WebSocket 帧与 HTTP 响应，收集聊天相关报文。

    Args:
        page: 已打开聊天页的 Playwright 页面对象。
        seconds: 监听时长（秒）。
        ws_urls: 已发现 WebSocket 连接 URL 集合（写回调用方）。

    Returns:
        监听统计字典。
    """
    stats: Dict[str, Any] = {
        "ws_connections": 0,
        "ws_frames": 0,
        "chat_responses": 0,
        "triplet_ok": 0,
        "ws_urls": [],
        "sample_urls": [],
    }
    frame_seq: List[str] = []

    def on_websocket(ws: Any) -> None:
        ws_url = ws.url
        stats["ws_connections"] += 1
        stats["ws_urls"].append(ws_url)
        logger.info("发现 WebSocket 连接: %s", ws_url)
        ws.on("framereceived", lambda payload: _on_frame(payload, ws_url))

    def _on_frame(payload: Any, ws_url: str) -> None:
        data = payload  # frame received payload 为字符串
        stats["ws_frames"] += 1
        frame_seq.append(ws_url)
        if len(frame_seq) > 100:
            frame_seq.pop(0)
        if not _is_chat_related(str(data)):
            return
        logger.info("聊天相关 WS 帧(前 200 字): %s", str(data)[:200])
        _dump_payload("ws", "frame", str(data))
        triplet = _try_extract_triplet(str(data))
        if triplet and triplet.get("_extractable"):
            stats["triplet_ok"] += 1
            logger.info("WS 帧三元组提取成功: %s", json.dumps(triplet, ensure_ascii=False)[:300])

    async def on_response(response: Any) -> None:
        try:
            url = response.url
            if not _is_chat_related(url):
                return
            if "socket" in url.lower() or "/ws/" in url.lower():
                return
            if len(stats["sample_urls"]) < 30:
                stats["sample_urls"].append(url)
            body = await response.text()
            if body and _is_chat_related(body):
                stats["chat_responses"] += 1
                logger.info("聊天相关响应 %s (前 200 字): %s", url, body[:200])
                _dump_payload("http", "response", f"URL: {url}\n{body}")
        except Exception:  # noqa: BLE001 - 单个响应失败不影响监听
            pass

    page.on("websocket", on_websocket)
    page.on("response", on_response)

    logger.info("开始被动监听 %s 秒（只听不重放，保持页面打开）", seconds)
    await page.wait_for_timeout(seconds * 1000)
    page.remove_listener("websocket", on_websocket)
    page.remove_listener("response", on_response)
    return stats


async def main() -> int:
    args = _parse_args()
    setup_logging()
    logger.info("开始收消息协议监听（s3），监听 %s 秒", args.listen_seconds)

    playwright = None
    context = None
    try:
        playwright, context = await launch_context(args.user_data_dir, headless=True)
        page = await context.new_page()
        # 先注册监听再导航，避免漏掉首屏加载阶段的连接。
        ws_urls: Set[str] = set()
        stats = await asyncio.gather(
            _listen(page, args.listen_seconds, ws_urls),
            _open_chat(page, args.chat_url),
        )
        result = stats[0]
        logger.info("监听汇总: %s", json.dumps(result, ensure_ascii=False, indent=2))
        logger.info(
            "结论线索: WS 连接数=%s, WS 帧数=%s, 聊天相关 HTTP 响应=%s, 三元组可解析=%s",
            result["ws_connections"],
            result["ws_frames"],
            result["chat_responses"],
            result["triplet_ok"],
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - 探查异常降级为失败
        logger.error("s3 监听失败: %s", exc)
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


async def _open_chat(page: Page, url: str) -> None:
    """打开聊天页（独立协程，与监听并行启动）。"""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        logger.info("聊天页已打开: %s", url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("聊天页打开异常（监听继续）: %s", exc)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
