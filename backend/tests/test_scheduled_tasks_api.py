# -*- coding: utf-8 -*-
"""
backend.tests.test_scheduled_tasks_api —— 定时任务接口单元测试
==============================================================
本文件用途：对 backend 定时任务接口（任务 17.6 配套，需求 21.2）进行单元测试，
覆盖核心验收场景：

- 管理员查询定时任务列表时，缺失的内置任务被幂等补齐（规范 14）；
- 管理员更新调度方式 / 配置 / 启停用生效（需求 21.2）；
- 非法调度方式被拒（返回中文提示）；
- 管理员启停用定时任务（需求 21.2 配套）；
- 执行日志列表后端分页查询（需求 21.2 / 21.4）；
- 非管理员访问定时任务接口被拒（需求 21.17）。

测试方案：pytest + FastAPI TestClient + SQLite 内存库（夹具见 conftest.py），
所有接口 HTTP 恒返回 200，业务成败由统一响应体 {code, success, message, data}
表达。
"""
from __future__ import annotations

from app.core.business_codes import CODE_FORBIDDEN, CODE_PARAM_ERROR

# 业务路由统一前缀（与 _bootstrap.API_PREFIX 默认值一致）。
API_PREFIX = "/api/v1"
LOGIN_URL = f"{API_PREFIX}/login"
TASKS_URL = f"{API_PREFIX}/scheduled-tasks"
RUN_LOGS_URL = f"{API_PREFIX}/scheduled-tasks/run-logs"


def _login(client, username: str, password: str) -> str:
    """登录并返回访问令牌字符串。"""
    resp = client.post(LOGIN_URL, json={"username": username, "password": password})
    return resp.json()["data"]["token"]


def _auth(token: str) -> dict:
    """构造带 Bearer 令牌的鉴权请求头。"""
    return {"Authorization": f"Bearer {token}"}


def test_list_tasks_seeds_defaults(client, test_users):
    """管理员查询任务列表时缺失的内置任务被幂等补齐（规范 14 / 需求 21.2）。"""
    admin_token = _login(client, test_users["admin"]["username"], test_users["admin"]["password"])
    resp = client.get(TASKS_URL, headers=_auth(admin_token)).json()
    assert resp["success"] is True
    keys = {task["task_key"] for task in resp["data"]["list"]}
    # 四项内置任务均应补齐（含 TikTok 营业时间窗，TIK-015）
    assert {"cookie_refresh", "product_sync", "log_file_cleanup", "tiktok_window"}.issubset(keys)


def test_update_task_persists(client, test_users):
    """管理员更新调度配置与启停用生效（需求 21.2）。"""
    admin_token = _login(client, test_users["admin"]["username"], test_users["admin"]["password"])
    task = client.get(TASKS_URL, headers=_auth(admin_token)).json()["data"]["list"][0]

    upd = client.put(
        f"{TASKS_URL}/{task['id']}",
        headers=_auth(admin_token),
        json={"schedule_type": "interval", "schedule_config": "1200", "enabled": False},
    ).json()
    assert upd["success"] is True
    assert upd["data"]["schedule_type"] == "interval"
    assert upd["data"]["schedule_config"] == "1200"
    assert upd["data"]["enabled"] is False


def test_update_task_invalid_schedule_type_rejected(client, test_users):
    """非法调度方式被拒并返回中文提示（需求 21.2）。"""
    admin_token = _login(client, test_users["admin"]["username"], test_users["admin"]["password"])
    task = client.get(TASKS_URL, headers=_auth(admin_token)).json()["data"]["list"][0]

    resp = client.put(
        f"{TASKS_URL}/{task['id']}",
        headers=_auth(admin_token),
        json={"schedule_type": "bad_type", "schedule_config": "1"},
    ).json()
    assert resp["success"] is False
    assert resp["code"] == CODE_PARAM_ERROR


def test_set_task_status(client, test_users):
    """管理员启停用定时任务（需求 21.2 配套）。"""
    admin_token = _login(client, test_users["admin"]["username"], test_users["admin"]["password"])
    task = client.get(TASKS_URL, headers=_auth(admin_token)).json()["data"]["list"][0]

    resp = client.put(
        f"{TASKS_URL}/{task['id']}/status",
        headers=_auth(admin_token),
        json={"enabled": False},
    ).json()
    assert resp["success"] is True
    assert resp["data"]["enabled"] is False


def test_list_run_logs_paginated(client, test_users):
    """执行日志列表后端分页查询（需求 21.2 / 21.4）。"""
    admin_token = _login(client, test_users["admin"]["username"], test_users["admin"]["password"])
    resp = client.get(RUN_LOGS_URL, headers=_auth(admin_token)).json()
    assert resp["success"] is True
    # 分页结构完整
    assert set(["list", "total", "page", "page_size"]).issubset(resp["data"].keys())


def test_non_admin_rejected(client, test_users):
    """非管理员访问定时任务接口被拒（需求 21.17）。"""
    guest_token = _login(client, test_users["guest"]["username"], test_users["guest"]["password"])
    resp = client.get(TASKS_URL, headers=_auth(guest_token)).json()
    assert resp["success"] is False
    assert resp["code"] == CODE_FORBIDDEN
