# -*- coding: utf-8 -*-
"""
scheduler.tests.test_reply_rate_check —— 回复率巡检任务测试（TIK-026）
=====================================================================
本文件用途：验证 scheduler 服务「reply_rate_check」任务的判定与告警行为：

- ``plan_reply_rate_actions`` 纯函数：回复率跌破阈值 → alert；回到阈值之上 →
  resolve；无数据 → 不动作；
- ``_aggregate_reply_rate`` 聚合：窗口内消息按「店铺 + 客户」切周期，回复率与
  ``common.utils.latency.reply_rate`` 口径一致（看板 / 告警同一实现）；
- ``run_reply_rate_check`` 执行体：取启用 TikTok 店铺（PDD 过滤）→ 计算回复率 →
  去重（AlertDedup 静默期）→ 告警 / 恢复（mock service_client，不发起真实 HTTP）。

测试方案：pytest + 内存 SQLite（夹具见 conftest.py）。跨服务 HTTP 调用经
monkeypatch 替换，不发起真实网络请求。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete

from common.db.repository import Repository
from common.models.log_models import ChatMessage
from common.models.shop_models import Shop
from common.models.task_models import TaskRunLog
from common.utils.alert_dedup import AlertDedup
from common.utils.latency import reply_rate
from common.utils.time_utils import BEIJING_TZ

from tasks import reply_rate_check, service_client
from tasks.constants import (
    RESULT_FAILED,
    RESULT_SUCCESS,
    TASK_REPLY_RATE_CHECK,
)
from tasks.reply_rate_check import EVENT_REPLY_RATE_BELOW_THRESHOLD

# 固定参考时刻：2024-01-01 10:00 北京时间（与 test_tiktok_window 同款注入）。
# 巡检窗口 = [2023-12-31 10:00, 2024-01-01 10:00)。
_NOW = datetime(2024, 1, 1, 10, 0, 0, tzinfo=BEIJING_TZ)
_WINDOW_START = _NOW - timedelta(hours=24)


def _create_shop(db_session, *, shop_id, platform="tiktok", status=1):
    """便捷建店：默认创建启用状态的 TikTok 店铺。"""
    Repository(Shop, db_session).create(
        shop_id=shop_id,
        shop_name=f"店-{shop_id}",
        owner_user_id=10,
        status=status,
        platform=platform,
    )
    db_session.commit()


def _create_message(db_session, *, shop_pk, customer_uid, direction, msg_time):
    """便捷创建一条窗口内聊天消息（msg_time 为北京时间带时区 datetime）。"""
    Repository(ChatMessage, db_session).create(
        shop_pk=shop_pk,
        customer_uid=customer_uid,
        direction=direction,
        msg_time=msg_time,
    )
    db_session.commit()


def _log_of(db_session):
    """读取 reply_rate_check 任务最近一条执行日志（最新在前）。"""
    logs = Repository(TaskRunLog, db_session).list(
        filters={"task_key": TASK_REPLY_RATE_CHECK}
    )
    assert logs, "应有至少一条执行日志"
    return logs[0]


def _fresh_dedup(monkeypatch):
    """给每个执行体测试注入独立的去重器，避免模块级单例跨测试污染。"""
    dedup = AlertDedup()
    monkeypatch.setattr(reply_rate_check, "_reply_rate_dedup", dedup)
    return dedup


# ----------------------------------------------------------------------
# plan_reply_rate_actions 纯函数
# ----------------------------------------------------------------------
def test_plan_alert_when_below_threshold():
    """回复率跌破阈值 → alert 候选。"""
    rates = {1: ("TK1", 0.70)}
    alert, resolve, no_data = reply_rate_check.plan_reply_rate_actions(rates)
    assert alert == [(1, "TK1")]
    assert resolve == []
    assert no_data == []


def test_plan_resolve_when_at_or_above_threshold():
    """回复率回到阈值之上（含等于）→ resolve 候选（解除静默）。"""
    rates = {1: ("TK1", 0.85), 2: ("TK2", 1.0)}
    alert, resolve, no_data = reply_rate_check.plan_reply_rate_actions(rates)
    assert alert == []
    assert sorted(resolve) == [(1, "TK1"), (2, "TK2")]
    assert no_data == []


def test_plan_no_data_ignored():
    """无任何回复周期（rate=None）→ 不告警也不 resolve（无数据不干扰状态）。"""
    rates = {1: ("TK1", None)}
    alert, resolve, no_data = reply_rate_check.plan_reply_rate_actions(rates)
    assert alert == []
    assert resolve == []
    assert no_data == [(1, "TK1")]


def test_plan_mixed_scenario():
    """混合场景：跌破 / 恢复 / 无数据三态并存时各自归类。"""
    rates = {
        1: ("TK1", 0.40),
        2: ("TK2", 0.90),
        3: ("TK3", None),
        4: ("TK4", 0.80),
    }
    alert, resolve, no_data = reply_rate_check.plan_reply_rate_actions(rates)
    assert sorted(alert) == [(1, "TK1"), (4, "TK4")]
    assert resolve == [(2, "TK2")]
    assert no_data == [(3, "TK3")]


def test_plan_uses_injected_threshold():
    """注入自定义阈值生效（默认 0.85 之外边界可调）。"""
    rates = {1: ("TK1", 0.90)}
    alert, resolve, _ = reply_rate_check.plan_reply_rate_actions(rates, threshold=0.95)
    assert alert == [(1, "TK1")]
    assert resolve == []


# ----------------------------------------------------------------------
# _aggregate_reply_rate 聚合口径（与 common.utils.latency 同一实现）
# ----------------------------------------------------------------------
def test_aggregate_reply_rate_matches_common(db_session):
    """聚合结果与直接调用 reply_rate 计算完全一致（口径唯一来源）。"""
    shop = Repository(Shop, db_session).create(
        shop_id="TK1", shop_name="店-TK1", owner_user_id=10, platform="tiktok"
    )
    db_session.commit()
    # 会话 A：快回（未超时）→ 达标；会话 B：无回复（待回复）→ 未达标。
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="A",
        direction="in", msg_time=_WINDOW_START + timedelta(hours=1),
    )
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="A",
        direction="out", msg_time=_WINDOW_START + timedelta(hours=1, minutes=1),
    )
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="B",
        direction="in", msg_time=_WINDOW_START + timedelta(hours=2),
    )

    rates = reply_rate_check._aggregate_reply_rate(
        reply_rate_check._query_window_messages(
            db_session, [shop.id], _WINDOW_START, _NOW
        )
    )
    expected = reply_rate(responded_cycles=1, over_threshold_count=0, pending_cycles=1)
    assert rates[shop.id] == expected
    assert rates[shop.id] == 0.5


# ----------------------------------------------------------------------
# run_reply_rate_check 执行体（mock service_client）
# ----------------------------------------------------------------------
def test_run_no_tiktok_shops(db_session, monkeypatch):
    """无启用 TikTok 店铺：记成功日志并跳过（不触发任何 HTTP 调用）。"""
    _fresh_dedup(monkeypatch)
    calls = []

    def _fail(*_args, **_kwargs):
        calls.append("called")
        return service_client.CallResult(ok=False, message="不应被调用")

    monkeypatch.setattr(service_client, "trigger_notify_event", _fail)
    reply_rate_check.run_reply_rate_check(now=_NOW)

    assert calls == []
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


def test_run_filters_out_pdd_shops(db_session, monkeypatch):
    """仅启用 PDD 店铺：平台过滤，不触发任何告警（PDD 不受影响）。"""
    _fresh_dedup(monkeypatch)
    _create_shop(db_session, shop_id="PDD1", platform="pdd")
    calls = []

    def _fail(*_args, **_kwargs):
        calls.append("called")
        return service_client.CallResult(ok=False, message="不应被调用")

    monkeypatch.setattr(service_client, "trigger_notify_event", _fail)
    reply_rate_check.run_reply_rate_check(now=_NOW)

    assert calls == []
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


def test_run_alert_when_rate_below_threshold(db_session, monkeypatch):
    """回复率跌破阈值 → 经 notify 链路告警一次，执行日志 success。"""
    _fresh_dedup(monkeypatch)
    _create_shop(db_session, shop_id="TK1")
    shop = Repository(Shop, db_session).get_by(shop_id="TK1")
    # 会话 A：超时回复（600 秒 > 300 秒）→ 回复率 0.0，跌破阈值。
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="A",
        direction="in", msg_time=_WINDOW_START + timedelta(hours=1),
    )
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="A",
        direction="out", msg_time=_WINDOW_START + timedelta(hours=1, minutes=10),
    )

    calls = []
    monkeypatch.setattr(
        service_client, "trigger_notify_event",
        lambda event_type, content, shop_pk: calls.append(
            (event_type, content, shop_pk)
        ) or service_client.CallResult(ok=True, message="ok", data={"total": 1}),
    )
    reply_rate_check.run_reply_rate_check(now=_NOW)

    assert len(calls) == 1
    event_type, content, shop_pk = calls[0]
    assert event_type == EVENT_REPLY_RATE_BELOW_THRESHOLD
    assert shop_pk == shop.id
    # 文案含店铺与实测回复率（0.00%），且为中文通知内容。
    assert "店-TK1" in content and "0.00%" in content and "85%" in content

    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS
    assert "已推送 1" in (log.message or "")


def test_run_dedup_within_silence_window(db_session, monkeypatch):
    """持续跌破但处于静默期内：同一静默窗口只告警一次（防刷屏）。"""
    dedup = _fresh_dedup(monkeypatch)
    _create_shop(db_session, shop_id="TK1")
    shop = Repository(Shop, db_session).get_by(shop_id="TK1")
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="A",
        direction="in", msg_time=_WINDOW_START + timedelta(hours=1),
    )

    calls = []
    monkeypatch.setattr(
        service_client, "trigger_notify_event",
        lambda *a, **kw: calls.append(a) or service_client.CallResult(ok=True),
    )
    # 两次连续巡检（窗口内均跌破）：第一次发送，第二次被静默去重。
    reply_rate_check.run_reply_rate_check(now=_NOW)
    reply_rate_check.run_reply_rate_check(now=_NOW)

    assert len(calls) == 1
    # 第二次巡检日志应记录「静默去重 1」。
    log = _log_of(db_session)
    assert "静默去重 1" in (log.message or "")
    assert log.run_result == RESULT_SUCCESS
    # 去重器状态：该键已记静默。
    assert not dedup.should_send(shop.id, EVENT_REPLY_RATE_BELOW_THRESHOLD)


def test_run_resolve_then_re_alert(db_session, monkeypatch):
    """跌破告警 → 回复率恢复（resolve 解除静默）→ 再次跌破可立即再告警。"""
    dedup = _fresh_dedup(monkeypatch)
    _create_shop(db_session, shop_id="TK1")
    shop = Repository(Shop, db_session).get_by(shop_id="TK1")
    # 会话 A：无回复（待回复）→ 回复率 0.0。
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="A",
        direction="in", msg_time=_WINDOW_START + timedelta(hours=1),
    )

    calls = []
    monkeypatch.setattr(
        service_client, "trigger_notify_event",
        lambda *a, **kw: calls.append(a) or service_client.CallResult(ok=True),
    )
    # 第一次：跌破 → 告警并记静默。
    reply_rate_check.run_reply_rate_check(now=_NOW)
    assert len(calls) == 1

    # 移除待回复消息（模拟人工处理完）：窗口内无周期 → 无数据（不动静默）。
    db_session.execute(sa_delete(ChatMessage).where(ChatMessage.shop_pk == shop.id))
    db_session.commit()
    reply_rate_check.run_reply_rate_check(now=_NOW)
    assert len(calls) == 1
    # 无数据不 resolve：静默仍保留（防无数据误清告警状态）。

    # 补一条「已回复未超时」消息：回复率 1.0 → resolve 解除静默。
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="B",
        direction="in", msg_time=_WINDOW_START + timedelta(hours=2),
    )
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="B",
        direction="out", msg_time=_WINDOW_START + timedelta(hours=2, minutes=1),
    )
    reply_rate_check.run_reply_rate_check(now=_NOW)
    assert len(calls) == 1
    assert dedup.should_send(shop.id, EVENT_REPLY_RATE_BELOW_THRESHOLD)

    # 再次跌破（新会话待回复）→ 立即再告警（不受上次静默影响）。
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="C",
        direction="in", msg_time=_WINDOW_START + timedelta(hours=3),
    )
    reply_rate_check.run_reply_rate_check(now=_NOW)
    assert len(calls) == 2


def test_run_no_data_no_action(db_session, monkeypatch):
    """窗口内无任何消息（无数据）：不告警也不 resolve（无误报）。"""
    _fresh_dedup(monkeypatch)
    _create_shop(db_session, shop_id="TK1")
    calls = []

    def _fail(*_args, **_kwargs):
        calls.append("called")
        return service_client.CallResult(ok=False, message="不应被调用")

    monkeypatch.setattr(service_client, "trigger_notify_event", _fail)
    reply_rate_check.run_reply_rate_check(now=_NOW)

    assert calls == []
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS
    assert "无数据 1" in (log.message or "")


def test_run_alert_push_failed_marks_failed(db_session, monkeypatch):
    """告警推送失败：记失败执行日志（巡检完成但推送异常，便于运维感知）。"""
    _fresh_dedup(monkeypatch)
    _create_shop(db_session, shop_id="TK1")
    shop = Repository(Shop, db_session).get_by(shop_id="TK1")
    _create_message(
        db_session, shop_pk=shop.id, customer_uid="A",
        direction="in", msg_time=_WINDOW_START + timedelta(hours=1),
    )

    monkeypatch.setattr(
        service_client, "trigger_notify_event",
        lambda *a, **kw: service_client.CallResult(ok=False, message="backend 不可达"),
    )
    reply_rate_check.run_reply_rate_check(now=_NOW)

    log = _log_of(db_session)
    assert log.run_result == RESULT_FAILED
    assert "推送失败" in (log.message or "")
