# -*- coding: utf-8 -*-
"""
tools.tests.test_first_response_drill —— 首响统计验收演练 CLI 集成测试
====================================================================
覆盖 tools/tiktok_acceptance/first_response_drill.py：
- 真实 backend 统计服务在 SQLite 库上跑通（不依赖 MySQL，隔离可重复）；
- 与 reconcile 对账工具的口径抽查结论为「一致」，且各指标数值符合预期；
- 分布桶末桶计数等于超时计数、回复率按「超时 + 待回复均计未达标」计算；
- 平台错配 / 无管理员用户等失败场景返回退出码 1。

说明：演练脚本自身会把 backend 目录加入 sys.path，故本文件可直接
``from tools.tiktok_acceptance import first_response_drill``。
"""
import contextlib
import datetime
import io
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from common.models.base import Base
from common.models.log_models import ChatMessage
from common.models.shop_models import Shop
from common.models.user_models import SysRole, SysUser
from common.utils.security import hash_password
from tools.tiktok_acceptance import first_response_drill


def _dt(h, m=0, s=0):
    return datetime.datetime(2026, 8, 30, h, m, s)


def _build_db(db_file):
    """建库并返回会话工厂（建全量表）。"""
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(
        bind=engine, class_=Session, expire_on_commit=False, future=True
    )


def _seed(factory, *, with_admin=True):
    """预置管理员用户、TikTok 店铺与三条会话。

    会话布局（窗口 2026-08-30 全天）：
    - A：买家两条聚合后 360 秒才回复 → 超时；
    - B：60 秒回复 → 达标；
    - C：只进未回 → 待回复。
    """
    shop_pk = None
    with factory() as session:
        if with_admin:
            role = SysRole(role_name="管理员", is_admin=True, status=1)
            session.add(role)
            session.flush()
            session.add(
                SysUser(
                    username="drill_admin",
                    password_hash=hash_password("drill-password-123"),
                    role_id=role.id,
                    status=1,
                )
            )
        shop = Shop(shop_id="tk-drill", shop_name="演练店铺", platform="tiktok", status=1)
        session.add(shop)
        session.flush()
        shop_pk = shop.id

        def msg(customer, direction, ts):
            return ChatMessage(
                shop_pk=shop.id,
                customer_uid=customer,
                direction=direction,
                msg_type="text",
                content="hi",
                msg_time=ts,
            )

        session.add_all(
            [
                msg("A", "in", _dt(9, 0, 0)),
                msg("A", "in", _dt(9, 5, 0)),
                msg("A", "out", _dt(9, 6, 0)),   # 首响 360s → 超时
                msg("B", "in", _dt(10, 0, 0)),
                msg("B", "out", _dt(10, 1, 0)),  # 首响 60s → 达标
                msg("C", "in", _dt(11, 0, 0)),   # 待回复
            ]
        )
        session.commit()
    return shop_pk


def _argv(shop_pk, db_file, extra=()):
    return [
        "--shop", str(shop_pk),
        "--since", "2026-08-30",
        "--until", "2026-08-30",
        "--db", f"sqlite:///{db_file}",
        *extra,
    ]


def _run_json(argv):
    """运行 CLI 并捕获 JSON 输出，返回 (退出码, 报告字典)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = first_response_drill.main(argv)
    return code, json.loads(buf.getvalue())


def test_drill_consistent_with_reconcile(tmp_path):
    """演练结论为「口径一致」，且各指标与预置数据吻合。"""
    db_file = tmp_path / "drill.db"
    shop_pk = _seed(_build_db(db_file))

    code, report = _run_json(_argv(shop_pk, db_file, ["--json"]))
    assert code == 0
    assert report["consistent"] is True
    assert report["mismatched_fields"] == []

    summary = report["backend"]["summary"]
    assert summary["responded_cycles"] == 2
    assert summary["pending_cycles"] == 1
    assert summary["over_threshold_count"] == 1
    assert summary["over_threshold_ratio"] == 0.5
    # 回复率 = (已回复 2 - 超时 1) / (已回复 2 + 待回复 1) ≈ 0.3333
    assert summary["reply_rate"] == 0.3333
    # 抽查项逐条与对账工具一致
    for item in report["comparisons"]:
        assert item["match"] is True, item


def test_drill_distribution_and_shop_rows(tmp_path):
    """分布末桶计数等于超时计数；分店铺明细带出店铺名与平台。"""
    db_file = tmp_path / "drill.db"
    shop_pk = _seed(_build_db(db_file))

    code, report = _run_json(_argv(shop_pk, db_file, ["--json"]))
    assert code == 0

    buckets = report["backend"]["distribution"]
    # 60s 落在「30–60 秒」的下一桶（左闭右开），360s 落在末桶
    assert [b["count"] for b in buckets] == [0, 0, 1, 0, 1]
    assert buckets[-1]["count"] == report["backend"]["summary"]["over_threshold_count"]

    shops = report["backend"]["shops"]
    assert len(shops) == 1
    assert shops[0]["shop_name"] == "演练店铺"
    assert shops[0]["platform"] == "tiktok"
    assert shops[0]["reply_rate"] == 0.3333


def test_drill_text_report_renders(tmp_path):
    """文本模式可渲染且包含关键小节（CLI 默认输出）。"""
    db_file = tmp_path / "drill.db"
    shop_pk = _seed(_build_db(db_file))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = first_response_drill.main(_argv(shop_pk, db_file))
    text = buf.getvalue()
    assert code == 0
    assert "【首响汇总（backend 真实链路）】" in text
    assert "【首响时长分布】" in text
    assert "【口径抽查（backend vs reconcile 对账工具）】" in text
    assert "口径完全一致" in text


def test_drill_platform_mismatch_returns_failure(tmp_path):
    """店铺平台与 --platform 筛选不匹配 → 查询失败，退出码 1。"""
    db_file = tmp_path / "drill.db"
    shop_pk = _seed(_build_db(db_file))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = first_response_drill.main(_argv(shop_pk, db_file, ["--platform", "pdd"]))
    assert code == 1
    assert "演练失败" in buf.getvalue()


def test_drill_without_admin_user_returns_failure(tmp_path):
    """库中无管理员用户 → 无法以全量视角演练，退出码 1。"""
    db_file = tmp_path / "drill.db"
    shop_pk = _seed(_build_db(db_file), with_admin=False)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = first_response_drill.main(_argv(shop_pk, db_file))
    assert code == 1
    assert "管理员" in buf.getvalue()


@pytest.mark.parametrize("missing_shop_pk", [9999])
def test_drill_missing_shop_returns_failure(tmp_path, missing_shop_pk):
    """店铺不存在 → 退出码 1。"""
    db_file = tmp_path / "drill.db"
    _seed(_build_db(db_file))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = first_response_drill.main(_argv(missing_shop_pk, db_file))
    assert code == 1
    assert "不存在" in buf.getvalue()
