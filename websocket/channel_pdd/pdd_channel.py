# -*- coding: utf-8 -*-
"""
channel_pdd.pdd_channel —— 拼多多店铺连接服务（WebSocket 收发）
=============================================================
本文件用途：复用改造参照项目 Customer-Agent-1.2.0 的
``Channel/pinduoduo/pdd_channel.py``（PDDChannel）及其 ConnectionMixin /
LifecycleMixin，实现本系统 websocket 服务的「店铺连接服务」，满足需求
5.1 / 5.2 / 5.6 / 5.8：

- 需求 5.1：启动某店铺连接时，与 ``wss://m-ws.pinduoduo.com/`` 建立 WebSocket
  连接并将连接状态更新为「已连接」。
- 需求 5.2：连接处于「已连接」时，按配置心跳间隔发送心跳保持连接，并记录最近
  心跳时间（北京时间）。
- 需求 5.4 / 5.5（与 task 10.8 状态机协同）：意外断开 / 歧义状态时按指数退避
  自动重连并置「重连中」；达重连上限置「错误」（风控日志由 task 10.8 状态机或
  上层据状态生成）。
- 需求 5.3（与 task 10.6 FIFO 队列协同）：收到客户消息写入消息队列按序消费。
- 需求 5.6：自动回复引擎生成回复内容时，经 WebSocket 发送至对应客户会话。
- 需求 5.8：连接状态查询返回各店铺当前连接状态及最近心跳时间（北京时间）。

解耦设计（并行任务按接口约定协作，缺位时安全降级）：
- **Token 获取**：通过可注入的 ``token_provider`` 回调取得 access_token，默认
  采用 ``pdd_token.default_token_provider``（复用 task 10.1 BaseRequest）。
- **消息入队**：通过可注入的 ``message_queue``（须具备异步 ``put`` 协程）写入；
  缺省使用内置 ``asyncio.Queue``（FIFO），与 task 10.6 的 FIFO 队列接口兼容。
- **状态管理**：通过 ``ConnectionStatusManager`` 写入连接状态与最近心跳时间，
  task 10.8 可在此基础上扩展状态判定与风控日志。
- **回复发送**：自动回复的实际下发走 HTTP 接口（``channel_pdd.api.send_message``，
  由 ``engine.message_consumer`` 调用），**非** 经本类的 ``send_reply``；
  ``send_reply`` 仅为「经当前 WebSocket 主动下发报文」的通用能力封装（备用，
  如后续的已读回执 / 协议层报文），不参与自动回复主链路。

实现约束（开发规范）：单文件 ≤500 行、文件名用下划线、导入置顶、注释完善、全中文；
地址 ``wss://m-ws.pinduoduo.com/`` 为拼多多固定基址（需求 5.1 明确指定），允许写死；
时间统一北京时间（开发规范 17）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

import websockets
from websockets import exceptions as ws_exceptions

from channel_pdd.core.connection_status import (
    ConnectionState,
    ConnectionStatusManager,
)
from channel_pdd.core.pdd_config import HeartbeatConfig, ReconnectConfig
from channel_pdd.core.pdd_token import default_token_provider
from channel_pdd.message_queue import FifoMessageQueue
from channel_pdd.pdd_message import PDDChatMessage

logger = logging.getLogger("channel_pdd.pdd_channel")

# 拼多多商家后台 WebSocket 固定基址（需求 5.1 明确指定，允许写死）。
PDD_WEBSOCKET_BASE_URL: str = "wss://m-ws.pinduoduo.com/"

# 握手参数中的 API 版本号（参照 Customer-Agent 实测口径）。
API_VERSION: str = "202506091557"

# 底层 websockets ping / 接收等待相关默认值。
_WS_PING_INTERVAL: float = 60.0
_WS_PING_TIMEOUT: float = 30.0
_WS_MAX_SIZE: int = 10 ** 7
_WS_CLOSE_TIMEOUT: float = 10.0

# 消费循环从队列取消息的等待超时（秒）：周期性返回以检查停止信号。
_CONSUME_GET_TIMEOUT: float = 1.0


def _log_raw_message_enabled() -> bool:
    """是否打印收到的完整原始报文（供排查消息结构，默认开启）。

    经环境变量 ``PDD_LOG_RAW_MESSAGE`` 控制：取值为 0 / false / no / off（不区分
    大小写）时关闭，其余（含未配置）默认开启。原始报文可能较大且含会话明文，
    仅用于排障，生产环境可经环境变量关闭。

    Returns:
        True 表示打印完整原始报文；False 表示不打印。
    """
    raw = os.environ.get("PDD_LOG_RAW_MESSAGE")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


class MessageQueueProtocol(Protocol):
    """消息队列协议（与 task 10.6 FIFO 队列接口约定）。

    要求提供异步 ``put`` 与 ``get`` 协程：``put`` 按到达顺序入队；``get`` 按 FIFO
    顺序取出（支持超时返回 None 以便消费循环周期性检查停止信号）。内置
    ``FifoMessageQueue`` 满足该协议。
    """

    async def put(self, item: Any) -> Any:  # pragma: no cover - 协议声明
        ...

    async def get(self, timeout: Optional[float] = None) -> Any:  # pragma: no cover
        ...


# Token 提供回调签名：token_provider(shop_id, user_id, channel_name) -> str | None
TokenProvider = Callable[..., Optional[str]]

# 消息处理回调签名：on_message(raw_message, shop_id, user_id) -> Awaitable[None]
MessageHandler = Callable[..., Awaitable[None]]


class PDDChannel:
    """拼多多店铺连接服务：建连、心跳、指数退避重连、收发与状态查询。

    每个店铺账号对应一个 ``PDDChannel`` 实例（各自的 WebSocket 连接与事件循环
    任务），通过共享的 ``ConnectionStatusManager`` 维护全局连接状态。
    """

    def __init__(
        self,
        shop_id: str,
        user_id: int,
        username: Optional[str] = None,
        channel_name: str = "pinduoduo",
        status_manager: Optional[ConnectionStatusManager] = None,
        reconnect_config: Optional[ReconnectConfig] = None,
        heartbeat_config: Optional[HeartbeatConfig] = None,
        token_provider: Optional[TokenProvider] = None,
        message_queue: Optional[MessageQueueProtocol] = None,
        message_handler: Optional[MessageHandler] = None,
        event_notifier: Optional[Callable[..., Any]] = None,
    ) -> None:
        """初始化店铺连接服务。

        Args:
            shop_id: 拼多多店铺业务标识。
            user_id: 归属用户 ID。
            username: 账号用户名（用于状态展示，缺省回退为 user_id）。
            channel_name: 渠道名称（默认 pinduoduo）。
            status_manager: 连接状态管理器（缺省自建独立实例）。
            reconnect_config: 重连配置（缺省采用默认指数退避配置）。
            heartbeat_config: 心跳配置（缺省采用默认配置）。
            token_provider: Token 获取回调（缺省采用 default_token_provider）。
            message_queue: 消息入队队列（缺省使用内置 asyncio.Queue，FIFO）。
            message_handler: 消息消费回调（缺省仅入队，由 task 10.6 / 上层消费）。
            event_notifier: 可选系统事件通知器（TIK-005 告警链路，默认 None）。
                签名为 ``event_notifier(event_type: str, content: str)``；当重连达上限
                置「错误」时触发 ``connection_disconnected`` 事件。**为 None 时行为与
                现状逐字节一致（硬约束），不引入任何额外调用。**
        """
        self.shop_id = shop_id
        self.user_id = user_id
        self.username = username or str(user_id)
        self.channel_name = channel_name

        self.status_manager = status_manager or ConnectionStatusManager()
        self.reconnect_config = reconnect_config or ReconnectConfig()
        self.heartbeat_config = heartbeat_config or HeartbeatConfig()
        self._token_provider: TokenProvider = token_provider or default_token_provider

        # 消息队列：缺省内置 FifoMessageQueue（FIFO），与 task 10.6 接口一致。
        # 由专用消费循环（_consume_loop）按 FIFO 顺序「单消费者」消费，保证
        # 「入队顺序 == 消费顺序」（需求 5.3），不再以并发任务乱序处理。
        self.message_queue: MessageQueueProtocol = message_queue or FifoMessageQueue(
            name=f"{user_id}:{shop_id}"
        )
        self._message_handler = message_handler

        # 系统事件通知器（TIK-005 告警链路）：默认 None，保持既有行为零变更。
        # 由 connection_manager 注入（闭包已绑定 shop_pk 的 alert notifier）。
        self._event_notifier: Optional[Callable[..., Any]] = event_notifier

        # 运行时状态。
        self.ws: Optional[Any] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        # FIFO 消费任务：贯穿连接生命周期（跨重连保持），按序消费队列消息。
        self._consume_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # 启停连接
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动店铺连接（按需带指数退避自动重连，需求 5.1 / 5.4）。"""
        self._stop_event = asyncio.Event()
        self.status_manager.update_status(
            self.shop_id, self.user_id, self.username, ConnectionState.CONNECTING
        )
        # 启动 FIFO 消费循环（跨重连持续运行，按入队顺序单消费者消费，需求 5.3）。
        if self._message_handler is not None and self._consume_task is None:
            self._consume_task = asyncio.create_task(self._consume_loop())
        if self.reconnect_config.enable_auto_reconnect:
            self._connect_task = asyncio.create_task(self._connect_with_retry())
        else:
            self._connect_task = asyncio.create_task(self._connect_once())

    async def stop(self) -> None:
        """主动停止店铺连接并清理任务（需求 5.x：主动停止置「断开」）。"""
        if self._stop_event is not None:
            self._stop_event.set()

        await self._cancel_task("_heartbeat_task")
        await self._cancel_task("_connect_task")

        # 停止 FIFO 消费循环（关闭队列拒绝新入队，消费循环检测停止信号后退出）。
        try:
            self.message_queue.close()
        except Exception:  # noqa: BLE001 - 队列无 close（如注入桩）时忽略
            pass
        await self._cancel_task("_consume_task")

        if self.ws is not None:
            await self._safe_close_websocket(self.ws)
            self.ws = None

        self.status_manager.update_status(
            self.shop_id, self.user_id, self.username, ConnectionState.DISCONNECTED
        )
        logger.info("已停止店铺 shop_id=%s 账号 %s 的连接", self.shop_id, self.username)

    async def _cancel_task(self, attr_name: str) -> None:
        """取消并等待指定的内部任务（容错，不向上抛异常）。

        Args:
            attr_name: 实例上任务属性名（如 ``_connect_task``）。
        """
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

    async def _consume_loop(self) -> None:
        """FIFO 消费循环：单消费者按入队顺序逐条消费消息（需求 5.3）。

        关键点：接收循环（``_message_loop``）只负责「入队」（快速、不阻塞接收与
        心跳）；本循环作为唯一消费者顺序 ``get`` 并处理，严格保证「入队顺序 ==
        消费顺序」。处理较慢的消息（如 AI 回复）期间，本循环 ``await`` 让出事件
        循环，其它店铺各自的消费循环可并发推进（跨店铺并发、店内 FIFO）。

        周期性以超时 ``get`` 返回，便于检查停止信号后优雅退出。
        """
        while not self._is_stopped():
            try:
                message = await self.message_queue.get(timeout=_CONSUME_GET_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 取消息异常不应终止消费循环
                logger.error("消费取消息失败: shop_id=%s, 错误: %s", self.shop_id, exc)
                continue
            if message is None:
                # 超时无消息：回到循环顶部检查停止信号。
                continue
            # 兼容 FifoMessageQueue（QueuedMessage 包装）与裸 payload 两种来源。
            payload = getattr(message, "payload", message)
            await self._handle_message(payload)

    # ------------------------------------------------------------------
    # 指数退避自动重连（需求 5.4 / 5.5）
    # ------------------------------------------------------------------
    async def _connect_with_retry(self) -> None:
        """带指数退避的自动重连循环：达上限置「错误」（需求 5.4 / 5.5）。"""
        for attempt in range(self.reconnect_config.max_attempts):
            if self._is_stopped():
                self.status_manager.update_status(
                    self.shop_id, self.user_id, self.username,
                    ConnectionState.DISCONNECTED,
                )
                return

            # 第二次及以后的尝试视为「重连中」（需求 5.4）。
            if attempt > 0:
                self.status_manager.update_status(
                    self.shop_id, self.user_id, self.username,
                    ConnectionState.RECONNECTING,
                )
                logger.info(
                    "尝试重连（%s/%s）: shop_id=%s",
                    attempt + 1, self.reconnect_config.max_attempts, self.shop_id,
                )

            try:
                await self._connect_once()
                # 正常结束（主动停止 / 连接关闭）。若为主动停止则不再重连。
                if self._is_stopped():
                    return
                # 非主动停止的断开视为歧义 / 意外断开，继续退避重连。
            except Exception as exc:  # noqa: BLE001 - 单次连接异常进入退避重连
                logger.warning("连接异常: shop_id=%s, 错误: %s", self.shop_id, exc)

            if self._is_stopped():
                self.status_manager.update_status(
                    self.shop_id, self.user_id, self.username,
                    ConnectionState.DISCONNECTED,
                )
                return

            # 最后一次尝试失败：置「错误」并结束（需求 5.5）。
            if attempt == self.reconnect_config.max_attempts - 1:
                self.status_manager.update_status(
                    self.shop_id, self.user_id, self.username,
                    ConnectionState.ERROR, "重连次数已达上限",
                )
                logger.error(
                    "连接失败，已达最大重试次数: shop_id=%s", self.shop_id
                )
                # TIK-005：达到重连上限（连接断开告警链路）触发事件通知。
                # 仅当注入了 event_notifier 才触发；为 None 时本分支与现状一致。
                if self._event_notifier is not None:
                    content = (
                        f"店铺 shop_id={self.shop_id} 长连接断开，"
                        f"重连已达上限（user_id={self.user_id}）"
                    )
                    try:
                        result = self._event_notifier(
                            "connection_disconnected", content
                        )
                        # 通知器可能为协程（异步发送），兼容 awaited 触发。
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:  # noqa: BLE001 - 告警不应影响主链路
                        logger.warning(
                            "连接断开事件通知失败（已忽略）: shop_id=%s, %s",
                            self.shop_id, exc,
                        )
                return

            # 指数退避等待（封顶 max_delay），等待期间响应停止信号。
            delay = self.reconnect_config.compute_delay(attempt)
            logger.warning("连接失败，%.1f 秒后重试: shop_id=%s", delay, self.shop_id)
            if await self._sleep_or_stop(delay):
                self.status_manager.update_status(
                    self.shop_id, self.user_id, self.username,
                    ConnectionState.DISCONNECTED,
                )
                return

    async def _sleep_or_stop(self, delay: float) -> bool:
        """退避等待 ``delay`` 秒，期间若收到停止信号则提前返回。

        Args:
            delay: 等待秒数。

        Returns:
            因停止信号提前结束返回 True；正常等待结束返回 False。
        """
        if self._stop_event is None:
            await asyncio.sleep(delay)
            return False
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            return True  # 停止事件在等待期内被设置
        except asyncio.TimeoutError:
            return False  # 等待正常超时，可继续重连

    def _is_stopped(self) -> bool:
        """判断是否已收到停止信号。"""
        return self._stop_event is not None and self._stop_event.is_set()

    # ------------------------------------------------------------------
    # 单次建连与收发（需求 5.1 / 5.3 / 5.6）
    # ------------------------------------------------------------------
    async def _connect_once(self) -> None:
        """执行单次 WebSocket 建连、心跳启动与消息接收循环。

        建连成功置「已连接」（需求 5.1）；连接关闭 / 异常向上抛出由重连循环处理。
        """
        access_token = self._token_provider(
            self.shop_id, self.user_id, self.channel_name
        )
        if not access_token:
            # Token 缺失：登录 / Token 模块未就绪时安全降级，抛出供重连循环处理。
            raise RuntimeError("未获取到有效 access_token（登录/Token 模块待接入）")

        full_url = self._build_handshake_url(access_token)
        logger.info("正在连接拼多多 WebSocket: shop_id=%s", self.shop_id)

        # 保活策略二选一，避免双重保活冗余：
        # - 启用自定义心跳循环（_heartbeat_loop）时，关闭 websockets 内建 ping
        #   （ping_interval=None），由自定义心跳统一保活并记录最近心跳时间（需求 5.2/5.8）；
        # - 未启用自定义心跳时，保留内建 ping 作为兜底保活与死连接检测。
        use_custom_heartbeat = self.heartbeat_config.enable_heartbeat
        ws_ping_interval = None if use_custom_heartbeat else _WS_PING_INTERVAL
        ws_ping_timeout = None if use_custom_heartbeat else _WS_PING_TIMEOUT

        async with websockets.connect(
            full_url,
            ping_interval=ws_ping_interval,
            ping_timeout=ws_ping_timeout,
            max_size=_WS_MAX_SIZE,
            compression=None,
            close_timeout=_WS_CLOSE_TIMEOUT,
        ) as websocket:
            self.ws = websocket
            self.status_manager.update_status(
                self.shop_id, self.user_id, self.username, ConnectionState.CONNECTED
            )
            logger.info("WebSocket 连接已建立: shop_id=%s", self.shop_id)

            # 连接建立后将客服置为「在线」，拼多多方会向该客服推送客户消息
            # （需求 5；同步 HTTP 调用丢入线程池，避免阻塞事件循环）。
            await self._set_cs_online()

            # 启动心跳保活（需求 5.2）。
            if use_custom_heartbeat:
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(websocket)
                )

            try:
                await self._message_loop(websocket)
            finally:
                await self._cancel_task("_heartbeat_task")
                self.ws = None

    def _build_handshake_url(self, access_token: str) -> str:
        """拼接 WebSocket 握手 URL（携带 access_token 等参数）。

        Args:
            access_token: 连接令牌。

        Returns:
            完整的握手 URL（基址为拼多多固定地址）。
        """
        params = {
            "access_token": access_token,
            "role": "mall_cs",
            "client": "web",
            "version": API_VERSION,
        }
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return f"{PDD_WEBSOCKET_BASE_URL}?{query}"

    async def _message_loop(self, websocket: Any) -> None:
        """消息接收循环：收到客户消息写入消息队列按序消费（需求 5.3）。

        Args:
            websocket: 已建立的 WebSocket 连接。
        """
        try:
            async for raw_message in websocket:
                if self._is_stopped():
                    logger.info("收到停止信号，退出消息循环: shop_id=%s", self.shop_id)
                    break
                # 收到消息即打 info 日志：复用 PDDChatMessage 解析出消息类型与文本
                # 内容（规范 52 复用共通），便于直观观测客户发来的内容。
                self._log_received_message(raw_message)
                await self._enqueue_message(raw_message)
        except ws_exceptions.ConnectionClosedOK:
            logger.info("WebSocket 正常关闭: shop_id=%s", self.shop_id)
        except ws_exceptions.ConnectionClosed as exc:
            logger.warning("WebSocket 连接关闭: shop_id=%s, 错误: %s", self.shop_id, exc)
            raise  # 交由重连循环按歧义 / 意外断开处理
        except Exception as exc:  # noqa: BLE001
            logger.error("消息循环错误: shop_id=%s, 错误: %s", self.shop_id, exc)
            raise

    def _log_received_message(self, raw_message: Any) -> None:
        """解析并打印收到的消息（提取发送方 / 类型 / 文本内容，失败则降级打印原文）。

        复用 ``PDDChatMessage`` 解析器还原消息语义：文本类直接展示文本内容；
        商品咨询 / 订单等结构化消息展示其结构化字段；解析异常时降级打印截断原文，
        保证日志始终可观测，且不影响后续入队与处理（仅日志用途，异常不外抛）。

        Args:
            raw_message: 从 WebSocket 收到的原始消息（文本 / 字节）。
        """
        try:
            text = (
                raw_message.decode("utf-8")
                if isinstance(raw_message, (bytes, bytearray))
                else str(raw_message)
            )
            # 排障开关开启时，先原样打印收到的完整报文（不解析、不截断），便于
            # 自行分析各消息类型的原始结构（经 PDD_LOG_RAW_MESSAGE 控制）。
            if _log_raw_message_enabled():
                logger.info(
                    "收到消息(完整原文): shop_id=%s, 报文=%s", self.shop_id, text
                )
            payload = json.loads(text)
            chat = PDDChatMessage(payload)
            logger.info(
                "收到消息: shop_id=%s, 类型=%s, 发送方=%s(%s), 昵称=%s, 内容=%s",
                self.shop_id,
                chat.user_msg_type,
                chat.from_user,
                chat.from_uid,
                chat.nickname,
                chat.content,
            )
        except Exception:  # noqa: BLE001 - 日志解析失败降级打印原文，绝不影响主流程
            preview = str(raw_message)
            if len(preview) > 500:
                preview = preview[:500] + "...(已截断)"
            logger.info("收到消息(原始): shop_id=%s, 内容=%s", self.shop_id, preview)

    async def _enqueue_message(self, raw_message: Any) -> None:
        """将原始消息写入 FIFO 队列，由消费循环按序处理（需求 5.3）。

        关键点：仅完成「入队」这一快速、非阻塞操作，使「接收」与「处理」彻底
        解耦——接收循环不会因处理耗时（解析→决策→AI→发送→落库）而阻塞后续消息
        接收与心跳；实际消费由唯一的 ``_consume_loop`` 顺序进行，保证「入队顺序
        == 消费顺序」（不再以并发任务乱序处理）。

        队列已满（达容量上限）时记录告警并丢弃当前消息，避免无界堆积导致内存
        泄漏；正常情况下消费循环持续消费，队列不会积压到上限。

        Args:
            raw_message: 从 WebSocket 收到的原始消息（文本 / 字节）。
        """
        try:
            await self.message_queue.put(raw_message)
        except Exception as exc:  # noqa: BLE001 - 入队异常（队列满 / 关闭）不中断接收
            logger.error("消息入队失败（丢弃本条）: shop_id=%s, 错误: %s", self.shop_id, exc)

    async def _handle_message(self, raw_message: Any) -> None:
        """处理单条消息（消费回调的包装，异常不外抛，避免中断消费循环）。

        Args:
            raw_message: 待处理的原始消息。
        """
        if self._message_handler is None:
            return
        try:
            await self._message_handler(raw_message, self.shop_id, self.user_id)
        except Exception as exc:  # noqa: BLE001 - 单条处理异常不影响其它消息
            logger.error("消息消费回调异常: shop_id=%s, 错误: %s", self.shop_id, exc)

    # ------------------------------------------------------------------
    # 心跳保活（需求 5.2 / 5.8）
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self, websocket: Any) -> None:
        """心跳检查循环：周期发送 ping 并记录最近心跳时间（北京时间，需求 5.2/5.8）。

        Args:
            websocket: 已建立的 WebSocket 连接。
        """
        consecutive_failures = 0
        try:
            while not self._is_stopped():
                try:
                    waiter = websocket.ping()
                    await asyncio.wait_for(
                        waiter, timeout=self.heartbeat_config.heartbeat_timeout
                    )
                    consecutive_failures = 0
                    # 记录最近心跳成功时间（北京时间，供连接状态查询，需求 5.8）。
                    self.status_manager.record_heartbeat(self.shop_id, self.user_id)
                    # 心跳收到响应（pong）打 info 日志，便于运维观测连接保活情况。
                    logger.info(
                        "心跳正常: shop_id=%s, user_id=%s, 下次心跳 %.0f 秒后",
                        self.shop_id,
                        self.user_id,
                        self.heartbeat_config.heartbeat_interval,
                    )
                    await asyncio.sleep(self.heartbeat_config.heartbeat_interval)
                except asyncio.TimeoutError:
                    consecutive_failures += 1
                    logger.warning(
                        "心跳超时: shop_id=%s, 连续失败 %s 次",
                        self.shop_id, consecutive_failures,
                    )
                    if consecutive_failures >= self.heartbeat_config.max_heartbeat_failures:
                        logger.error(
                            "心跳连续失败超过上限，触发重连: shop_id=%s", self.shop_id
                        )
                        await self._safe_close_websocket(websocket)
                        break
        except asyncio.CancelledError:
            logger.info("心跳循环被取消: shop_id=%s", self.shop_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("心跳循环异常: shop_id=%s, 错误: %s", self.shop_id, exc)

    # ------------------------------------------------------------------
    # 客服在线状态（需求 5：连接建立后置在线，拼多多才推送消息）
    # ------------------------------------------------------------------
    async def _set_cs_online(self) -> None:
        """将本店铺客服置为「在线」（同步 HTTP 调用丢线程池，失败不影响连接）。"""
        try:
            # 延迟导入，避免模块级循环依赖；同步请求经 to_thread 不阻塞事件循环。
            from channel_pdd.api.set_cs_status import set_cs_online

            ok = await asyncio.to_thread(
                set_cs_online, self.shop_id, self.user_id, self.channel_name
            )
            if ok:
                logger.info("已将客服置为在线: shop_id=%s", self.shop_id)
            else:
                logger.warning("设置客服在线失败（不影响连接）: shop_id=%s", self.shop_id)
        except Exception as exc:  # noqa: BLE001 - 置在线失败不影响连接主流程
            logger.error("设置客服在线异常: shop_id=%s, %s", self.shop_id, exc)

    # ------------------------------------------------------------------
    # 回复发送（需求 5.6）
    # ------------------------------------------------------------------
    async def send_reply(self, payload: Any) -> bool:
        """经当前 WebSocket 主动下发一条报文（通用能力，备用）。

        说明：自动回复的实际下发走 HTTP 接口（``send_message``），不经本方法；
        本方法仅封装「经 ws 主动发送」的通用能力，供后续协议层报文 / 已读回执等
        场景按需使用。

        Args:
            payload: 报文内容。字典 / 列表自动序列化为 JSON 字符串；字符串 / 字节
                直接发送。

        Returns:
            发送成功返回 True；连接不可用或发送失败返回 False。
        """
        if self.ws is None:
            logger.error("发送回复失败：连接不可用 shop_id=%s", self.shop_id)
            return False

        if isinstance(payload, (dict, list)):
            message = json.dumps(payload, ensure_ascii=False)
        else:
            message = payload

        try:
            await self.ws.send(message)
            return True
        except Exception as exc:  # noqa: BLE001 - 发送失败记录日志后降级
            logger.error("发送回复失败: shop_id=%s, 错误: %s", self.shop_id, exc)
            return False

    # ------------------------------------------------------------------
    # 连接状态查询（需求 5.8）
    # ------------------------------------------------------------------
    def get_connection_status(self) -> Optional[Dict[str, Any]]:
        """查询本店铺当前连接状态及最近心跳时间（北京时间，需求 5.8）。

        Returns:
            连接状态字典（含 state / state_label / last_heartbeat_time 等）；
            尚无状态记录时返回 None。
        """
        status = self.status_manager.get_status(self.shop_id, self.user_id)
        return status.to_dict() if status is not None else None

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    @staticmethod
    async def _safe_close_websocket(websocket: Any) -> None:
        """安全关闭 WebSocket（兼容同步 / 异步 close，吞掉异常）。

        Args:
            websocket: 待关闭的 WebSocket 连接。
        """
        try:
            close_fn = getattr(websocket, "close", None)
            if close_fn is None:
                return
            result = close_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 关闭失败不影响主流程
            logger.warning("关闭 WebSocket 失败: %s", exc)


__all__ = [
    "PDDChannel",
    "PDD_WEBSOCKET_BASE_URL",
    "API_VERSION",
    "MessageQueueProtocol",
]
