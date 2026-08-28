# -*- coding: utf-8 -*-
"""
scheduler.tests.test_tiktok_window —— TikTok 营业时间窗控制任务测试
================================================================
本文件用途：验证 scheduler 服务「tiktok_window」任务（需求 24.x，TIK-015）的
期望态收敛逻辑与执行日志：

- ``plan_window_actions`` 纯函数：期望态 vs 实际态 → connect / disconnect 决策
  （期望连接未连接 → connect；期望断开已连接 → disconnect；一致 → 无动作）；
- ``run_tiktok_window`` 执行体：取 TikTok 店铺（PDD 店铺被过滤）→ 计算期望态 →
  status-batch 查实态 → 逐店收敛（mock service_client，不发起真实 HTTP）；
- 判定一致性：scheduler 侧经 ``common.utils.business_hours`` 判定，与 websocket
  引擎 ``engine.business_hours``（re-export）同款逻辑（固定 now 对齐验证）。

测试方案：pytest + 内存 SQLite（夹具见 conftest.py）。跨服务 HTTP 调用经
monkeypatch 替换，不发起真实网络请求。
"""
from __future__ import annotations

from datetime import datetime, time

from common.db.repository import Repository
from common.models.config_models import BusinessHours
from common.models.shop_models import Shop
from common.models.task_models import TaskRunLog
from common.utils.time_utils import BEIJING_TZ
from common.utils.business_hours import is_within_business_hours as common_within

from tasks import service_client, task_runners
from tasks.constants import RESULT_FAILED, RESULT_SUCCESS, TASK_TIKTOK_WINDOW


# ----------------------------------------------------------------------
# plan_window_actions 纯函数（期望态 vs 实态 → 收敛动作）
# ----------------------------------------------------------------------
def test_plan_connect_when_expected_but_not_connected():
    """期望连接而实际未连接 → connect。"""
    expected = [(1, "TK1", 10, True)]
    actions = task_runners.plan_window_actions(expected, {"TK1": False})
    assert actions == [("connect", 1, "TK1", 10)]


def test_plan_disconnect_when_expected_off_but_connected():
    """期望断开而实际已连接 → disconnect。"""
    expected = [(2, "TK2", 10, False)]
    actions = task_runners.plan_window_actions(expected, {"TK2": True})
    assert actions == [("disconnect", 2, "TK2", 10)]


def test_plan_no_action_when_state_matches():
    """期望态与实态一致（连接 / 断开两情形）→ 无动作（幂等收敛）。"""
    expected = [(1, "TK1", 10, True), (2, "TK2", 10, False)]
    actions = task_runners.plan_window_actions(
        expected, {"TK1": True, "TK2": False}
    )
    assert actions == []


def test_plan_mixed_scenario():
    """混合场景：期望连接未连接 → connect；期望断开已连接 → disconnect。"""
    expected = [
        (1, "TK1", 10, True),
        (2, "TK2", 10, False),
        (3, "TK3", 10, True),
        (4, "TK4", 10, False),
    ]
    # 实态：TK1 未连 / TK2 已连 / TK3 已连（一致）/ TK4 未连（一致）。
    actual = {"TK1": False, "TK2": True, "TK3": True, "TK4": False}
    actions = task_runners.plan_window_actions(expected, actual)
    assert actions == [("connect", 1, "TK1", 10), ("disconnect", 2, "TK2", 10)]


def test_plan_unknown_shop_treated_as_not_connected():
    """实态缺失的店铺按「未连接」处理：期望连接 → connect。"""
    expected = [(1, "TK1", 10, True)]
    actions = task_runners.plan_window_actions(expected, {})
    assert actions == [("connect", 1, "TK1", 10)]


# ----------------------------------------------------------------------
# 判定一致性：scheduler 与 websocket 引擎同款逻辑（common 单一实现）
# ----------------------------------------------------------------------
def test_business_hours_judgement_consistent_with_engine():
    """scheduler 侧经 common 判定：星期 + 时刻语义与引擎一致（固定参考时刻）。"""
    # 固定参考时刻（2024-01-01 为周一 10:00，北京时区）验证星期 + 时刻判定。
    now = datetime(2024, 1, 1, 10, 0, 0, tzinfo=BEIJING_TZ)
    assert common_within("09:00", "18:00", weekdays="1,2,3,4,5", now=now) is True
    assert common_within("09:00", "18:00", weekdays="6,7", now=now) is False
    assert common_within("23:00", "06:00", weekdays="", now=now) is False


# ----------------------------------------------------------------------
# run_tiktok_window 执行体（mock service_client）
# ----------------------------------------------------------------------
def _create_shop(db_session, *, shop_id, platform="tiktok", status=1, proxy_server=None):
    """便捷建店：默认创建启用状态的 TikTok 店铺（可指定店铺级出口代理）。"""
    Repository(Shop, db_session).create(
        shop_id=shop_id,
        shop_name=f"店-{shop_id}",
        owner_user_id=10,
        status=status,
        platform=platform,
        proxy_server=proxy_server,
    )
    db_session.commit()


def _create_business_hours(db_session, *, shop_pk, start="09:00", end="18:00",
                           enabled=True, weekdays=""):
    """便捷创建营业时间配置（时刻为 "HH:MM" 字符串，None 表示不设置）。"""
    def _parse(value):
        if not value:
            return None
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)

    Repository(BusinessHours, db_session).create(
        shop_pk=shop_pk,
        start_time=_parse(start),
        end_time=_parse(end),
        enabled=enabled,
        weekdays=weekdays,
    )
    db_session.commit()


def _log_of(db_session, task_key=TASK_TIKTOK_WINDOW):
    """读取指定任务最近一条执行日志（列表默认时间倒序，最新在前）。"""
    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": task_key})
    assert logs, "应有至少一条执行日志"
    return logs[0]


# 固定参考时刻：2024-01-01（周一）10:00 北京时间——位于 09:00~18:00 营业窗内，
# 且为工作日（weekdays 判定可对齐验证）。测试统一注入，避免依赖当前时刻。
_NOW = datetime(2024, 1, 1, 10, 0, 0, tzinfo=BEIJING_TZ)


def test_run_window_no_tiktok_shops(db_session, monkeypatch):
    """无启用 TikTok 店铺：记成功日志并跳过（不触发任何 HTTP 调用）。"""
    calls = []

    def _fail(*_args, **_kwargs):
        calls.append("called")
        return service_client.CallResult(ok=False, message="不应被调用")

    monkeypatch.setattr(service_client, "query_status_batch", _fail)
    task_runners.run_tiktok_window(now=_NOW)

    assert calls == []
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


def test_run_window_filters_out_pdd_shops(db_session, monkeypatch):
    """仅启用 PDD 店铺：平台过滤，不触发任何收敛动作（PDD 不受影响）。"""
    _create_shop(db_session, shop_id="PDD1", platform="pdd")
    calls = []

    def _fail(*_args, **_kwargs):
        calls.append("called")
        return service_client.CallResult(ok=False, message="不应被调用")

    monkeypatch.setattr(service_client, "query_status_batch", _fail)
    task_runners.run_tiktok_window(now=_NOW)

    assert calls == []
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


def test_run_window_connect_when_window_open(db_session, monkeypatch):
    """营业窗口内期望连接、实际未连接 → connect（platform='tiktok'）。"""
    _create_shop(db_session, shop_id="TK1")
    shop = Repository(Shop, db_session).get_by(shop_id="TK1")
    _create_business_hours(db_session, shop_pk=shop.id)

    calls = []
    monkeypatch.setattr(
        service_client, "query_status_batch",
        lambda shops: service_client.CallResult(
            ok=True, data={"statuses": [{"shop_id": "TK1", "connected": False}]}
        ),
    )
    monkeypatch.setattr(
        service_client, "trigger_connect",
        lambda shop_pk, shop_id, owner_user_id, platform="pdd", proxy_server=None: calls.append(
            (shop_pk, shop_id, owner_user_id, platform, proxy_server)
        ) or service_client.CallResult(ok=True, message="ok"),
    )
    task_runners.run_tiktok_window(now=_NOW)

    # 未配置代理的店铺：connect 透传 proxy_server=None（不走代理）。
    assert calls == [(shop.id, "TK1", 10, "tiktok", None)]
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


def test_run_window_disconnect_outside_window(db_session, monkeypatch):
    """窗口外期望断开、实际已连接 → disconnect。"""
    _create_shop(db_session, shop_id="TK2")
    shop = Repository(Shop, db_session).get_by(shop_id="TK2")
    _create_business_hours(db_session, shop_pk=shop.id, start="23:00", end="06:00")

    calls = []
    monkeypatch.setattr(
        service_client, "query_status_batch",
        lambda shops: service_client.CallResult(
            ok=True, data={"statuses": [{"shop_id": "TK2", "connected": True}]}
        ),
    )
    monkeypatch.setattr(
        service_client, "trigger_disconnect",
        lambda shop_pk, shop_id, owner_user_id: calls.append(
            (shop_pk, shop_id, owner_user_id)
        ) or service_client.CallResult(ok=True, message="ok"),
    )
    task_runners.run_tiktok_window(now=_NOW)

    assert calls == [(shop.id, "TK2", 10)]
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


def test_run_window_no_action_when_state_consistent(db_session, monkeypatch):
    """状态一致：无收敛动作，记成功日志。"""
    _create_shop(db_session, shop_id="TK3")
    shop = Repository(Shop, db_session).get_by(shop_id="TK3")
    _create_business_hours(db_session, shop_pk=shop.id)

    calls = []
    monkeypatch.setattr(
        service_client, "query_status_batch",
        lambda shops: service_client.CallResult(
            ok=True, data={"statuses": [{"shop_id": "TK3", "connected": True}]}
        ),
    )
    monkeypatch.setattr(
        service_client, "trigger_connect",
        lambda *_a, **_kw: calls.append("connect") or service_client.CallResult(ok=True, message="ok"),
    )
    monkeypatch.setattr(
        service_client, "trigger_disconnect",
        lambda *_a, **_kw: calls.append("disconnect") or service_client.CallResult(ok=True, message="ok"),
    )
    task_runners.run_tiktok_window(now=_NOW)

    assert calls == []
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


def test_run_window_status_batch_failure(db_session, monkeypatch):
    """status-batch 查询失败：记失败日志，不做任何收敛动作。"""
    _create_shop(db_session, shop_id="TK4")
    shop = Repository(Shop, db_session).get_by(shop_id="TK4")
    _create_business_hours(db_session, shop_pk=shop.id)

    calls = []
    monkeypatch.setattr(
        service_client, "query_status_batch",
        lambda shops: service_client.CallResult(ok=False, message="目标服务暂不可用"),
    )
    monkeypatch.setattr(
        service_client, "trigger_connect",
        lambda *_a, **_kw: calls.append("connect") or service_client.CallResult(ok=True, message="ok"),
    )
    monkeypatch.setattr(
        service_client, "trigger_disconnect",
        lambda *_a, **_kw: calls.append("disconnect") or service_client.CallResult(ok=True, message="ok"),
    )
    task_runners.run_tiktok_window(now=_NOW)

    assert calls == []
    log = _log_of(db_session)
    assert log.run_result == RESULT_FAILED


def test_run_window_partial_failure_marks_failed(db_session, monkeypatch):
    """部分收敛动作失败：记失败日志，但整体遍历不中断。"""
    _create_shop(db_session, shop_id="TK5")
    _create_shop(db_session, shop_id="TK6")
    shops = Repository(Shop, db_session).list(filters={"platform": "tiktok"})
    for shop in shops:
        _create_business_hours(db_session, shop_pk=shop.id)

    monkeypatch.setattr(
        service_client, "query_status_batch",
        lambda shops: service_client.CallResult(
            ok=True,
            data={"statuses": [
                {"shop_id": "TK5", "connected": False},
                {"shop_id": "TK6", "connected": False},
            ]},
        ),
    )
    connect_calls = []

    def _connect(shop_pk, shop_id, owner_user_id, platform="pdd", proxy_server=None):
        connect_calls.append((shop_id, platform))
        # 首个成功、第二个失败：验证失败不中断遍历。
        return service_client.CallResult(
            ok=(shop_id == "TK5"), message="ok" if shop_id == "TK5" else "连接失败"
        )

    monkeypatch.setattr(service_client, "trigger_connect", _connect)
    task_runners.run_tiktok_window(now=_NOW)

    # 两个店铺都被遍历触发 connect（顺序无业务意义，以集合断言）。
    assert set(connect_calls) == {("TK5", "tiktok"), ("TK6", "tiktok")}
    log = _log_of(db_session)
    assert log.run_result == RESULT_FAILED


def test_run_window_without_business_hours_connects(db_session, monkeypatch):
    """未配置营业时间：按全天营业，期望连接（与引擎需求 11.4 语义一致）。"""
    _create_shop(db_session, shop_id="TK7")
    shop = Repository(Shop, db_session).get_by(shop_id="TK7")

    calls = []
    monkeypatch.setattr(
        service_client, "query_status_batch",
        lambda shops: service_client.CallResult(
            ok=True, data={"statuses": [{"shop_id": "TK7", "connected": False}]}
        ),
    )
    monkeypatch.setattr(
        service_client, "trigger_connect",
        lambda shop_pk, shop_id, owner_user_id, platform="pdd", proxy_server=None: calls.append(
            (shop_pk, shop_id, owner_user_id, platform, proxy_server)
        ) or service_client.CallResult(ok=True, message="ok"),
    )
    task_runners.run_tiktok_window(now=_NOW)

    assert calls == [(shop.id, "TK7", 10, "tiktok", None)]
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


def test_run_window_connect_passes_shop_proxy(db_session, monkeypatch):
    """店铺配置了出口代理：connect 将 proxy_server 透传给 websocket（Phase 2 前置）。"""
    _create_shop(db_session, shop_id="TK8", proxy_server="http://127.0.0.1:7890")
    shop = Repository(Shop, db_session).get_by(shop_id="TK8")
    _create_business_hours(db_session, shop_pk=shop.id)

    calls = []
    monkeypatch.setattr(
        service_client, "query_status_batch",
        lambda shops: service_client.CallResult(
            ok=True, data={"statuses": [{"shop_id": "TK8", "connected": False}]}
        ),
    )
    monkeypatch.setattr(
        service_client, "trigger_connect",
        lambda shop_pk, shop_id, owner_user_id, platform="pdd", proxy_server=None: calls.append(
            (shop_pk, shop_id, owner_user_id, platform, proxy_server)
        ) or service_client.CallResult(ok=True, message="ok"),
    )
    task_runners.run_tiktok_window(now=_NOW)

    assert calls == [(shop.id, "TK8", 10, "tiktok", "http://127.0.0.1:7890")]
    log = _log_of(db_session)
    assert log.run_result == RESULT_SUCCESS


__all__ = [
    "test_plan_connect_when_expected_but_not_connected",
    "test_plan_disconnect_when_expected_off_but_connected",
    "test_plan_no_action_when_state_matches",
    "test_plan_mixed_scenario",
    "test_plan_unknown_shop_treated_as_not_connected",
    "test_business_hours_judgement_consistent_with_engine",
    "test_run_window_no_tiktok_shops",
    "test_run_window_filters_out_pdd_shops",
    "test_run_window_connect_when_window_open",
    "test_run_window_disconnect_outside_window",
    "test_run_window_no_action_when_state_consistent",
    "test_run_window_status_batch_failure",
    "test_run_window_partial_failure_marks_failed",
    "test_run_window_without_business_hours_connects",
    "test_run_window_connect_passes_shop_proxy",
]
