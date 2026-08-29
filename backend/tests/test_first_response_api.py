# -*- coding: utf-8 -*-
"""
backend.tests.test_first_response_api —— 首响时长统计接口/服务单元测试
====================================================================
本文件用途：对 backend 首响时长统计（app.api.routes.dashboard 的
``GET /dashboard/first-response`` 与 app.services.first_response_service）做单元
测试，覆盖 TIK-025 的核心口径与约束：

- 平台维度筛选（pdd / tiktok / 全部）与店铺维度筛选（shop_pk）；
- 首响时长分布分桶（左闭右开，末桶即超 5 分钟）与超 5 分钟占比；
- 回复率口径：超时与待回复均计未达标（待回复计入分母）；
- 数据范围隔离（需求 3.7）：非管理员只统计本人 / 被授权店铺；
- 参数校验：非法平台、非法日期、超长范围、无权限店铺均返回 success=false；
- 无权限访问被拒（需求 2.4）。

测试方案：pytest + FastAPI TestClient + 内存 SQLite（夹具见 conftest.py）。
首响口径与 tools/tiktok_acceptance/reconcile.py 共用 common.utils.latency，
故本文件不再重复验证纯函数本身（其单测见 common/tests/test_latency.py）。
"""
from __future__ import annotations

import pytest

from app.core.business_codes import CODE_FORBIDDEN, CODE_PARAM_ERROR, MSG_FORBIDDEN
from app.services import first_response_service
from common.models.log_models import ChatMessage
from common.models.shop_models import Shop
from common.models.user_models import (
    SysPermission,
    SysRole,
    SysRolePermission,
    SysUser,
)
from common.utils.security import hash_password
from common.utils.time_utils import now_beijing_naive

API_PREFIX = "/api/v1"
LOGIN_URL = f"{API_PREFIX}/login"
FIRST_RESPONSE_URL = f"{API_PREFIX}/dashboard/first-response"


def _today_at(hour: int, minute: int = 0, second: int = 0):
    """构造「今日」指定时刻的北京时间朴素 datetime（窗口内数据用）。"""
    return now_beijing_naive().replace(
        hour=hour, minute=minute, second=second, microsecond=0
    )


@pytest.fixture()
def fr_env(db_session):
    """预置 dashboard 权限、两个用户与三家店铺的聊天消息。

    店铺与消息布局（全部落在今日，默认 7 天窗口内）：
    - shop_tk（tiktok，dash_user）：c1 两段（20s 达标 / 420s 超时），c2 一段待回复；
    - shop_pdd（pdd，dash_user）：c3 一段（60s 达标）；
    - shop_other（tiktok，other_user）：c4 一段（600s 超时，不属于 dash_user）。
    """
    role_dash = SysRole(role_name="首响统计用户", is_admin=False, status=1)
    role_other = SysRole(role_name="其它用户", is_admin=False, status=1)
    db_session.add_all([role_dash, role_other])
    db_session.flush()

    perm = SysPermission(resource_key="dashboard", action="view")
    db_session.add(perm)
    db_session.flush()
    db_session.add(SysRolePermission(role_id=role_dash.id, permission_id=perm.id))
    db_session.flush()

    dash_password = "fr-password-123"
    other_password = "other-password-123"
    dash_user = SysUser(
        username="fr_user",
        password_hash=hash_password(dash_password),
        role_id=role_dash.id,
        status=1,
    )
    other_user = SysUser(
        username="fr_other",
        password_hash=hash_password(other_password),
        role_id=role_other.id,
        status=1,
    )
    db_session.add_all([dash_user, other_user])
    db_session.flush()

    shop_tk = Shop(
        shop_id="tk-1", shop_name="TikTok 店A", platform="tiktok",
        owner_user_id=dash_user.id, status=1,
    )
    shop_pdd = Shop(
        shop_id="pdd-1", shop_name="拼多多店B", platform="pdd",
        owner_user_id=dash_user.id, status=1,
    )
    shop_other = Shop(
        shop_id="tk-2", shop_name="他人 TikTok 店", platform="tiktok",
        owner_user_id=other_user.id, status=1,
    )
    db_session.add_all([shop_tk, shop_pdd, shop_other])
    db_session.flush()

    def msg(shop_pk: int, customer: str, direction: str, ts) -> ChatMessage:
        return ChatMessage(
            shop_pk=shop_pk, customer_uid=customer, direction=direction, msg_time=ts
        )

    db_session.add_all(
        [
            # TikTok 店A：c1 第一段 20 秒达标
            msg(shop_tk.id, "c1", "in", _today_at(10, 0, 0)),
            msg(shop_tk.id, "c1", "out", _today_at(10, 0, 20)),
            # TikTok 店A：c1 第二段 420 秒（7 分钟）超时
            msg(shop_tk.id, "c1", "in", _today_at(10, 5, 0)),
            msg(shop_tk.id, "c1", "out", _today_at(10, 12, 0)),
            # TikTok 店A：c2 窗口结束仍待回复
            msg(shop_tk.id, "c2", "in", _today_at(10, 20, 0)),
            # 拼多多店B：60 秒达标
            msg(shop_pdd.id, "c3", "in", _today_at(11, 0, 0)),
            msg(shop_pdd.id, "c3", "out", _today_at(11, 1, 0)),
            # 他人店铺：600 秒超时（不应被 dash_user 统计到）
            msg(shop_other.id, "c4", "in", _today_at(12, 0, 0)),
            msg(shop_other.id, "c4", "out", _today_at(12, 10, 0)),
        ]
    )
    db_session.commit()

    return {
        "dash_user": {"username": "fr_user", "password": dash_password, "id": dash_user.id},
        "other_user": {"username": "fr_other", "password": other_password, "id": other_user.id},
        "shop_tk_pk": shop_tk.id,
        "shop_pdd_pk": shop_pdd.id,
        "shop_other_pk": shop_other.id,
    }


def _login_token(client, username: str, password: str) -> str:
    """登录并返回访问令牌（断言登录成功）。"""
    resp = client.post(LOGIN_URL, json={"username": username, "password": password})
    body = resp.json()
    assert body["success"] is True, body
    return body["data"]["token"]


def _auth_header(token: str) -> dict:
    """构造 Bearer 鉴权请求头。"""
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------
# 平台 / 店铺维度筛选
# ----------------------------------------------------------------------
def test_platform_filter_tiktok_only(db_session, fr_env):
    """platform=tiktok 只统计 TikTok 店铺（需求：dashboard 平台维度）。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, platform="tiktok"
    )
    assert resp.success is True
    data = resp.data
    assert data["platform"] == "tiktok"
    assert data["shop_count"] == 1
    assert [row["shop_name"] for row in data["shops"]] == ["TikTok 店A"]
    summary = data["summary"]
    # 2 个已回复周期（20s / 420s）、1 个待回复周期。
    assert summary["responded_cycles"] == 2
    assert summary["pending_cycles"] == 1
    assert summary["pending_conversations"] == 1
    # 超时 1 个（420s > 300s），占比 0.5。
    assert summary["over_threshold_count"] == 1
    assert summary["over_threshold_ratio"] == 0.5


def test_all_platforms_when_platform_omitted(db_session, fr_env):
    """不传 platform 时统计全部平台（拼多多 + TikTok）。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(db_session, dash_user)
    assert resp.success is True
    data = resp.data
    assert data["platform"] is None
    assert data["shop_count"] == 2
    summary = data["summary"]
    # 3 个已回复周期（20s / 60s / 420s）+ 1 个待回复周期。
    assert summary["responded_cycles"] == 3
    assert summary["pending_cycles"] == 1
    # 均值 (20+60+420)/3；P50=60、P90=420（最近秩口径）。
    assert summary["mean_seconds"] == 166.67
    assert summary["p50_seconds"] == 60.0
    assert summary["p90_seconds"] == 420.0
    assert summary["max_seconds"] == 420.0
    assert summary["over_threshold_ratio"] == 0.3333


def test_platform_pdd_only(db_session, fr_env):
    """platform=pdd 只统计拼多多店铺。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, platform="pdd"
    )
    assert resp.success is True
    data = resp.data
    assert data["shop_count"] == 1
    assert data["summary"]["responded_cycles"] == 1
    assert data["summary"]["over_threshold_count"] == 0
    assert data["summary"]["reply_rate"] == 1.0


def test_shop_pk_filter(db_session, fr_env):
    """shop_pk 精确到单店铺，结果与按该店铺平台筛选一致。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, shop_pk=fr_env["shop_pdd_pk"]
    )
    assert resp.success is True
    data = resp.data
    assert data["shop_pk"] == fr_env["shop_pdd_pk"]
    assert data["shop_count"] == 1
    assert data["shops"][0]["shop_name"] == "拼多多店B"
    assert data["shops"][0]["platform"] == "pdd"


# ----------------------------------------------------------------------
# 分布与回复率口径
# ----------------------------------------------------------------------
def test_distribution_buckets(db_session, fr_env):
    """分布分桶左闭右开，末桶（超 5 分钟）计数等于超时计数。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(db_session, dash_user)
    data = resp.data
    buckets = data["distribution"]
    # 默认 5 桶：0-30s / 30-60s / 1-3min / 3-5min / 超 5 分钟
    assert [b["label"] for b in buckets] == [
        "30 秒内", "30–60 秒", "1–3 分钟", "3–5 分钟", "超 5 分钟"
    ]
    # 20s → 首桶；60s → 第三桶（上界不含）；420s → 末桶。
    assert [b["count"] for b in buckets] == [1, 0, 1, 0, 1]
    assert buckets[-1]["count"] == data["summary"]["over_threshold_count"]
    assert buckets[-1]["upper"] is None
    # 占比之和归一（各桶保留 4 位小数，故用近似比较）
    assert sum(b["ratio"] for b in buckets) == pytest.approx(1.0, abs=1e-3)


def test_reply_rate_pending_counts_as_failed(db_session, fr_env):
    """回复率：(已回复 - 超时) / (已回复 + 待回复)，待回复计未达标。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, platform="tiktok"
    )
    summary = resp.data["summary"]
    # (2 - 1) / (2 + 1) ≈ 0.3333
    assert summary["reply_rate"] == 0.3333
    # 分店铺回复率与汇总一致（本例仅 tiktok 一家店）
    assert resp.data["shops"][0]["reply_rate"] == 0.3333


def test_empty_window_returns_none_rates(db_session, fr_env):
    """窗口内无数据时：计数为 0，比率类指标为 None（不判 0 也不判 1）。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, start_date="2020-01-01", end_date="2020-01-02"
    )
    assert resp.success is True
    summary = resp.data["summary"]
    assert summary["responded_cycles"] == 0
    assert summary["over_threshold_ratio"] is None
    assert summary["reply_rate"] is None
    assert all(b["count"] == 0 for b in resp.data["distribution"])


# ----------------------------------------------------------------------
# 数据范围隔离与参数校验
# ----------------------------------------------------------------------
def test_data_scope_isolation(db_session, fr_env):
    """非管理员只统计本人店铺（他人 TikTok 店的 600s 超时不计入，需求 3.7）。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, platform="tiktok"
    )
    shop_pks = [row["shop_pk"] for row in resp.data["shops"]]
    assert fr_env["shop_other_pk"] not in shop_pks
    assert resp.data["summary"]["max_seconds"] == 420.0


def test_invalid_platform_rejected(db_session, fr_env):
    """非法平台标识返回 success=false（业务码 40000）。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, platform="shopee"
    )
    assert resp.success is False
    assert resp.code == CODE_PARAM_ERROR


def test_invalid_date_rejected(db_session, fr_env):
    """非法日期格式返回 success=false。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, start_date="2024/01/01"
    )
    assert resp.success is False
    assert resp.code == CODE_PARAM_ERROR


def test_range_exceeding_max_rejected(db_session, fr_env):
    """超出 92 天上限返回 success=false。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, start_date="2020-01-01", end_date="2024-01-01"
    )
    assert resp.success is False
    assert resp.code == CODE_PARAM_ERROR


def test_out_of_scope_shop_rejected(db_session, fr_env):
    """指定无权访问的店铺返回 success=false（不泄漏店铺是否存在）。"""
    dash_user = db_session.get(SysUser, fr_env["dash_user"]["id"])
    resp = first_response_service.get_first_response_stats(
        db_session, dash_user, shop_pk=fr_env["shop_other_pk"]
    )
    assert resp.success is False
    assert resp.code == CODE_PARAM_ERROR


def test_first_response_access_denied_for_unauthorized(client, fr_env):
    """无 dashboard 权限的用户访问首响统计被拒（需求 2.4）。"""
    token = _login_token(
        client, fr_env["other_user"]["username"], fr_env["other_user"]["password"]
    )
    resp = client.get(FIRST_RESPONSE_URL, headers=_auth_header(token))
    body = resp.json()
    assert resp.status_code == 200
    assert body["success"] is False
    assert body["code"] == CODE_FORBIDDEN
    assert body["message"] == MSG_FORBIDDEN


def test_first_response_endpoint_with_platform(client, fr_env):
    """经 HTTP 接口按平台查询（端到端串一遍路由层与权限层）。"""
    token = _login_token(
        client, fr_env["dash_user"]["username"], fr_env["dash_user"]["password"]
    )
    resp = client.get(
        FIRST_RESPONSE_URL,
        params={"platform": "tiktok"},
        headers=_auth_header(token),
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["success"] is True
    assert body["data"]["summary"]["responded_cycles"] == 2
    assert body["data"]["shop_count"] == 1
