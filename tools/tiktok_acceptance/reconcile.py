# -*- coding: utf-8 -*-
"""
tools.tiktok_acceptance.reconcile —— TIK-018 端到端验收对账 CLI
==============================================================
本工具用途（TIK-018 验收标准 1/2 的对账侧工具）：
- 按店铺 + 时间窗口统计 pdd_chat_message 的收发消息数与首响时长；
- 输出「每日收发统计」供与 seller center「客户消息」页计数人工对账；
- 输出「会话明细」与「首响汇总」量化验收标准 1（买家新消息 5 分钟内首响）。

口径说明（北京时间，与规范 17 一致）：
- 消息时间 msg_time 统一为北京时间，--since/--until 按北京时间解析，不做时区换算；
- 首响时长口径见同目录 latency.py（多买家消息静默聚合、>300s 判超时）；
- 消息日志以落库为准：验收对账 = 本工具输出 vs 平台后台计数人工比对（半自动）。

运行方式（使用仓库根目录 .venv，common 已以 editable 方式安装）：
    python tools/tiktok_acceptance/reconcile.py --shop 3 --days 2
    python tools/tiktok_acceptance/reconcile.py --shop 3 --since 2026-08-30T00:00:00 --until 2026-08-31T23:59:59 --json
    python tools/tiktok_acceptance/reconcile.py --shop 3 --days 7 --customer U123  # 只看单个会话

本地无 MySQL 时的验证方式（--db 指定 sqlite 文件/内存库，需先建表）：
    ../.venv/Scripts/python -c "from sqlalchemy import create_engine; from sqlalchemy.orm import sessionmaker; \
from common.models import *; e=create_engine('sqlite:///t.db'); \
import common.models.log_models; from common.models.base import Base; Base.metadata.create_all(e)"
    python tools/tiktok_acceptance/reconcile.py --shop 3 --days 1 --db sqlite:///t.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from common.models.log_models import ChatMessage

# 同目录模块：脚本直跑（sys.path[0]=本目录）与包导入（pytest）两种场景均需可达
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from latency import DIRECTION_IN, DIRECTION_OUT, PendingCycle, compute_cycles, first_response_stats


# ----------------------------------------------------------------------
# 数据库访问
# ----------------------------------------------------------------------
def _make_session_factory(db_url: Optional[str]) -> sessionmaker:
    """构造会话工厂：--db 指定时用该 URL（本地 sqlite 验证），否则走系统配置。

    系统配置路径为 common.db.session.get_session_factory()（MySQL 连接池单例，
    配置来自 common.core.config.get_settings().database_url）。
    """
    if db_url:
        from sqlalchemy import create_engine

        engine = create_engine(db_url)
        return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)
    from common.db.session import get_session_factory

    return get_session_factory()


def query_messages(
    session: Session, shop_pk: int, since: datetime, until: datetime
) -> List[ChatMessage]:
    """按店铺 + 时间窗口查询聊天消息（升序），msg_time 为空的行不参与统计。"""
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.shop_pk == shop_pk,
            ChatMessage.msg_time.is_not(None),
            ChatMessage.msg_time >= since,
            ChatMessage.msg_time <= until,
        )
        .order_by(ChatMessage.customer_uid, ChatMessage.msg_time, ChatMessage.id)
    )
    return list(session.execute(stmt).scalars())


# ----------------------------------------------------------------------
# 报告构建（纯逻辑，可单测）
# ----------------------------------------------------------------------
def _fmt_dt(value: Optional[datetime]) -> Optional[str]:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def _conversation_stats(msgs: List[ChatMessage]) -> Dict[str, Any]:
    """单个会话的收发计数与首响统计（cycles/pending 来自 latency 纯函数）。"""
    in_count = sum(1 for m in msgs if m.direction == DIRECTION_IN)
    out_count = sum(1 for m in msgs if m.direction == DIRECTION_OUT)
    cycles, pending = compute_cycles((m.direction, m.msg_time) for m in msgs)
    latency_list = [c.latency_seconds for c in cycles]
    if cycles:
        status = "待回复" if pending else "已回复"
    elif pending:
        status = "待回复"
    else:
        status = "仅客服消息"
    first = min((m.msg_time for m in msgs), default=None)
    last = max((m.msg_time for m in msgs), default=None)
    return {
        "customer_uid": msgs[0].customer_uid,
        "in_count": in_count,
        "out_count": out_count,
        "first_msg": _fmt_dt(first),
        "last_msg": _fmt_dt(last),
        "status": status,
        "cycles": len(cycles),
        "pending_cycles": len(pending),
        "latency_median_seconds": (
            sorted(latency_list)[len(latency_list) // 2] if latency_list else None
        ),
        "latency_max_seconds": max(latency_list) if latency_list else None,
    }


def build_report(
    messages: List[ChatMessage], shop_pk: int, since: datetime, until: datetime
) -> Dict[str, Any]:
    """由消息列表构建对账报告（JSON 结构，--json 直接输出）。"""
    by_customer: Dict[str, List[ChatMessage]] = defaultdict(list)
    for m in messages:
        by_customer[m.customer_uid].append(m)

    conversations = [_conversation_stats(v) for v in by_customer.values()]
    conversations.sort(key=lambda c: c["customer_uid"])

    daily: Dict[str, Dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0})
    for m in messages:
        day = m.msg_time.date().isoformat()
        if m.direction == DIRECTION_IN:
            daily[day]["in"] += 1
        elif m.direction == DIRECTION_OUT:
            daily[day]["out"] += 1
    daily_rows = [
        {"date": d, "in": v["in"], "out": v["out"]} for d, v in sorted(daily.items())
    ]

    all_cycles, all_pending = [], []
    pending_conversations = 0
    for v in by_customer.values():
        cycles, pending = compute_cycles((m.direction, m.msg_time) for m in v)
        all_cycles.extend(cycles)
        all_pending.extend(pending)
        if pending:
            pending_conversations += 1

    return {
        "shop_pk": shop_pk,
        "since": since.isoformat(sep=" "),
        "until": until.isoformat(sep=" "),
        "totals": {
            "conversations": len(conversations),
            "in_messages": sum(m.direction == DIRECTION_IN for m in messages),
            "out_messages": sum(m.direction == DIRECTION_OUT for m in messages),
        },
        "daily": daily_rows,
        "conversations": conversations,
        "first_response_stats": first_response_stats(
            all_cycles, all_pending, pending_conversations
        ),
    }


# ----------------------------------------------------------------------
# 文本渲染
# ----------------------------------------------------------------------
def render_text(report: Dict[str, Any]) -> str:
    """渲染为对齐文本报告（默认输出）。"""
    lines: List[str] = []
    t = report["totals"]
    lines.append("=" * 72)
    lines.append(
        f"店铺 shop_pk={report['shop_pk']}  窗口 [{report['since']} ~ {report['until']}]"
    )
    lines.append(
        f"会话 {t['conversations']} 个｜收 {t['in_messages']} 条｜发 {t['out_messages']} 条"
    )
    lines.append("=" * 72)

    lines.append("【每日收发统计（与 seller center 计数人工对账）】")
    lines.append("  日期        收    发")
    for row in report["daily"]:
        lines.append(f"  {row['date']}  {row['in']:>5}  {row['out']:>5}")
    if not report["daily"]:
        lines.append("  （窗口内无消息）")

    lines.append("")
    lines.append("【会话明细】")
    header = "  客户       收  发  状态      首响中位(秒) 最长(秒) 周期数"
    lines.append(header)
    for c in report["conversations"]:
        med = "-" if c["latency_median_seconds"] is None else f"{c['latency_median_seconds']:.0f}"
        mx = "-" if c["latency_max_seconds"] is None else f"{c['latency_max_seconds']:.0f}"
        lines.append(
            f"  {c['customer_uid'][:14]:<14} {c['in_count']:>3} {c['out_count']:>3}  "
            f"{c['status']:<6} {med:>10} {mx:>8} {c['cycles']:>5}"
        )

    lines.append("")
    lines.append("【首响时长汇总（阈值 300 秒 = 5 分钟）】")
    s = report["first_response_stats"]
    if s["responded_cycles"]:
        lines.append(
            f"  已回复周期 {s['responded_cycles']}｜平均 {s['mean_seconds']:.0f}s｜"
            f"中位 {s['p50_seconds']:.0f}s｜P90 {s['p90_seconds']:.0f}s｜最长 {s['max_seconds']:.0f}s"
        )
        lines.append(
            f"  超 5 分钟 {s['over_threshold_count']} 个（占比 {s['over_threshold_ratio'] * 100:.1f}%）"
        )
    else:
        lines.append("  无已回复周期")
    lines.append(
        f"  待回复周期 {s['pending_cycles']} 个（涉及会话 {s['pending_conversations']} 个）"
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _parse_time(value: str) -> datetime:
    """解析 --since/--until（接受 ISO 日期或日期时间，按北京时间处理）。"""
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromisoformat(text + "T00:00:00")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TIK-018 验收对账：按店铺统计收发消息与首响时长（北京时间）"
    )
    parser.add_argument("--shop", type=int, required=True, help="店铺主键 shop_pk")
    parser.add_argument(
        "--since", type=str, default=None, help="窗口起点（ISO，北京时间），如 2026-08-30T00:00:00"
    )
    parser.add_argument(
        "--until", type=str, default=None, help="窗口终点（ISO，北京时间），默认当前时刻"
    )
    parser.add_argument(
        "--days", type=int, default=7, help="默认窗口天数（无 --since/--until 时生效），默认 7"
    )
    parser.add_argument("--customer", type=str, default=None, help="只看指定 customer_uid 会话")
    parser.add_argument("--json", action="store_true", help="输出 JSON 全量报告")
    parser.add_argument("--db", type=str, default=None, help="数据库 URL 覆盖（默认系统配置）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    until = _parse_time(args.until) if args.until else datetime.now()
    if args.since:
        since = _parse_time(args.since)
    else:
        since = (until - timedelta(days=args.days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    factory = _make_session_factory(args.db)
    with factory() as session:
        messages = query_messages(session, args.shop, since, until)

    if args.customer:
        messages = [m for m in messages if m.customer_uid == args.customer]

    report = build_report(messages, args.shop, since, until)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
