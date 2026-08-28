# -*- coding: utf-8 -*-
"""
tools.tests.test_reconcile —— 对账报告集成测试（SQLite 内存库）
==============================================================
覆盖 build_report / query_messages：
- 多会话收发计数与首响周期切分正确；
- 店铺隔离（shop_pk 过滤）与时间窗口过滤；
- 待回复会话 / 仅客服消息会话的状态标注；
- CLI 冒烟（--db 指向 sqlite 文件，输出文本报告）。
"""
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import common.models.log_models  # noqa: F401  导入以注册全部表
from common.models.base import Base
from common.models.log_models import ChatMessage
from tools.tiktok_acceptance import reconcile
from tools.tiktok_acceptance.reconcile import build_report, query_messages


def _dt(day, h, m=0, s=0):
    return datetime.datetime(2026, 8, day, h, m, s)


@pytest.fixture
def db_session():
    """内存 SQLite 会话，建全量表。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


def _msg(shop_pk, customer_uid, direction, dt, content="hi"):
    return ChatMessage(
        shop_pk=shop_pk,
        customer_uid=customer_uid,
        direction=direction,
        msg_type="text",
        content=content,
        msg_time=dt,
    )


def _seed(session):
    """预置：店铺 1 四会话 + 店铺 2 一会话 + 窗口外一条。"""
    session.add_all(
        [
            # 店铺 1 / A：买家两条聚合后回复，首响 360s（超 5 分钟）
            _msg(1, "A", "in", _dt(30, 9, 0, 0)),
            _msg(1, "A", "in", _dt(30, 9, 5, 0)),
            _msg(1, "A", "out", _dt(30, 9, 6, 0)),
            # 店铺 1 / B：首响 60s（达标）
            _msg(1, "B", "in", _dt(30, 10, 0, 0)),
            _msg(1, "B", "out", _dt(30, 10, 1, 0)),
            # 店铺 1 / C：只进未回 → 待回复
            _msg(1, "C", "in", _dt(30, 11, 0, 0)),
            # 店铺 1 / D：只发未收 → 仅客服消息
            _msg(1, "D", "out", _dt(30, 12, 0, 0)),
            # 店铺 2：应被隔离
            _msg(2, "X", "in", _dt(30, 13, 0, 0)),
            _msg(2, "X", "out", _dt(30, 13, 1, 0)),
            # 店铺 1 窗口外（前一天）：应被过滤
            _msg(1, "A", "in", _dt(29, 23, 0, 0)),
        ]
    )
    session.commit()


def test_query_messages_filters_shop_and_window(db_session):
    _seed(db_session)
    since, until = _dt(30, 0, 0, 0), _dt(30, 23, 59, 59)
    rows = query_messages(db_session, 1, since, until)
    assert len(rows) == 7  # 店铺 1 全部 7 条（不含店铺 2 与窗口外）
    assert all(r.shop_pk == 1 for r in rows)
    assert all(since <= r.msg_time <= until for r in rows)


def test_build_report_totals_and_daily(db_session):
    _seed(db_session)
    since, until = _dt(30, 0, 0, 0), _dt(30, 23, 59, 59)
    messages = query_messages(db_session, 1, since, until)
    report = build_report(messages, 1, since, until)

    assert report["totals"] == {
        "conversations": 4,
        "in_messages": 4,  # A2 + B1 + C1
        "out_messages": 3,  # A1 + B1 + D1
    }
    assert report["daily"] == [{"date": "2026-08-30", "in": 4, "out": 3}]


def test_build_report_conversation_status(db_session):
    _seed(db_session)
    since, until = _dt(30, 0, 0, 0), _dt(30, 23, 59, 59)
    report = build_report(query_messages(db_session, 1, since, until), 1, since, until)

    by_uid = {c["customer_uid"]: c for c in report["conversations"]}
    assert by_uid["A"]["status"] == "已回复"
    assert by_uid["A"]["in_count"] == 2
    assert by_uid["A"]["cycles"] == 1
    assert by_uid["B"]["status"] == "已回复"
    assert by_uid["C"]["status"] == "待回复"
    assert by_uid["C"]["pending_cycles"] == 1
    assert by_uid["D"]["status"] == "仅客服消息"


def test_build_report_first_response_stats(db_session):
    _seed(db_session)
    since, until = _dt(30, 0, 0, 0), _dt(30, 23, 59, 59)
    report = build_report(query_messages(db_session, 1, since, until), 1, since, until)

    stats = report["first_response_stats"]
    assert stats["responded_cycles"] == 2
    assert stats["mean_seconds"] == 210.0  # (360 + 60) / 2
    assert stats["p50_seconds"] == 60.0
    assert stats["p90_seconds"] == 360.0
    assert stats["max_seconds"] == 360.0
    assert stats["over_threshold_count"] == 1
    assert stats["over_threshold_ratio"] == 0.5
    assert stats["pending_cycles"] == 1
    assert stats["pending_conversations"] == 1


def test_cli_smoke(tmp_path):
    """CLI 冒烟：--db 指向 sqlite 文件，输出文本报告且能解析 JSON 模式。"""
    db_file = tmp_path / "reconcile.db"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)
    with factory() as session:
        session.add(_msg(1, "A", "in", _dt(30, 9, 0, 0)))
        session.add(_msg(1, "A", "out", _dt(30, 9, 1, 0)))
        session.commit()

    argv = [
        "--shop", "1",
        "--since", "2026-08-30T00:00:00",
        "--until", "2026-08-30T23:59:59",
        "--db", f"sqlite:///{db_file}",
    ]
    assert reconcile.main(argv) == 0

    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert reconcile.main(argv + ["--json"]) == 0
    import json
    data = json.loads(buf.getvalue())
    assert data["shop_pk"] == 1
    assert data["first_response_stats"]["responded_cycles"] == 1
