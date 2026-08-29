# -*- coding: utf-8 -*-
"""
channel_tiktok.tiktok_channel —— TikTok 店铺通道主循环（监控 + 登录失效检测）
============================================================================
本文件用途：实现 TikTok 店铺通道的「生命周期管理 + 周期性监控循环 + 登录失效检测」
（TIK-013 关键路径）。与 ``PDDChannel`` 协议一致（``start`` / ``stop`` /
``get_connection_status``），内部以「DOM 轮询快照 + 会话守卫去抖」收消息。

设计要点：
- **营业时间第二道闸**：``start()`` 先查 ``BusinessHours``（含 weekdays）；窗口外
  置 ``DISCONNECTED`` 并 return，不启动浏览器、不登记注册表。
- **浏览器会话**：复用 ``BrowserSession``（每店独立 user-data-dir、可选 headless /
  代理）；``start()`` 经其拉起浏览器并打开聊天页。
- **手动发送复用**：构造接收可选 ``sender``（``TikTokSender``），与自动回复消费器
  共用同一实例（共享串行锁）；``routes/messages`` 从注册表取本通道 ``sender`` 发消息。
- **监控循环**：周期（``poll_interval``）抓会话快照 → ``diff_conversations`` 求新买家
  消息 → ``ReplyDebouncer`` 去抖 → 到期消息转原始报文入 FIFO 队列（复用
  ``common.utils.message_queue`` 与 ``message_handler`` 回调约定）。
- **登录失效检测 + 恢复探测**（Phase 2 保活）：``page.url`` 命中 ``LOGIN_PAGE_MARKERS``
  或聊天页出现 IM 会话过期弹窗（``IM_EXPIRED_MODAL_MARKERS``，TIK-018 实测补充）
  → 置状态 + 触发 ``login_expired`` 告警，随后周期探测登录态恢复（人工重登后自动
  续接监控，超时未恢复才停循环）；快照抓取异常与页面存活探测失败（浏览器退出 /
  崩溃）视为 ``connection_disconnected``。
- **告警去重**：``event_notifier`` 为 ``build_alert_notifier`` 产出的闭包（绑定
  shop_pk 与去重维度），直接调用即可。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、日志禁用 debug（38）、
导入置顶（51）、复用既有组件（52）。
硬约束：本文件为 TIK-013 唯一新增实现文件，**不改任何既有文件**，不真实启动浏览器。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from channel_pdd.core.connection_status import (
    ConnectionState,
    ConnectionStatusManager,
)
from channel_pdd.message_queue import FifoMessageQueue
from channel_tiktok.browser_session import BrowserSession
from channel_tiktok.login_recovery import wait_login_recovery
from channel_tiktok.selectors import (
    IM_EXPIRED_MODAL_MARKERS,
    LOGIN_PAGE_MARKERS,
    TIKTOK_CHAT_PATH,
    TIKTOK_CHAT_URL_TEMPLATE,
    TIKTOK_SELLER_URL,
)
from channel_tiktok.session_guard import ReplyDebouncer
from common.db.repository import Repository
from common.db.session import session_scope
from common.models.config_models import BusinessHours
from engine.business_hours import is_within_business_hours
logger = logging.getLogger("channel_tiktok.tiktok_channel")

# 轮询间隔内消费循环从队列取消息的等待超时（秒）：周期返回以检查停止信号。
_CONSUME_GET_TIMEOUT: float = 1.0

# 消息处理回调签名：message_handler(raw_message, shop_id, user_id) -> Awaitable[None]
MessageHandler = Callable[..., Any]

# 事件通知器签名：event_notifier(event_type: str, content: str) -> Any
EventNotifier = Callable[[str, str], Any]


# ----------------------------------------------------------------------
# 纯函数：会话快照 diff（可属性测试，无 I/O）
# ----------------------------------------------------------------------
def diff_conversations(
    old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """计算两次会话列表快照间「新出现的买家消息」列表（纯函数）。

    ``old`` / ``new`` 为 ``conversation_id -> 最新消息字典`` 映射（消息含
    ``conversation_id`` / ``from_user``（``'user'`` 买家 / ``'mall_cs'`` 卖家）/
    ``msg_id``）。DIFF 契约：仅 ``new`` 中存在且最新 ``from_user == 'user'`` 的会话
    参与；若 ``old`` 无该会话、或最新 ``msg_id`` 与 ``new`` 不同 → 视为新买家消息；
    其余不返回。

    Args:
        old: 上一轮快照；可为 None（视为空）。
        new: 本轮快照；可为 None（视为空）。
    Returns:
        新买家消息字典列表（每项来自 ``new``）。
    """
    old_map: Dict[str, Any] = old if isinstance(old, dict) else {}
    new_map: Dict[str, Any] = new if isinstance(new, dict) else {}
    result: List[Dict[str, Any]] = []
    for cid, msg in new_map.items():
        if not isinstance(msg, dict):
            continue
        # 仅关注买家消息（归一化角色）。
        if msg.get("from_user") != "user":
            continue
        prev = old_map.get(cid)
        prev_id = (
            prev.get("msg_id") if isinstance(prev, dict) else None
        )
        cur_id = msg.get("msg_id")
        # 新会话（prev 为 None）或消息已更新（msg_id 不同）→ 视为新买家消息。
        if prev is None or prev_id != cur_id:
            result.append(msg)
    return result


# ----------------------------------------------------------------------
# 模块内营业时间查询小函数（不 import engine.message_consumer）
# ----------------------------------------------------------------------
def _query_business_hours(shop_pk: int) -> Optional[Dict[str, Any]]:
    """查询指定店铺的营业时间配置（读 BusinessHours 表）。

    仅本模块内使用，避免与 ``engine.message_consumer`` 强耦合（PLAN §5.1 硬约束）。

    Args:
        shop_pk: 店铺主键 shop.id。

    Returns:
        配置字典（``start_time`` / ``end_time`` / ``enabled`` / ``weekdays``）或 None。
    """
    try:
        with session_scope() as session:
            rows = Repository(BusinessHours, session).list(
                filters={"shop_pk": shop_pk}, order_by=False
            )
            if not rows:
                return None
            bh = rows[0]
            return {
                "start_time": bh.start_time,
                "end_time": bh.end_time,
                "enabled": bool(bh.enabled),
                "weekdays": bh.weekdays or "",
            }
    except Exception as exc:  # noqa: BLE001 - 库异常按未配置处理，不阻断通道
        logger.warning(
            "查询营业时间失败（按全天营业处理）: shop_pk=%s, %s", shop_pk, exc
        )
        return None


class TikTokChannel:
    """TikTok 店铺通道：生命周期 + 监控循环 + 登录失效检测（TIK-013）。

    每店一个实例（独立浏览器会话与监控任务），共享 ``ConnectionStatusManager`` 维护
    连接状态；消息入站复用 ``FifoMessageQueue`` + ``message_handler`` 回调。
    """

    def __init__(
        self,
        shop_id: str,
        user_id: int,
        shop_pk: int,
        message_queue: FifoMessageQueue,
        message_handler: Optional[MessageHandler],
        status_manager: Optional[ConnectionStatusManager] = None,
        browser_session: Optional[BrowserSession] = None,
        event_notifier: Optional[EventNotifier] = None,
        poll_interval: float = 5.0,
        debounce_seconds: float = 45.0,
        respect_business_hours: bool = True,
        oec_seller_id: Optional[str] = None,
        sender: Optional[Any] = None,
        login_recovery_interval: float = 60.0,
        login_recovery_max_tries: int = 10,
    ) -> None:
        """初始化 TikTok 通道。

        Args:
            shop_id: TikTok 店铺业务标识。
            user_id: 归属用户 ID。
            shop_pk: 店铺主键 shop.id（营业时间 / 告警去重维度）。
            message_queue: 消息入队 FIFO 队列（``asyncio.Queue`` 语义，含 ``put``）。
            message_handler: 消费回调 ``handler(raw, shop_id, user_id)``；None 仅入队不消费。
            status_manager: 连接状态管理器（缺省自建）。
            browser_session: 可注入浏览器会话（缺省按 shop_pk 自建）。
            event_notifier: 可选告警通知器（``build_alert_notifier`` 产出）；None 不告警。
            poll_interval: 监控轮询间隔（秒，默认 5.0）。
            debounce_seconds: 去抖静默阈值（秒，默认 45.0）。
            respect_business_hours: 是否启用营业时间第二道闸（默认 True）。
            oec_seller_id: 聊天页 URL 必带参数（TIK-018 回填）；None 走基础路径。
            sender: 手动发送复用的 ``TikTokSender``（共享串行锁）；None 只收不发。
            login_recovery_interval: 登录失效后恢复探测间隔（秒，默认 60.0，Phase 2 保活）。
            login_recovery_max_tries: 最大探测次数（默认 10，约 10 分钟，超时停止监控）。
        """
        self.shop_id = shop_id
        self.user_id = user_id
        self.shop_pk = shop_pk
        self.message_queue = message_queue
        self._message_handler = message_handler
        self.status_manager = status_manager or ConnectionStatusManager()
        # 浏览器会话延迟到 start() 时按需自建；测试注入桩。
        self._browser_session: Optional[BrowserSession] = browser_session
        self._event_notifier = event_notifier
        self.poll_interval = poll_interval
        self.respect_business_hours = respect_business_hours
        self.oec_seller_id = oec_seller_id
        self.sender = sender
        self.login_recovery_interval = login_recovery_interval
        self.login_recovery_max_tries = login_recovery_max_tries

        # 会话回复去抖守卫（静默阈值可配置，默认 45.0，对应 TIKTOK_DEBOUNCE_SECONDS）。
        self._debouncer = ReplyDebouncer(silence_seconds=debounce_seconds)

        # 运行时状态。
        self._stop_event: Optional[asyncio.Event] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._consume_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动 TikTok 通道（营业时间闸门 → 浏览器 → 监控循环）。"""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        else:
            self._stop_event.clear()

        # 营业时间第二道闸：窗口外不启动浏览器、不登记注册表。
        if self.respect_business_hours:
            bh = _query_business_hours(self.shop_pk)
            if bh is not None:
                within = is_within_business_hours(
                    bh["start_time"],
                    bh["end_time"],
                    enabled=bh["enabled"],
                    weekdays=bh.get("weekdays"),
                )
                if not within:
                    logger.info(
                        "店铺 shop_id=%s 当前非营业时间，跳过建立连接", self.shop_id
                    )
                    self._set_status(
                        ConnectionState.DISCONNECTED, "非营业时间窗口外"
                    )
                    return

        self._set_status(ConnectionState.CONNECTING)
        try:
            if self._browser_session is None:
                self._browser_session = BrowserSession(shop_pk=self.shop_pk)
            await self._browser_session.start()
            await self._open_chat_page()
        except Exception as exc:  # noqa: BLE001 - 浏览器启动失败置错误态，不影响其它店铺
            logger.error(
                "TikTok 浏览器启动失败: shop_id=%s, %s", self.shop_id, exc
            )
            self._set_status(ConnectionState.ERROR, f"浏览器启动失败: {exc}")
            return

        self._set_status(ConnectionState.CONNECTED)

        # 消费循环（FIFO 消费，PDDChannel 同款用法）。
        if self._message_handler is not None and self._consume_task is None:
            self._consume_task = asyncio.create_task(self._consume_loop())
        # 监控循环：周期快照 diff → 去抖 → 入队；登录失效检测 + 恢复探测。
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("TikTok 通道已启动: shop_id=%s", self.shop_id)

    async def stop(self) -> None:
        """主动停止通道：停循环任务 → 释放浏览器 → 置 DISCONNECTED。"""
        if self._stop_event is not None:
            self._stop_event.set()
        await self._cancel_task("_monitor_task")
        await self._cancel_task("_consume_task")
        if self._browser_session is not None:
            try:
                await self._browser_session.close()
            except Exception as exc:  # noqa: BLE001 - 释放失败忽略，不影响停流程
                logger.warning(
                    "关闭浏览器会话失败（已忽略）: shop_id=%s, %s", self.shop_id, exc
                )
        self._set_status(ConnectionState.DISCONNECTED)
        logger.info("已停止 TikTok 通道: shop_id=%s", self.shop_id)

    async def _cancel_task(self, attr_name: str) -> None:
        """取消并等待内部任务（容错，不向上抛异常）。"""
        task: Optional[asyncio.Task] = getattr(self, attr_name, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as exc:  # noqa: BLE001 - 清理阶段不抛异常
                logger.warning("取消任务 %s 时出错: %s", attr_name, exc)
        setattr(self, attr_name, None)

    # ------------------------------------------------------------------
    # 聊天页打开 / 登录失效检测
    # ------------------------------------------------------------------
    def _chat_url(self) -> str:
        """拼接聊天页 URL（带 oec_seller_id 走完整模板，否则基础路径）。"""
        if self.oec_seller_id:
            return TIKTOK_CHAT_URL_TEMPLATE.format(
                base=TIKTOK_SELLER_URL, id=self.oec_seller_id
            )
        return TIKTOK_SELLER_URL + TIKTOK_CHAT_PATH

    async def _open_chat_page(self) -> None:
        """打开 TikTok 客服聊天页（登录态已就绪时调用）。"""
        page = self._browser_session.page if self._browser_session else None
        if page is None:
            raise RuntimeError("浏览器会话未就绪，无法打开聊天页")
        await page.goto(self._chat_url())
        logger.info("已打开 TikTok 聊天页: shop_id=%s, url=%s", self.shop_id, self._chat_url())

    def _is_login_expired(self) -> bool:
        """检测当前页面是否跳转登录页（登录态失效）。

        ``page.url`` 命中任一 ``LOGIN_PAGE_MARKERS`` 返回 True；无页面 / 无 URL 返回
        False（不误报）。
        """
        page = self._browser_session.page if self._browser_session else None
        if page is None:
            return False
        url = getattr(page, "url", None)
        if not url:
            return False
        return any(marker in url for marker in LOGIN_PAGE_MARKERS)

    async def _check_page_alive(self) -> bool:
        """页面存活探测：浏览器进程退出 / 页面崩溃时 evaluate 必抛，据此置断开。

        TIK-018 实测补充：快照抓取为纯 DOM 读取之外的桩时不触碰页面，浏览器死亡
        无法经快照异常暴露，故监控循环每轮先做最小存活探测（页面对象不存在时视为
        存活，兼容测试桩注入）。
        """
        page = self._browser_session.page if self._browser_session else None
        if page is None:
            return True
        try:
            await page.evaluate("1")
            return True
        except Exception as exc:  # noqa: BLE001 - 浏览器已死/页面失效按断开处理
            logger.error(
                "页面存活探测失败（视为连接断开）: shop_id=%s, %s", self.shop_id, exc
            )
            return False

    async def _is_im_login_expired(self) -> bool:
        """检测 IM 子系统会话过期弹窗（主站登录态有效时仍可能出现）。

        TIK-018 实测：聊天页 URL 不跳转，IM 会话过期以 ``.p-modal`` 弹窗呈现
        （「Your login has expired」）。探测异常一律返回 False（不误报），死亡
        浏览器由 ``_check_page_alive`` 兜底。
        """
        page = self._browser_session.page if self._browser_session else None
        if page is None:
            return False
        try:
            text = await page.evaluate(
                "() => { const m = document.querySelector('.p-modal');"
                " return m ? (m.innerText || '') : ''; }"
            )
        except Exception:  # noqa: BLE001 - 探测失败不误报
            return False
        if not isinstance(text, str):
            return False
        lowered = text.lower()
        return any(marker.lower() in lowered for marker in IM_EXPIRED_MODAL_MARKERS)

    # ------------------------------------------------------------------
    # 事件告警（经 AlertDedup 链路，复用 TIK-005 组件）
    # ------------------------------------------------------------------
    def _on_login_expired(self) -> None:
        """登录态失效处理：置状态 + 经 event_notifier 触发 login_expired 告警。"""
        self._set_status(ConnectionState.DISCONNECTED, "登录态失效")
        if self._event_notifier is not None:
            try:
                self._event_notifier(
                    "login_expired",
                    f"店铺 shop_id={self.shop_id} TikTok 登录态失效，需人工重新登录",
                )
            except Exception as exc:  # noqa: BLE001 - 告警不影响主链路
                logger.warning("登录失效告警失败（已忽略）: %s", exc)

    def _on_connection_disconnected(self, reason: str) -> None:
        """连接断开处理：置状态 + 经 event_notifier 触发 connection_disconnected 告警。"""
        self._set_status(ConnectionState.DISCONNECTED, reason)
        if self._event_notifier is not None:
            try:
                self._event_notifier(
                    "connection_disconnected",
                    f"店铺 shop_id={self.shop_id} TikTok 连接断开: {reason}",
                )
            except Exception as exc:  # noqa: BLE001 - 告警不影响主链路
                logger.warning("连接断开告警失败（已忽略）: %s", exc)

    # ------------------------------------------------------------------
    # 登录失效恢复探测（Phase 2 保活：人工重登后自动续接监控）
    # ------------------------------------------------------------------
    async def _probe_login_recovery(self) -> bool:
        """周期探测登录态恢复（人工重登后续接监控；超时返回 False 退出监控）。"""
        return await wait_login_recovery(
            is_expired=self._is_login_expired,
            reopen_chat=self._open_chat_page,
            sleep=self._sleep_or_stop,
            is_stopped=self._is_stopped,
            interval=self.login_recovery_interval,
            max_tries=self.login_recovery_max_tries,
        )

    # ------------------------------------------------------------------
    # 监控循环
    # ------------------------------------------------------------------
    async def _monitor_loop(self) -> None:
        """监控循环：周期快照 → diff → 去抖 → 入队；登录失效检测 + 恢复探测。"""
        logger.info("TikTok 监控循环启动: shop_id=%s", self.shop_id)
        old: Dict[str, Any] = {}
        while not self._is_stopped():
            # 页面存活探测优先：浏览器进程退出 / 页面崩溃在此暴露并断开告警。
            if not await self._check_page_alive():
                self._on_connection_disconnected("页面存活探测失败（浏览器退出或页面失效）")
                break
            # 登录失效检测优先于快照抓取（URL 跳登录页 或 IM 会话过期弹窗）。
            if self._is_login_expired() or await self._is_im_login_expired():
                self._on_login_expired()
                # Phase 2 保活：失效后周期探测登录态恢复，超时未恢复才退出监控循环。
                recovered = await self._probe_login_recovery()
                if not recovered:
                    break
                old = {}
                continue
            try:
                snapshot = await self._capture_conversations()
            except Exception as exc:  # noqa: BLE001 - 抓取异常视为连接断开
                logger.error(
                    "抓取会话快照失败（视为连接断开）: shop_id=%s, %s",
                    self.shop_id, exc,
                )
                self._on_connection_disconnected(f"会话快照抓取失败: {exc}")
                break
            if not isinstance(snapshot, dict):
                snapshot = {}

            # 归一化：确保每条消息带 from_user（兼容仅含 sender_role 的快照源）。
            new_map = {cid: self._normalize_message(m) for cid, m in snapshot.items()}

            # 纯函数 diff 求新买家消息 → 喂入去抖守卫。
            new_buyer = diff_conversations(old, new_map)
            now = time.time()
            for msg in new_buyer:
                self._debouncer.feed(msg, now)

            # 取走静默期满的应处理买家消息，转原始报文入队。
            due = self._debouncer.pop_due(now)
            for msg in due:
                await self._enqueue_buyer_message(msg)

            old = new_map
            await self._sleep_or_stop(self.poll_interval)
        logger.info("TikTok 监控循环退出: shop_id=%s", self.shop_id)

    async def _capture_conversations(self) -> Dict[str, Any]:
        """抓取当前会话列表快照（选择器待 TIK-018 回填；当前返回空映射，测试注入桩）。"""
        page = self._browser_session.page if self._browser_session else None
        if page is None:
            return {}
        # TODO(TIK-018 实测): 使用 SELECTOR_CONVERSATION_ITEM 解析会话列表，构造
        # {conversation_id: {from_user/sender_role, msg_id, content, ...}} 映射。
        return {}

    @staticmethod
    def _normalize_message(msg: Any) -> Any:
        """归一化单条消息：缺 ``from_user`` 时由 ``sender_role`` 推导（仅内存拷贝）。"""
        if not isinstance(msg, dict) or "from_user" in msg:
            return msg
        sender_role = msg.get("sender_role")
        normalized = dict(msg)
        if sender_role == "buyer":
            normalized["from_user"] = "user"
        elif sender_role == "seller":
            normalized["from_user"] = "mall_cs"
        else:
            normalized["from_user"] = sender_role
        return normalized

    async def _enqueue_buyer_message(self, msg: Dict[str, Any]) -> None:
        """将应处理的买家消息原始报文入队（供 message_handler 消费）。"""
        try:
            await self.message_queue.put(msg)
        except Exception as exc:  # noqa: BLE001 - 入队异常仅记日志，不中断监控
            logger.error("买家消息入队失败（丢弃本条）: shop_id=%s, %s", self.shop_id, exc)

    # ------------------------------------------------------------------
    # 消费循环（PDDChannel 同款用法）
    # ------------------------------------------------------------------
    async def _consume_loop(self) -> None:
        """FIFO 消费循环：单消费者按入队顺序调用 message_handler 处理消息。"""
        while not self._is_stopped():
            try:
                item = await self.message_queue.get(timeout=_CONSUME_GET_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 取消息异常不应终止消费
                logger.error("消费取消息失败: shop_id=%s, %s", self.shop_id, exc)
                continue
            if item is None:
                continue
            payload = getattr(item, "payload", item)
            try:
                await self._message_handler(payload, self.shop_id, self.user_id)
            except Exception as exc:  # noqa: BLE001 - 单条处理异常不影响其它消息
                logger.error("消息消费回调异常: shop_id=%s, %s", self.shop_id, exc)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _is_stopped(self) -> bool:
        """是否已收到停止信号。"""
        return self._stop_event is not None and self._stop_event.is_set()

    async def _sleep_or_stop(self, delay: float) -> None:
        """等待 ``delay`` 秒，期间若收到停止信号则提前返回。"""
        if self._stop_event is None:
            await asyncio.sleep(delay)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    def _set_status(self, state: ConnectionState, error: Optional[str] = None) -> None:
        """写入本店铺连接状态（用户名以 shop_id 呈现）。"""
        self.status_manager.update_status(
            self.shop_id, self.user_id, str(self.shop_id), state, error
        )

    def get_connection_status(self) -> Optional[Dict[str, Any]]:
        """查询本店铺当前连接状态（状态字典；无记录返回 None）。"""
        status = self.status_manager.get_status(self.shop_id, self.user_id)
        return status.to_dict() if status is not None else None


__all__ = [
    "TikTokChannel",
    "diff_conversations",
]
