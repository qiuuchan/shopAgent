# -*- coding: utf-8 -*-
"""
engine.alert_dedup —— 告警去重与防抖（兼容壳，实现已上移 common）
===================================================================
本文件用途：TIK-005 的告警去重组件**已上移** ``common.utils.alert_dedup``
（TIK-026 因 scheduler 侧「回复率跌破阈值」告警需复用同一去重语义而上移，跨服务
禁止导入 websocket 包），本文件保留为**兼容壳**：re-export common 版全部符号，
保证 websocket 侧存量调用点（connection_manager / cookies / 测试）零改动。

新增调用方请直接 ``from common.utils.alert_dedup import ...``，不要依赖本壳。

设计说明详见 ``common.utils.alert_dedup`` 模块 docstring。
"""
from __future__ import annotations

from common.utils.alert_dedup import (  # noqa: F401 —— 兼容壳 re-export
    AlertDedup,
    AlertSendCb,
    DEFAULT_SILENCE_SECONDS,
    build_alert_notifier,
    get_alert_dedup,
)

__all__ = [
    "AlertDedup",
    "DEFAULT_SILENCE_SECONDS",
    "AlertSendCb",
    "build_alert_notifier",
    "get_alert_dedup",
]
