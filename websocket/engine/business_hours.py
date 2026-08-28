# -*- coding: utf-8 -*-
"""
文件用途：营业时间判定（websocket.engine.business_hours）
========================================================
本模块为「营业时间判定」的 **websocket 侧兼容入口**：判定纯函数已上移至公共库
``common.utils.business_hours``（websocket 引擎与 scheduler 共用，规范 52：
公共逻辑收敛至 common，scheduler 不 import websocket 包），本模块保留原名与
原导出符号（``TimeLike`` / ``is_within_business_hours``）以便既有调用方与测试
零改动（对 PDD 现有路径零行为变更）。

对应需求：
- 11.2：当前北京时间处于营业区间内 → 执行自动回复处理；
- 11.3：当前北京时间不处于营业区间内 → 不发送自动回复（由上层记日志）；
- 11.4：营业时间未配置 → 默认全天执行自动回复处理（判定恒为 True）。

判定规则细节（跨午夜区间、星期维度、全天营业等）见
``common.utils.business_hours`` 的模块头与函数 docstring。
"""
from __future__ import annotations

# 自公共库 re-export（实现单一来源，两侧判定必然一致）。
from common.utils.business_hours import (  # noqa: F401
    TimeLike,
    _parse_time,
    is_within_business_hours,
)

__all__ = [
    "TimeLike",
    "is_within_business_hours",
]
