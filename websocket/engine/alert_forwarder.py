# -*- coding: utf-8 -*-
"""
engine.alert_forwarder —— 系统事件告警转发器（websocket → backend）
====================================================================
本文件用途：把 websocket 侧的系统事件告警（连接断开 / 登录态失效）转发给 **backend**
的内部接口 ``/api/v1/internal/notify-events``，由 backend 按「店铺 + 已启用通知渠道」
（企微群机器人等）推送并落 ``pdd_notify_record``（需求 18.3 / 18.4）。

背景（TIK-018 实测补充）：``build_alert_notifier`` 的 ``send_cb`` 此前恒为 ``None``
（仅日志占位），告警无法到达企微。本模块产出真实 ``send_cb`` 供
``build_alert_notifier`` 注入，补齐服务间最后一公里。

设计要点（对齐 ``channel_pdd.chat_event_forwarder`` 同款约定）：
- 经 common 统一服务间 HTTP 客户端 ``service_client`` 调 backend 内部接口，地址经
  环境变量配置（禁止写死 localhost，规范 21）；携带共享密钥 ``X-Internal-Token``。
- 尽力转发：失败仅记日志，绝不抛异常打断告警去重主链路（需求 26）。
- 事件低频且载荷极小：同步 HTTP 经 ``asyncio.to_thread`` 丢线程池执行，事件循环
  仅调度不等待（fire-and-forget）；无事件循环时（单测 / 同步上下文）退化为直调。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、日志禁用 debug（38）。
"""
from __future__ import annotations

import asyncio
import logging

from common.core.config import get_settings
from common.services import service_client

logger = logging.getLogger("engine.alert_forwarder")

# backend 内部系统事件通知接口相对路径（与 backend 路由约定一致）。
_NOTIFY_EVENTS_PATH: str = "/api/v1/internal/notify-events"

# 转发超时（秒）：尽力转发，避免占用线程池。
_PUSH_TIMEOUT_SECONDS: float = 5.0


def _forward_sync(event_type: str, content: str, shop_pk: int) -> None:
    """同步转发一条系统事件到 backend 内部接口（异常仅记日志，不抛出）。"""
    token = get_settings().internal_service_token
    response = service_client.post_json(
        service_client.backend_base_url(),
        _NOTIFY_EVENTS_PATH,
        {
            "event_type": event_type,
            "content": content,
            "shop_pk": int(shop_pk),
        },
        timeout=_PUSH_TIMEOUT_SECONDS,
        headers={"X-Internal-Token": token},
    )
    if not response.success:
        logger.warning(
            "转发系统事件到 backend 失败：shop_pk=%s, event_type=%s, err=%s",
            shop_pk, event_type, response.message or response.error,
        )


def backend_alert_send_cb(event_type: str, content: str, shop_pk: int) -> None:
    """告警发送回调（``build_alert_notifier.send_cb`` 签名）：转发事件到 backend。

    在事件循环内调用时经线程池异步执行（不阻塞、不等待）；无事件循环时同步直调。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _forward_sync(event_type, content, shop_pk)
        return
    loop.create_task(asyncio.to_thread(_forward_sync, event_type, content, shop_pk))
