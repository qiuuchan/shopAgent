# -*- coding: utf-8 -*-
"""
channel_tiktok.tiktok_sender —— TikTok 消息 DOM 发送器
=====================================================
本文件用途：通过 Playwright 操作 TikTok 卖家后台聊天页 DOM，向指定买家会话发送文本
消息（TIK-012）。设计要点（PLAN §4.3 / §5.5）：

- 同步签名 ``send_text(recipient_uid, content) -> Optional[dict]``，对齐 PDD
  ``SendMessage.send_text``，供 MessageConsumer ``_send_reply`` 经 ``asyncio.to_thread``
  调用时零差异注入。
- 线程桥接：TikTokSender 内部使用 ``asyncio.run_coroutine_threadsafe(dom_send_coro,
  main_loop).result(timeout)`` 将 DOM 操作调度回主事件循环执行，并阻塞等待结果
  （构造时由 TikTokChannel 注入主循环引用 ``main_loop``）。对 MessageConsumer 完全无感。
- 主循环侧串行化：DOM 操作经主循环侧 ``asyncio.Lock`` 串行执行，防止同店并发 DOM
  操作相互干扰（PLAN §9 风险 5）。
- 成功检测：发送后检测「消息流出现己方气泡」（选择器引用 ``selectors.py``，未实测部分
  以 TODO(TIK-018 实测) 标注，见文件内常量），超时（默认 15s）视为失败返回 None。
- 发送最小随机间隔（频率断路器，PLAN §7）：每次发送前在 **桥接之前**（调用方
  工作线程，同步 sleep）等待一个 ``[min, max]`` 区间内的随机间隔，模拟人工回复
  节奏、降低相邻发送过于密集触发平台风控的概率；间隔可经构造参数覆盖，区间上限
  ≤ 0 时关闭（测试友好）。TIK-018 实测修正：不得放在桥接协程内（桥接等待超时
  仅 15s，45-120s 节流必然超时误判失败）。
- ``send_image`` 显式不支持（Phase 1 返回 None）。

选择器处理：会话列表项 / 输入框 / 发送按钮 / 消息流 / 己方气泡选择器在本文件以
TODO(TIK-018 实测) 占位常量声明，不臆造实测值（空店无活跃会话不渲染，见
selectors.py「待确认项」）。正式取值待 TIK-018 真实会话补测回填。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、日志禁用 debug（38）、
不做真实浏览器操作（仅注入假对象测试，TIK-012 不联调）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("channel_tiktok.tiktok_sender")

# 发送超时（秒）：对应 TIKTOK_SEND_TIMEOUT_SECONDS 默认值（PLAN §5.6）。
DEFAULT_SEND_TIMEOUT_SECONDS: float = 15.0

# 发送最小随机间隔（秒）：频率断路器（PLAN §7）——每次发送前在主循环侧 sleep 一个
# [min, max] 区间内的随机间隔，模拟人工回复节奏、降低相邻发送过于密集触发平台风控
# 的概率。区间上限 ≤ 0 时跳过 sleep（可经构造参数关闭，测试友好）。
DEFAULT_MIN_SEND_INTERVAL_SECONDS: float = 45.0
DEFAULT_MAX_SEND_INTERVAL_SECONDS: float = 120.0

# 选择器（TIK-018 真实会话实测回填，定义见 selectors.py「聊天页结构选择器」）。
# 此处 re-export 供测试 / 上层引用（历史占位常量已由实测值替换）。
from channel_tiktok.selectors import (  # noqa: F401 - re-export
    SELECTOR_CONVERSATION_ITEM,
    SELECTOR_CONVERSATION_ITEM_USERNAME,
    SELECTOR_MESSAGE_INPUT,
    SELECTOR_MESSAGE_LIST,
    SELECTOR_MY_MESSAGE_BUBBLE,
    SELECTOR_SEND_BUTTON,
)


class TikTokSender:
    """TikTok 文本消息 DOM 发送器（同步签名，主循环桥接）。

    构造时注入主循环引用与浏览器会话 / 页面引用；测试可注入假对象（FakePage /
    FakeLoop）以验证调用序与线程桥接，无需真实浏览器。
    """

    def __init__(
        self,
        shop_id: str,
        user_id: int,
        *,
        browser_session: Any = None,
        main_loop: Optional[asyncio.AbstractEventLoop] = None,
        send_timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
        min_send_interval: float = DEFAULT_MIN_SEND_INTERVAL_SECONDS,
        max_send_interval: float = DEFAULT_MAX_SEND_INTERVAL_SECONDS,
    ) -> None:
        """构造发送器。

        Args:
            shop_id: 店铺业务标识。
            user_id: 归属用户 ID。
            browser_session: 浏览器会话引用（提供 ``page`` 属性；可注入假对象）。
            main_loop: 主事件循环引用（用于 ``run_coroutine_threadsafe`` 桥接）；
                若为 None，则在同步调用时尝试取 ``asyncio.get_event_loop()`` 兜底。
            send_timeout: 发送超时（秒），默认 15.0。
            min_send_interval: 发送最小随机间隔下限（秒），默认 45.0（频率断路器，
                PLAN §7）；每条发送前随机采样并 sleep。
            max_send_interval: 发送最小随机间隔上限（秒），默认 120.0。
        """
        self.shop_id = shop_id
        self.user_id = user_id
        self.browser_session = browser_session
        self.main_loop = main_loop
        self.send_timeout = send_timeout
        self.min_send_interval = min_send_interval
        self.max_send_interval = max_send_interval
        # 主循环侧串行锁（协程内获取，防同店并发 DOM 操作）。
        self._send_lock: Optional[asyncio.Lock] = None

    # ------------------------------------------------------------------
    # 同步对外接口（对齐 PDD SendMessage.send_text）
    # ------------------------------------------------------------------
    def send_text(
        self,
        recipient_uid: str,
        content: str,
        *,
        enforce_interval: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """发送文本消息（同步签名，主循环桥接）。

        Args:
            recipient_uid: 接收消息的客户 UID（用于定位会话列表项）。
            content: 文本内容。
            enforce_interval: 是否应用发送最小随机间隔（默认 True，自动回复防风控
                节流）；手动发送传 False（人工操作自有节奏，且发送超时 15s 内不应被
                45-120s 间隔拖垮）。

        Returns:
            成功返回响应字典（含 ``success`` 标志）；失败 / 超时返回 None。
        """
        loop = self.main_loop or asyncio.get_event_loop()
        # 频率断路器（PLAN §7）：发送最小随机间隔。TIK-018 实测修正：节流必须在
        # 桥接 **之前**（同步 sleep 于调用方工作线程，不占事件循环）——原实现把
        # sleep 放在桥接协程内，而桥接等待超时仅 send_timeout（15s），45-120s
        # 节流必然触发超时误判失败。
        if enforce_interval and self.max_send_interval > 0:
            time.sleep(random.uniform(self.min_send_interval, self.max_send_interval))
        coro = self._dom_send_text(
            recipient_uid, content, enforce_interval=enforce_interval
        )
        try:
            # 线程桥接：把 DOM 协程调度回主循环执行并阻塞等待结果，超时视为失败。
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as exc:  # noqa: BLE001 - 桥接失败（如主循环已关闭）
            # 协程尚未入队执行，主动关闭，避免 GC 时产生「never awaited」告警。
            coro.close()
            logger.error("TikTok 发送文本消息异常: shop_id=%s, %s", self.shop_id, exc)
            return None
        try:
            result = future.result(timeout=self.send_timeout)
        except Exception as exc:  # noqa: BLE001 - 超时 / 执行异常统一兜底
            # 尽力取消仍在主循环排队的任务（已执行完的取消无效，无害）。
            future.cancel()
            logger.error("TikTok 发送文本消息异常: shop_id=%s, %s", self.shop_id, exc)
            return None
        return result

    def send_image(self, recipient_uid: str, image_url: str) -> Optional[Dict[str, Any]]:
        """发送图片消息（Phase 1 显式不支持，返回 None）。"""
        logger.info("TikTok 图片发送暂不支持(Phase 1): shop_id=%s", self.shop_id)
        return None

    # ------------------------------------------------------------------
    # 主循环侧协程（串行化 DOM 操作 + 成功检测）
    # ------------------------------------------------------------------
    async def _dom_send_text(
        self,
        recipient_uid: str,
        content: str,
        *,
        enforce_interval: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """主循环内执行的 DOM 发送协程（串行化 + 成功检测）。

        步骤：点击会话列表项 → 输入框填入 → human-like 逐字输入 → 点击发送 →
        检测消息流出现己方气泡视为成功（超时视为失败）。

        Args:
            recipient_uid: 接收客户 UID。
            content: 文本内容。
            enforce_interval: 是否应用发送最小随机间隔（默认 True；手动发送传 False，
                人工操作自有节奏，且 15s 发送超时不应被 45-120s 间隔拖垮）。

        Returns:
            成功返回响应字典；失败 / 超时返回 None。
        """
        # 获取主循环侧串行锁（确保同店 DOM 操作串行）。
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            page = self._get_page()
            if page is None:
                logger.error("TikTok 发送失败: 无可用页面 shop_id=%s", self.shop_id)
                return None
            # 频率断路器：已于 send_text 桥接前完成（见其注释），协程内仅做 DOM 操作。
            try:
                # 1) 点击目标会话（按买家用户名定位会话卡，TIK-018 实测选择器）。
                #    注：:text-is 嵌套在 :has() 内不受支持（实测匹配 0），用 :has-text。
                await page.click(self._conversation_selector(recipient_uid))
                # 2) 统计当前己方气泡数（必须在打开会话之后——消息流仅在该会话
                #    打开时渲染；成功检测按「数量 +1」增量判定，避免历史气泡恒真）。
                try:
                    before_bubbles = await page.locator(SELECTOR_MY_MESSAGE_BUBBLE).count()
                except Exception:  # noqa: BLE001 - 假页面无 locator 时退化为 0
                    before_bubbles = 0
                # 3) 输入框填入（human-like 逐字输入在 _type_humanlike 内）。
                await page.fill(SELECTOR_MESSAGE_INPUT, "")
                await self._type_humanlike(page, content)
                # 4) 点击发送。
                await page.click(SELECTOR_SEND_BUTTON)
                # 5) 成功检测：消息流出现「第 before_bubbles+1 个」己方气泡。
                appeared = await self._wait_my_bubble(page, before_bubbles)
                if not appeared:
                    logger.error(
                        "TikTok 发送后未检测到己方气泡(超时): shop_id=%s, to=%s",
                        self.shop_id, recipient_uid,
                    )
                    return None
            except Exception as exc:  # noqa: BLE001 - DOM 异常统一视为失败
                logger.error("TikTok DOM 发送异常: shop_id=%s, %s", self.shop_id, exc)
                return None

        return {
            "success": True,
            "shop_id": self.shop_id,
            "recipient_uid": recipient_uid,
            "content": content,
        }

    async def _type_humanlike(self, page: Any, content: str) -> None:
        """human-like 逐字输入（模拟人工打字节奏）。

        注：Phase 1 为可测试，这里仅做「分段 fill + 模拟延迟」的纯协程实现，
        具体节奏参数（最小/最大间隔）待 TIK-018 实测后细化（PLAN §7 频率断路器）。
        测试可注入 FakePage 验证调用序。
        """
        # 简单分段：逐字输入（真实场景叠加随机延迟，此处保持测试可预测）。
        await page.type(SELECTOR_MESSAGE_INPUT, content)

    async def _wait_my_bubble(self, page: Any, before_bubbles: int = 0) -> bool:
        """等待消息流出现「第 before_bubbles+1 个」己方气泡（成功检测）。

        TIK-018 实测升级：会话内可能已有历史己方气泡，简单等待任一气泡出现会
        恒真；改用 ``:nth-match`` 按「数量增量」等待新气泡。超时或异常返回 False。
        """
        nth = max(int(before_bubbles or 0), 0) + 1
        try:
            await page.wait_for_selector(
                f":nth-match({SELECTOR_MY_MESSAGE_BUBBLE}, {nth})",
                timeout=self.send_timeout * 1000,
            )
            return True
        except Exception:  # noqa: BLE001 - 超时 / 选择器缺失均视为未出现
            return False

    @staticmethod
    def _conversation_selector(recipient_uid: str) -> str:
        """按买家用户名构造目标会话卡选择器（TIK-018 实测结构）。

        会话卡无稳定 uid 属性；``:has(:text-is(...))`` 嵌套文本伪类不受 Playwright
        支持（实测匹配 0），故用 ``:has-text``（卡文本含用户名，子串匹配）。同名
        前缀买家（如 test / test2）存在误配可能，Phase 1 单测试店可接受，转人工
        / 多买家精确路由列为 Phase 2 改进。
        """
        safe = str(recipient_uid).replace("\\", "\\\\").replace('"', '\\"')
        return f"{SELECTOR_CONVERSATION_ITEM}:has-text(\"{safe}\")"

    def _get_page(self) -> Any:
        """从注入的浏览器会话获取活跃页面（可注入 FakePage）。"""
        if self.browser_session is not None and hasattr(self.browser_session, "page"):
            return self.browser_session.page
        return None


__all__ = [
    "TikTokSender",
    "DEFAULT_SEND_TIMEOUT_SECONDS",
    "DEFAULT_MIN_SEND_INTERVAL_SECONDS",
    "DEFAULT_MAX_SEND_INTERVAL_SECONDS",
    "SELECTOR_CONVERSATION_ITEM",
    "SELECTOR_MESSAGE_LIST",
    "SELECTOR_MY_MESSAGE_BUBBLE",
]
