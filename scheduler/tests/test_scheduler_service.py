# -*- coding: utf-8 -*-
"""
scheduler.tests.test_scheduler_service —— 定时任务调度与执行日志单元测试
======================================================================
本文件用途：对 scheduler 服务任务 15.1 的核心逻辑进行单元测试，覆盖需求 21.2 /
21.4 的关键验收场景：

- 执行日志写入（task_run_log）：成功 / 失败均落库，并刷新 scheduled_task 的
  last_run_at（需求 21.2）；
- 文件日志清理：仅删除磁盘上过期日志文件，**绝不删除数据库业务日志表数据**
  （规范 11 / 需求 19.5 / 21.4）；
- 保留天数解析：读取 basic.log_retention_days，越界 / 缺失回退默认；
- 调度器触发器构造：cron / interval 合法构造、非法配置跳过；
- 任务执行体：跨服务调用成功 / 失败下的执行日志结果。

测试方案：pytest + 内存 SQLite（夹具见 conftest.py）。跨服务 HTTP 调用经
monkeypatch 替换，不发起真实网络请求。
"""
from __future__ import annotations

import os
from datetime import timedelta

from sqlalchemy.orm import Session

from common.db.repository import Repository
from common.models.setting_models import SysSetting
from common.models.shop_models import Shop
from common.models.task_models import ScheduledTask, TaskRunLog
from common.models.log_models import SystemLog
from common.utils.time_utils import now_beijing_naive

from tasks import log_cleanup, service_client, task_run_log, task_runners
from tasks.constants import (
    RESULT_FAILED,
    RESULT_SUCCESS,
    SCHEDULE_TYPE_CRON,
    SCHEDULE_TYPE_INTERVAL,
    SUPPORTED_TASK_KEYS,
    TASK_COOKIE_REFRESH,
    TASK_LOG_FILE_CLEANUP,
    TASK_PRODUCT_SYNC,
    TASK_REPLY_RATE_CHECK,
)
from tasks.scheduler_service import SchedulerService


# ----------------------------------------------------------------------
# 执行日志写入（task_run_log，需求 21.2）
# ----------------------------------------------------------------------
def test_write_success_log_persists_and_updates_last_run(db_session: Session):
    """写成功日志：追加 task_run_log 且刷新对应任务 last_run_at（需求 21.2）。"""
    # 预置一条任务配置。
    Repository(ScheduledTask, db_session).create(
        task_key=TASK_COOKIE_REFRESH,
        task_name="Cookie 刷新",
        schedule_type=SCHEDULE_TYPE_INTERVAL,
        schedule_config="3600",
        enabled=True,
    )
    db_session.commit()

    task_run_log.write_success(TASK_COOKIE_REFRESH, "成功 2 个")

    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_COOKIE_REFRESH})
    assert len(logs) == 1
    assert logs[0].run_result == RESULT_SUCCESS
    assert logs[0].message == "成功 2 个"
    assert logs[0].log_time is not None

    task = Repository(ScheduledTask, db_session).get_by(task_key=TASK_COOKIE_REFRESH)
    assert task.last_run_at is not None


def test_write_failed_log_persists(db_session: Session):
    """写失败日志：以 failed 结果落库（需求 21.2）。"""
    task_run_log.write_failed(TASK_PRODUCT_SYNC, "目标服务暂不可用")

    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_PRODUCT_SYNC})
    assert len(logs) == 1
    assert logs[0].run_result == RESULT_FAILED
    assert logs[0].message == "目标服务暂不可用"


# ----------------------------------------------------------------------
# 文件日志清理：仅清磁盘文件，禁止物理删除数据库业务日志（需求 21.4 / 19.5）
# ----------------------------------------------------------------------
def test_cleanup_only_removes_expired_disk_files(tmp_path, monkeypatch):
    """按保留天数仅删除过期磁盘日志文件，保留期内文件不删（需求 21.4）。"""
    monkeypatch.setenv("LOG_FILE_DIR", str(tmp_path))

    old_file = tmp_path / "app.log.2020-01-01"
    new_file = tmp_path / "app.log"
    other_file = tmp_path / "readme.txt"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")
    other_file.write_text("keep", encoding="utf-8")

    # 将旧日志文件 mtime 调到 10 天前，新日志文件保持现在。
    old_ts = (now_beijing_naive() - timedelta(days=10)).timestamp()
    os.utime(old_file, (old_ts, old_ts))

    removed = log_cleanup.cleanup_log_files(retention_days=7)

    assert str(old_file) in removed
    assert not old_file.exists()       # 过期日志文件被删
    assert new_file.exists()           # 保留期内日志文件保留
    assert other_file.exists()         # 非日志文件不动


def test_cleanup_never_touches_db_business_logs(db_session: Session, tmp_path, monkeypatch):
    """文件日志清理绝不删除数据库业务日志表数据（规范 11 / 需求 19.5）。"""
    monkeypatch.setenv("LOG_FILE_DIR", str(tmp_path))
    # 预置数据库业务日志（系统日志 + 任务执行日志）。
    Repository(SystemLog, db_session).create(
        level="info", module="test", content="历史系统日志", log_time=now_beijing_naive()
    )
    Repository(TaskRunLog, db_session).create(
        task_key=TASK_LOG_FILE_CLEANUP, run_result=RESULT_SUCCESS, message="x",
        log_time=now_beijing_naive(),
    )
    db_session.commit()

    # 预置一个过期磁盘日志文件。
    old_file = tmp_path / "x.log"
    old_file.write_text("old", encoding="utf-8")
    old_ts = (now_beijing_naive() - timedelta(days=400)).timestamp()
    os.utime(old_file, (old_ts, old_ts))

    log_cleanup.cleanup_log_files(retention_days=30)

    # 数据库业务日志数量不变（未被物理删除）。
    assert Repository(SystemLog, db_session).count() == 1
    assert Repository(TaskRunLog, db_session).count() == 1
    # 磁盘过期日志被清理。
    assert not old_file.exists()


def test_cleanup_missing_dir_returns_empty(monkeypatch):
    """日志目录不存在时安全跳过，返回空列表。"""
    monkeypatch.setenv("LOG_FILE_DIR", "f:/__not_exists_dir__/__nope__")
    assert log_cleanup.cleanup_log_files(retention_days=7) == []


# ----------------------------------------------------------------------
# 保留天数解析（需求 21.6）
# ----------------------------------------------------------------------
def test_resolve_retention_reads_setting(db_session: Session):
    """读取 basic.log_retention_days 合法值。"""
    Repository(SysSetting, db_session).create(
        setting_key="basic",
        scope="global",
        owner_user_id=None,
        setting_value='{"log_retention_days": 15}',
    )
    db_session.commit()
    assert log_cleanup.resolve_retention_days() == 15


def test_resolve_retention_out_of_range_fallback(db_session: Session):
    """越界保留天数回退默认值。"""
    Repository(SysSetting, db_session).create(
        setting_key="basic",
        scope="global",
        owner_user_id=None,
        setting_value='{"log_retention_days": 9999}',
    )
    db_session.commit()
    assert log_cleanup.resolve_retention_days() == log_cleanup.DEFAULT_RETENTION_DAYS


def test_resolve_retention_missing_setting_fallback(db_session: Session):
    """无设置记录时回退默认值。"""
    assert log_cleanup.resolve_retention_days() == log_cleanup.DEFAULT_RETENTION_DAYS


# ----------------------------------------------------------------------
# 调度器触发器构造
# ----------------------------------------------------------------------
def test_build_cron_trigger_valid():
    """合法 cron 表达式构造 CronTrigger。"""
    task = ScheduledTask(
        task_key=TASK_COOKIE_REFRESH,
        task_name="Cookie 刷新",
        schedule_type=SCHEDULE_TYPE_CRON,
        schedule_config="0 3 * * *",
        enabled=True,
    )
    trigger = SchedulerService._build_trigger(task)
    assert trigger is not None


def test_build_interval_trigger_valid():
    """合法间隔秒数构造 IntervalTrigger。"""
    task = ScheduledTask(
        task_key=TASK_PRODUCT_SYNC,
        task_name="商品同步",
        schedule_type=SCHEDULE_TYPE_INTERVAL,
        schedule_config="3600",
        enabled=True,
    )
    trigger = SchedulerService._build_trigger(task)
    assert trigger is not None


def test_build_trigger_invalid_config_returns_none():
    """非法调度配置返回 None（该任务将被跳过注册）。"""
    bad_interval = ScheduledTask(
        task_key=TASK_PRODUCT_SYNC, task_name="x",
        schedule_type=SCHEDULE_TYPE_INTERVAL, schedule_config="-1", enabled=True,
    )
    bad_cron = ScheduledTask(
        task_key=TASK_COOKIE_REFRESH, task_name="x",
        schedule_type=SCHEDULE_TYPE_CRON, schedule_config="not-a-cron", enabled=True,
    )
    unknown = ScheduledTask(
        task_key=TASK_COOKIE_REFRESH, task_name="x",
        schedule_type="unknown", schedule_config="1", enabled=True,
    )
    assert SchedulerService._build_trigger(bad_interval) is None
    assert SchedulerService._build_trigger(bad_cron) is None
    assert SchedulerService._build_trigger(unknown) is None


def test_scheduler_registers_only_enabled_valid_tasks(db_session: Session):
    """调度器仅注册启用且配置合法的任务，并可正常停止。"""
    repo = Repository(ScheduledTask, db_session)
    repo.create(task_key=TASK_COOKIE_REFRESH, task_name="Cookie 刷新",
                schedule_type=SCHEDULE_TYPE_INTERVAL, schedule_config="3600", enabled=True)
    repo.create(task_key=TASK_PRODUCT_SYNC, task_name="商品同步",
                schedule_type=SCHEDULE_TYPE_CRON, schedule_config="0 2 * * *", enabled=True)
    # 停用任务不应注册。
    repo.create(task_key=TASK_LOG_FILE_CLEANUP, task_name="日志清理",
                schedule_type=SCHEDULE_TYPE_INTERVAL, schedule_config="86400", enabled=False)
    db_session.commit()

    service = SchedulerService()
    try:
        registered = service.start()
        assert registered == 2
    finally:
        service.shutdown()


# ----------------------------------------------------------------------
# 任务执行体：跨服务调用结果驱动执行日志（需求 21.2）
# ----------------------------------------------------------------------
def test_run_cookie_refresh_no_shops(db_session: Session):
    """无启用店铺：Cookie 刷新记成功日志并跳过。"""
    task_runners.run_cookie_refresh()
    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_COOKIE_REFRESH})
    assert len(logs) == 1
    assert logs[0].run_result == RESULT_SUCCESS


def test_run_cookie_refresh_all_success(db_session: Session, monkeypatch):
    """全部店铺刷新成功：记成功执行日志（含 platform 透传）。"""
    Repository(Shop, db_session).create(
        shop_id="S1", shop_name="店1", owner_user_id=1, status=1
    )
    db_session.commit()

    captured = {}
    monkeypatch.setattr(
        service_client, "trigger_cookie_refresh",
        lambda shop_pk, shop_id, owner_user_id, platform="pdd": captured.update(
            platform=platform
        ) or service_client.CallResult(ok=True, message="ok"),
    )
    task_runners.run_cookie_refresh()

    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_COOKIE_REFRESH})
    assert len(logs) == 1
    assert logs[0].run_result == RESULT_SUCCESS
    # 无平台字段店铺回退默认 'pdd'。
    assert captured.get("platform") == "pdd"


def test_run_cookie_refresh_tiktok_platform_passed(db_session: Session, monkeypatch):
    """TikTok 店铺 cookie 刷新透传 platform='tiktok'（websocket 侧跳过刷新，防误刷）。"""
    Repository(Shop, db_session).create(
        shop_id="S_TK", shop_name="TikTok店", owner_user_id=1, status=1, platform="tiktok"
    )
    db_session.commit()

    captured = {}
    monkeypatch.setattr(
        service_client, "trigger_cookie_refresh",
        lambda shop_pk, shop_id, owner_user_id, platform="pdd": captured.update(
            platform=platform
        ) or service_client.CallResult(ok=True, message="ok"),
    )
    task_runners.run_cookie_refresh()

    # TikTok 店铺透传 platform='tiktok'（websocket 侧据此跳过 PDD 刷新路径）。
    assert captured.get("platform") == "tiktok"
    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_COOKIE_REFRESH})
    assert len(logs) == 1
    assert logs[0].run_result == RESULT_SUCCESS


def test_run_cookie_refresh_tiktok_probe_ok_not_logged(
    db_session: Session, monkeypatch
):
    """TikTok 巡检 ok：不打点，执行日志与改造前一致（避免常态刷屏）。"""
    Repository(Shop, db_session).create(
        shop_id="S_TK", shop_name="TikTok店", owner_user_id=1, status=1, platform="tiktok"
    )
    db_session.commit()

    monkeypatch.setattr(
        service_client, "trigger_cookie_refresh",
        lambda shop_pk, shop_id, owner_user_id, platform="pdd":
            service_client.CallResult(ok=True, message="ok", data={"login_probe": "ok"}),
    )
    task_runners.run_cookie_refresh()

    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_COOKIE_REFRESH})
    assert logs[0].run_result == RESULT_SUCCESS
    assert logs[0].message == "Cookie 刷新完成：成功 1 个，失败 0 个"
    assert "巡检异常" not in logs[0].message


def test_run_cookie_refresh_tiktok_probe_abnormal_appended(
    db_session: Session, monkeypatch
):
    """TikTok 巡检异常：明细追加到执行日志，构成 TIK-023 观测时间线。"""
    Repository(Shop, db_session).create(
        shop_id="S_TK", shop_name="TikTok店", owner_user_id=1, status=1, platform="tiktok"
    )
    db_session.commit()

    monkeypatch.setattr(
        service_client, "trigger_cookie_refresh",
        lambda shop_pk, shop_id, owner_user_id, platform="pdd":
            service_client.CallResult(
                ok=True, message="ok", data={"login_probe": "im_expired"}
            ),
    )
    task_runners.run_cookie_refresh()

    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_COOKIE_REFRESH})
    assert "店铺[S_TK] 巡检异常：IM 会话过期弹窗" in logs[0].message
    # 巡检异常不改变任务成败语义（巡检只观测，处置归告警链路与主循环）。
    assert logs[0].run_result == RESULT_SUCCESS


def test_run_cookie_refresh_pdd_ignores_probe(db_session: Session, monkeypatch):
    """PDD 店铺即使回传 login_probe 也不记录（零行为变更）。"""
    Repository(Shop, db_session).create(
        shop_id="S1", shop_name="店1", owner_user_id=1, status=1
    )
    db_session.commit()

    monkeypatch.setattr(
        service_client, "trigger_cookie_refresh",
        lambda shop_pk, shop_id, owner_user_id, platform="pdd":
            service_client.CallResult(
                ok=True, message="ok", data={"login_probe": "im_expired"}
            ),
    )
    task_runners.run_cookie_refresh()

    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_COOKIE_REFRESH})
    assert logs[0].message == "Cookie 刷新完成：成功 1 个，失败 0 个"


def test_collect_login_probe_note_unknown_value_passthrough():
    """未登记的巡检取值原样记录，便于排障时不丢信息。"""
    notes: list[str] = []
    task_runners._collect_login_probe_note(
        service_client.CallResult(ok=True, data={"login_probe": "future_value"}),
        "S9", "tiktok", notes,
    )
    assert notes == ["店铺[S9] 巡检异常：future_value"]


def test_run_product_sync_with_failure(db_session: Session, monkeypatch):
    """部分店铺同步失败：记失败执行日志（需求 21.2）。"""
    Repository(Shop, db_session).create(
        shop_id="S1", shop_name="店1", owner_user_id=1, status=1
    )
    db_session.commit()

    monkeypatch.setattr(
        service_client, "trigger_product_sync",
        lambda shop_pk: service_client.CallResult(ok=False, message="签名缺失"),
    )
    task_runners.run_product_sync()

    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_PRODUCT_SYNC})
    assert len(logs) == 1
    assert logs[0].run_result == RESULT_FAILED


def test_run_log_file_cleanup_writes_success(db_session: Session, tmp_path, monkeypatch):
    """文件日志清理执行体：执行后记成功日志。"""
    monkeypatch.setenv("LOG_FILE_DIR", str(tmp_path))
    task_runners.run_log_file_cleanup()
    logs = Repository(TaskRunLog, db_session).list(filters={"task_key": TASK_LOG_FILE_CLEANUP})
    assert len(logs) == 1
    assert logs[0].run_result == RESULT_SUCCESS


# ----------------------------------------------------------------------
# TIK-026：回复率巡检任务注册链路（任务键 ↔ 执行体 ↔ 调度注册）
# ----------------------------------------------------------------------
def test_supported_keys_and_runners_are_consistent():
    """受支持任务键集合与 TASK_RUNNERS 执行体一一对应（无孤儿键、无漏注册）。"""
    assert set(task_runners.TASK_RUNNERS.keys()) == set(SUPPORTED_TASK_KEYS)
    # 回复率巡检执行体已注册。
    assert TASK_REPLY_RATE_CHECK in task_runners.TASK_RUNNERS


def test_reply_rate_check_registered_as_hourly_interval(db_session: Session):
    """reply_rate_check 种子配置（interval 3600 秒 = 每小时）可被调度器正常注册。"""
    Repository(ScheduledTask, db_session).create(
        task_key=TASK_REPLY_RATE_CHECK,
        task_name="TikTok 回复率巡检",
        schedule_type=SCHEDULE_TYPE_INTERVAL,
        schedule_config="3600",
        enabled=True,
    )
    db_session.commit()

    service = SchedulerService()
    try:
        registered = service.start()
        assert registered == 1
        # 以任务键为 job_id 注册了一个可调度作业。
        assert service._scheduler.get_job(TASK_REPLY_RATE_CHECK) is not None
    finally:
        service.shutdown()
