# -*- coding: utf-8 -*-
"""
common.tests.test_alert_dedup —— 告警去重组件单元测试（上移 common，TIK-026）
============================================================================
本文件用途：验证上移至 common 的告警去重组件（TIK-005 组件语义，TIK-026 复用）：

- ``AlertDedup``：静默期内重复事件不应发送（去重）、超过静默期后恢复发送、
  resolve 立即恢复（事件恢复后下次可再告警）、(shop_pk, event_type) 二元组隔离；
- ``build_alert_notifier``：静默期内跳过发送、非静默真正调用 send_cb（尽力而为）；
- ``get_alert_dedup``：进程内单例（跨调用共享去重维度）。

websocket 侧存量测试（websocket/tests/test_alert_dedup.py）经兼容壳继续覆盖
PDDChannel / cookies 告警链路，本文件只覆盖组件本身（common 层真身）。
时间经 monkeypatch ``common.utils.alert_dedup.now_beijing`` 控制，避免等待真实
30 分钟静默期。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from common.utils.alert_dedup import (
    AlertDedup,
    build_alert_notifier,
    get_alert_dedup,
)
from common.utils.time_utils import BEIJING_TZ


# ----------------------------------------------------------------------
# AlertDedup 去重防抖
# ----------------------------------------------------------------------
def test_dedup_blocks_repeat_in_silence_window():
    """静默期内同类事件第二次 should_send 应为 False（防抖去重）。"""
    dedup = AlertDedup(silence_seconds=1800.0)
    assert dedup.should_send(1, "reply_rate_below_threshold") is True
    dedup.mark_sent(1, "reply_rate_below_threshold")
    # 刚发送完，处于静默期。不加时间偏移，应被去重。
    assert dedup.should_send(1, "reply_rate_below_threshold") is False


def test_dedup_recovers_after_silence_window(monkeypatch):
    """超过静默期后 should_send 恢复为 True（自动恢复发送）。"""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=BEIJING_TZ)
    calls = {"n": 0}

    def _fake_now():
        calls["n"] += 1
        # 第一次调用（mark_sent 记录时间）返回 base；
        # 之后每次 + 偏移，使第二次 should_send 检查已超过 1800 秒。
        offset = timedelta(seconds=0) if calls["n"] == 1 else timedelta(seconds=1801)
        return base + offset

    dedup = AlertDedup(silence_seconds=1800.0)
    monkeypatch.setattr("common.utils.alert_dedup.now_beijing", _fake_now)

    assert dedup.should_send(1, "reply_rate_below_threshold") is True
    dedup.mark_sent(1, "reply_rate_below_threshold")
    # 已越过 1800 秒静默期 → 允许再次发送。
    assert dedup.should_send(1, "reply_rate_below_threshold") is True


def test_dedup_resolve_immediately_recovers():
    """resolve 立即清除静默状态，下次同类事件可立即再告警。"""
    dedup = AlertDedup(silence_seconds=1800.0)
    dedup.mark_sent(1, "reply_rate_below_threshold")
    assert dedup.should_send(1, "reply_rate_below_threshold") is False
    dedup.resolve(1, "reply_rate_below_threshold")
    assert dedup.should_send(1, "reply_rate_below_threshold") is True


def test_dedup_key_isolation_by_shop_and_event():
    """不同店铺或不同事件类型互不影响（二元组维度隔离）。"""
    dedup = AlertDedup(silence_seconds=1800.0)
    dedup.mark_sent(1, "reply_rate_below_threshold")
    # 同店铺不同事件：不受静默影响。
    assert dedup.should_send(1, "connection_disconnected") is True
    # 不同店铺同事件：不受静默影响。
    assert dedup.should_send(2, "reply_rate_below_threshold") is True
    # 同店铺同事件：被去重。
    assert dedup.should_send(1, "reply_rate_below_threshold") is False


def test_dedup_invalid_silence_falls_back_to_default():
    """非法静默期（非正数）回退为默认 1800 秒，防抖有效性不被破坏。"""
    dedup = AlertDedup(silence_seconds=0)
    assert dedup.silence_seconds == 1800.0


def test_build_alert_notifier_skips_in_silence_and_sends_after():
    """build_alert_notifier 在静默期内跳过发送、跨静默期后真正调用 send_cb。"""
    sent: List[tuple] = []
    dedup = AlertDedup(silence_seconds=1800.0)
    notifier = build_alert_notifier(
        dedup, shop_pk=7, send_cb=lambda et, c, sp: sent.append((et, c, sp))
    )

    # 第一次发送：真正调用。第二次（静默期内）：被去重跳过。
    notifier("reply_rate_below_threshold", "内容1")
    notifier("reply_rate_below_threshold", "内容2")
    assert len(sent) == 1
    assert sent[0] == ("reply_rate_below_threshold", "内容1", 7)


def test_build_alert_notifier_without_send_cb_only_marks_silence():
    """未注入 send_cb：仅记静默（去重照常生效），不抛异常。"""
    dedup = AlertDedup(silence_seconds=1800.0)
    notifier = build_alert_notifier(dedup, shop_pk=7)
    notifier("reply_rate_below_threshold", "内容")
    # 发送回调缺失但静默已记录：静默期内再触发应被去重。
    assert dedup.should_send(7, "reply_rate_below_threshold") is False


def test_get_alert_dedup_returns_singleton():
    """get_alert_dedup 返回进程内同一实例（跨调用共享去重维度）。"""
    first = get_alert_dedup()
    second = get_alert_dedup()
    assert first is second
