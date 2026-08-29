# -*- coding: utf-8 -*-
"""
tools.tests.test_login_expiry —— 登录态过期周期观测测试（纯函数 + SQLite 集成）
==============================================================================
覆盖 tools/tiktok_acceptance/login_expiry.py：
- ``suggest_probe_interval``：无样本取保守值、按最短间隔除以除数、上下界夹取、整分钟进位；
- ``analyse_expiry``：乱序输入排序去重、间隔计算、样本不足不出结论、样本充足出中位/最短/最长；
- ``probe_coverage``：应有次数推算、覆盖率上限 1.0、不完整判定；
- ``count_probe_alerts``：关键词计数与空 message 容错；
- ``build_report`` / ``query_*``：店铺隔离、时间窗口过滤、事件类型区分、CLI 冒烟。
"""
import datetime
import json
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import common.models.log_models  # noqa: F401  导入以注册全部表
import common.models.task_models  # noqa: F401  导入以注册全部表
from common.models.base import Base
from common.models.log_models import NotifyRecord
from common.models.task_models import TaskRunLog
from tools.tiktok_acceptance import login_expiry
from tools.tiktok_acceptance.login_expiry import (
    DEFAULT_SUGGEST_SECONDS,
    MAX_PROBE_INTERVAL_SECONDS,
    MIN_PROBE_INTERVAL_SECONDS,
    analyse_expiry,
    build_report,
    count_probe_alerts,
    probe_coverage,
    query_cookie_refresh_runs,
    query_notify_events,
    suggest_probe_interval,
)


def _dt(day, hour=0, minute=0, second=0):
    return datetime.datetime(2026, 9, day, hour, minute, second)


@pytest.fixture
def db_session():
    """内存 SQLite 会话，建全量表。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


# ----------------------------------------------------------------------
# 纯函数：建议巡检周期
# ----------------------------------------------------------------------
def test_suggest_probe_interval_no_sample_returns_default():
    """无间隔样本时返回保守建议值，不贸然放宽巡检。"""
    assert suggest_probe_interval([]) == DEFAULT_SUGGEST_SECONDS


def test_suggest_probe_interval_divides_shortest_and_rounds_up_to_minute():
    """建议周期 = 最短间隔 / 4，向上取整到整分钟。"""
    # 7200 / 4 = 1800，恰为整分钟
    assert suggest_probe_interval([7200.0]) == 1800
    # 7000 / 4 = 1750 → 进位到 1800
    assert suggest_probe_interval([7000.0, 9000.0]) == 1800


def test_suggest_probe_interval_clamped_by_bounds():
    """极短间隔夹到下界 600 秒，极长间隔夹到上界 6 小时。"""
    assert suggest_probe_interval([60.0]) == MIN_PROBE_INTERVAL_SECONDS
    # 30 天 / 4 = 648000 秒，远超上界
    assert suggest_probe_interval([30 * 86400.0]) == MAX_PROBE_INTERVAL_SECONDS


# ----------------------------------------------------------------------
# 纯函数：过期周期分析
# ----------------------------------------------------------------------
def test_analyse_expiry_empty_gives_no_sample_conclusion():
    """无事件：样本为空，结论提示需结合覆盖率确认是否漏报。"""
    result = analyse_expiry([])
    assert result.event_count == 0
    assert result.intervals_seconds == []
    assert result.min_seconds is None
    assert result.sample_sufficient is False
    assert "无 login_expired 事件" in result.conclusion


def test_analyse_expiry_sorts_and_dedups_input():
    """乱序输入按升序处理，重复时间点去重（同一次告警的重复落库不计为两次）。"""
    result = analyse_expiry([_dt(3), _dt(1), _dt(1), _dt(2)])
    assert result.event_count == 3
    assert result.intervals_seconds == [86400.0, 86400.0]
    assert result.sample_sufficient is True


def test_analyse_expiry_single_interval_not_sufficient():
    """仅 2 次事件只有 1 个间隔样本，不出周期结论。"""
    result = analyse_expiry([_dt(1), _dt(4)])
    assert result.event_count == 2
    assert len(result.intervals_seconds) == 1
    assert result.sample_sufficient is False
    assert "不足以判定周期性" in result.conclusion


def test_analyse_expiry_reports_min_median_max():
    """3 次事件给出 2 个间隔，输出最短/中位/最长与建议周期。"""
    # 间隔：1 天 与 3 天
    result = analyse_expiry([_dt(1), _dt(2), _dt(5)])
    assert result.min_seconds == 86400.0
    assert result.max_seconds == 3 * 86400.0
    # 两个样本的中位为二者均值
    assert result.median_seconds == 2 * 86400.0
    assert result.sample_sufficient is True
    assert result.suggested_interval_seconds == MAX_PROBE_INTERVAL_SECONDS


def test_analyse_expiry_ignores_none_times():
    """日志时间为 None 的记录不参与统计（不抛异常）。"""
    result = analyse_expiry([_dt(1), None, _dt(2)])
    assert result.event_count == 2


# ----------------------------------------------------------------------
# 纯函数：巡检覆盖率与异常打点
# ----------------------------------------------------------------------
def test_probe_coverage_ratio_and_completeness():
    """应有次数按窗口跨度除以周期推算，覆盖率达标即判定窗口完整。"""
    since, until = _dt(1), _dt(2)  # 跨度 1 天，周期 600 秒 → 应有 144 次
    coverage = probe_coverage([_dt(1, 1), _dt(1, 2)], since, until, 600)
    assert coverage["expected_runs"] == 144
    assert coverage["actual_runs"] == 2
    assert coverage["complete"] is False

    full = probe_coverage([_dt(1)] * 144, since, until, 600)
    assert full["coverage_ratio"] == 1.0
    assert full["complete"] is True


def test_probe_coverage_zero_span_or_interval_no_division_error():
    """窗口跨度为 0 或周期为 0 时不抛除零错误，覆盖率为 0。"""
    coverage = probe_coverage([], _dt(1), _dt(1), 600)
    assert coverage["expected_runs"] == 0
    assert coverage["coverage_ratio"] == 0.0
    assert probe_coverage([_dt(1)], _dt(1), _dt(2), 0)["expected_runs"] == 0


def test_count_probe_alerts_tolerates_none_message():
    """只统计含「巡检异常」的日志，None / 普通成功日志不计数。"""
    assert count_probe_alerts(["Cookie 刷新完成：成功 1 个，失败 0 个", None]) == 0
    assert count_probe_alerts(["店铺[S1] 巡检异常：IM 会话过期弹窗"]) == 1


# ----------------------------------------------------------------------
# 数据库访问与报告
# ----------------------------------------------------------------------
def _event(shop_pk, event_type, dt, send_result="成功"):
    return NotifyRecord(
        shop_pk=shop_pk,
        event_type=event_type,
        content="登录态失效",
        send_result=send_result,
        log_time=dt,
    )


def _run(dt, message):
    return TaskRunLog(
        task_key="cookie_refresh", run_result="success", message=message, log_time=dt
    )


def test_query_notify_events_filters_shop_and_window(db_session):
    """查询按店铺 + 窗口 + 事件类型过滤，其他店铺/窗口外/其他事件不混入。"""
    db_session.add_all(
        [
            _event(1, "login_expired", _dt(2)),
            _event(1, "login_expired", _dt(20)),  # 窗口外
            _event(2, "login_expired", _dt(2)),  # 其他店铺
            _event(1, "connection_disconnected", _dt(2)),  # 其他事件
        ]
    )
    db_session.commit()
    rows = query_notify_events(db_session, 1, _dt(1), _dt(10), "login_expired")
    assert len(rows) == 1
    assert rows[0].log_time == _dt(2)


def test_query_cookie_refresh_runs_filters_task_key(db_session):
    """执行日志只取 cookie_refresh 任务键。"""
    db_session.add_all(
        [
            _run(_dt(2), "Cookie 刷新完成：成功 1 个，失败 0 个"),
            TaskRunLog(
                task_key="product_sync",
                run_result="success",
                message="商品同步",
                log_time=_dt(2),
            ),
        ]
    )
    db_session.commit()
    assert len(query_cookie_refresh_runs(db_session, _dt(1), _dt(10))) == 1


def test_build_report_separates_expired_and_disconnect(db_session):
    """报告区分 login_expired（样本）与 connection_disconnected（仅参考计数）。"""
    db_session.add_all(
        [
            _event(3, "login_expired", _dt(1)),
            _event(3, "login_expired", _dt(3)),
            _event(3, "connection_disconnected", _dt(2)),
            _run(_dt(1, 0, 10), "Cookie 刷新完成：成功 1 个，失败 0 个；店铺[S3] 巡检异常：IM 会话过期弹窗"),
        ]
    )
    db_session.commit()

    report = build_report(db_session, 3, _dt(1), _dt(10), probe_interval_seconds=86400)
    assert report["expiry"]["event_count"] == 2
    assert report["disconnect_event_count"] == 1
    assert report["probe_alert_runs"] == 1
    assert report["probe_coverage"]["actual_runs"] == 1
    assert len(report["events"]) == 2
    assert report["events"][0]["time"] == "2026-09-01 00:00:00"


def test_build_report_other_shop_events_excluded(db_session):
    """其他店铺的过期事件不进入本报告。"""
    db_session.add_all(
        [_event(3, "login_expired", _dt(1)), _event(9, "login_expired", _dt(2))]
    )
    db_session.commit()
    report = build_report(db_session, 3, _dt(1), _dt(10))
    assert report["expiry"]["event_count"] == 1


# ----------------------------------------------------------------------
# CLI 冒烟
# ----------------------------------------------------------------------
def test_cli_json_output_on_sqlite_file(tmp_path):
    """CLI 指向 sqlite 文件库可正常出报告（--json 输出可解析）。"""
    db_file = tmp_path / "obs.db"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as session:
        session.add_all(
            [
                _event(3, "login_expired", _dt(1)),
                _event(3, "login_expired", _dt(2)),
                _event(3, "login_expired", _dt(5)),
            ]
        )
        session.commit()
    engine.dispose()

    repo_root = _repo_root()
    proc = subprocess.run(
        [
            sys.executable,
            "tools/tiktok_acceptance/login_expiry.py",
            "--shop", "3",
            "--since", "2026-09-01T00:00:00",
            "--until", "2026-09-10T00:00:00",
            "--json",
            "--db", f"sqlite:///{db_file}",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["expiry"]["event_count"] == 3
    assert payload["expiry"]["sample_sufficient"] is True


def _repo_root():
    """返回仓库根目录（tools/tests 向上两级）。"""
    import os

    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_render_text_mentions_pdd_coupling_warning():
    """文本报告必须提示 PDD / TikTok 共用任务的调参约束（防止误调大周期）。"""
    import os

    db_file = os.path.join(_repo_root(), ".tmp_login_expiry_smoke.db")
    if os.path.exists(db_file):
        os.remove(db_file)
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as session:
        session.add(_event(3, "login_expired", _dt(1)))
        session.commit()
    engine.dispose()

    proc = subprocess.run(
        [
            sys.executable,
            "tools/tiktok_acceptance/login_expiry.py",
            "--shop", "3",
            "--since", "2026-09-01T00:00:00",
            "--until", "2026-09-10T00:00:00",
            "--db", f"sqlite:///{db_file}",
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "PDD" in proc.stdout
    assert "共用任务" in proc.stdout
    os.remove(db_file)


def test_module_exports_constants():
    """关键常量可被外部引用（README 与运维脚本依赖）。"""
    assert login_expiry.EVENT_LOGIN_EXPIRED == "login_expired"
    assert login_expiry.COOKIE_REFRESH_TASK_KEY == "cookie_refresh"
    assert login_expiry.PROBE_ALERT_KEYWORD == "巡检异常"
