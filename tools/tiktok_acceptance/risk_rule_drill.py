# -*- coding: utf-8 -*-
"""
tools.tiktok_acceptance.risk_rule_drill —— 风控频率断路器配置演练 CLI（TIK-020）
================================================================================
本工具用途（TIK-020「频率断路器配置化」验收 ①/② 的实测工具）：
在**真实数据库**上配置店铺风控规则（RiskRule），用真实的 TikTok 消息解析器与
真实的自动回复决策链驱动一批「演练消息」，验证：

- 阶段 A（未限制）：店铺风控不生效时，全部消息照常自动回复、不产生风控日志
  （验收 ②：未配规则店铺行为不变）；
- 阶段 B（限流命中）：配置单店铺窗口回复上限后，窗口内前 N 条照常回复，
  第 N+1 条起暂停自动回复并**真实落库风控日志** ``pdd_risk_log``
  （验收 ①：超限消息不再自动回复且 risk_log 可查）；
- 收尾：把规则固化为正式取值（默认 20 条 / 3600 秒），供灰度期使用。

真实度与隔离边界（重要）：
- 真实：风控规则读写、店铺运行时加载（营业时间 / 默认回复 / 关键词等）、
  决策链判定、风控日志落库全部走真实库与真实代码；
- 隔离：发送器 / 消息日志 / 聊天记录 / 实时推送均用内存桩替换，演练不会真的
  向买家发消息，也不会污染 ``pdd_message_log`` / ``pdd_chat_message`` 对账数据；
- 风控日志属审计数据，按规范不做物理删除，演练记录随正式运行记录一并留存。

运行方式（仓库根目录，使用 .venv）：
    ../.venv/Scripts/python tools/tiktok_acceptance/risk_rule_drill.py --shop-pk 1
    ../.venv/Scripts/python tools/tiktok_acceptance/risk_rule_drill.py --shop-pk 1 --drill-limit 3 --final-limit 20

退出码：0 = 两阶段结论均符合预期；1 = 结论不符或前置条件不满足（如非营业时间）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from common.db.repository import Repository
from common.models.config_models import BusinessHours, RiskRule
from common.models.log_models import RiskLog
from common.models.shop_models import Shop
from common.utils.business_hours import is_within_business_hours
from common.utils.time_utils import now_beijing, now_beijing_naive

# websocket 服务目录：engine / channel_tiktok 均在该包内，脚本直跑时需手动可达。
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_WEBSOCKET_DIR = os.path.join(_REPO_ROOT, "websocket")
for _path in (_WEBSOCKET_DIR, _REPO_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from channel_tiktok.tiktok_message import parse_tiktok_raw  # noqa: E402
from engine.message_consumer import MessageConsumer  # noqa: E402
from engine.reply_engine import ACTION_RISK_BLOCKED  # noqa: E402


# ----------------------------------------------------------------------
# 桩：替换对外副作用，仅记录调用
# ----------------------------------------------------------------------
class RecordingSender:
    """发送器桩：记录待发送的回复，不真正调用 TikTok 浏览器发送。"""

    def __init__(self) -> None:
        self.text_calls: List[Tuple[Any, Any]] = []
        self.image_calls: List[Tuple[Any, Any]] = []

    def send_text(self, recipient_uid, content):
        self.text_calls.append((recipient_uid, content))
        return {"success": True}

    def send_image(self, recipient_uid, content):
        self.image_calls.append((recipient_uid, content))
        return {"success": True}


def _collector(sink: List[Dict[str, Any]]):
    """返回把入参收集进列表的写入器（替代落库，避免污染对账数据）。"""

    def _write(values: Dict[str, Any]) -> None:
        sink.append(values)

    return _write


def _noop_realtime_push(**_kwargs: Any) -> None:
    """替换实时推送：演练不向 backend / 前端广播聊天事件。"""


# ----------------------------------------------------------------------
# 数据库读写
# ----------------------------------------------------------------------
def read_rule(session: Session, shop_pk: int) -> Optional[RiskRule]:
    """读取店铺当前风控规则；未配置返回 None。"""
    return Repository(RiskRule, session).get_by(shop_pk=shop_pk)


def upsert_rule(
    session: Session,
    shop_pk: int,
    *,
    session_limit: Optional[int],
    shop_limit: Optional[int],
    window_seconds: int,
    enabled: bool,
    user_id: Optional[int],
) -> RiskRule:
    """按店铺 upsert 风控规则并提交（与 backend risk_control_service 同一语义）。

    必须显式 commit：决策链的运行时加载走独立会话，未提交的规则对演练不可见。
    """
    repo = Repository(RiskRule, session)
    existing = repo.get_by(shop_pk=shop_pk)
    values = {
        "session_reply_limit": session_limit,
        "shop_reply_limit": shop_limit,
        "window_seconds": window_seconds,
        "enabled": enabled,
    }
    record = (
        repo.create(shop_pk=shop_pk, created_by=user_id, **values)
        if existing is None
        else repo.update(existing.id, **values)
    )
    session.commit()
    return record


def stage_since() -> datetime:
    """取阶段起始时刻：对齐到秒并回退 1 秒。

    库侧 ``log_time`` 为秒级精度，参考时刻若带微秒会因截断而反超落库时间，
    导致刚写入的风控日志查不到（边界漏查）。
    """
    return now_beijing_naive().replace(microsecond=0) - timedelta(seconds=1)


def count_logs_since(session: Session, shop_pk: int, since: datetime) -> int:
    """统计某店铺在 since 之后新增的风控日志条数。"""
    stmt = select(RiskLog).where(RiskLog.shop_pk == shop_pk, RiskLog.log_time >= since)
    return len(list(session.execute(stmt).scalars()))


def recent_risk_logs(
    session: Session, shop_pk: int, since: datetime, limit: int = 5
) -> List[RiskLog]:
    """查询演练期间落库的风控日志（最新 limit 条），作为「risk_log 可查」凭据。"""
    stmt = (
        select(RiskLog)
        .where(RiskLog.shop_pk == shop_pk, RiskLog.log_time >= since)
        .order_by(RiskLog.id.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def ensure_business_hours(session: Session, shop_pk: int) -> bool:
    """校验当前处于营业时间内（非营业时间消息会被前置拦截，测不到风控）。"""
    business = Repository(BusinessHours, session).get_by(shop_pk=shop_pk)
    if business is None:
        return True
    return is_within_business_hours(
        business.start_time,
        business.end_time,
        enabled=bool(business.enabled),
        weekdays=business.weekdays,
    )


# ----------------------------------------------------------------------
# 演练执行
# ----------------------------------------------------------------------
def make_consumer(shop_pk: int, shop_id: str, user_id: Optional[int]) -> MessageConsumer:
    """构造消费器：真实运行时加载 + 真实风控日志落库，对外副作用用桩替换。"""
    consumer = MessageConsumer(
        shop_id=shop_id,
        shop_pk=shop_pk,
        user_id=user_id,
        channel_name="tiktok",
        message_parser=parse_tiktok_raw,
        sender=RecordingSender(),
        log_writer=_collector([]),
        risk_log_writer=None,  # None = 用默认真实落库（验收①的 risk_log 凭据）
        notifier=None,
        chat_message_writer=_collector([]),
        default_reply_record_writer=_collector([]),
    )
    consumer._push_realtime_message = _noop_realtime_push  # noqa: SLF001 - 演练期禁用外部推送
    return consumer


def build_raw(customer_uid: str, to_uid: str, index: int) -> Dict[str, Any]:
    """构造一条 TikTok 买家文本消息 raw（格式对齐监控循环产出）。"""
    return {
        "msg_id": f"drill-{index}",
        "conversation_id": f"drill-{customer_uid}",
        "sender_role": "buyer",
        "msg_type": "text",
        "content": f"风控演练消息 {index}",
        "from_uid": customer_uid,
        "to_uid": to_uid,
        "nickname": "风控演练",
        "timestamp": int(now_beijing().timestamp()) + index,
    }


async def run_stage(
    consumer: MessageConsumer,
    customer_uid: str,
    to_uid: str,
    total: int,
) -> Dict[str, Any]:
    """驱动一批演练消息，返回「已回复 / 被风控暂停」的消息序号统计。"""
    replied: List[int] = []
    blocked: List[int] = []
    for index in range(1, total + 1):
        outcome = await consumer.consume_raw(build_raw(customer_uid, to_uid, index))
        if outcome.action == ACTION_RISK_BLOCKED:
            blocked.append(index)
        elif outcome.replied:
            replied.append(index)
    return {"replied": replied, "blocked": blocked}


async def drive_stage(shop: Shop, args: argparse.Namespace, total: int, suffix: str) -> Dict[str, Any]:
    """新建消费器（清空内存计数）并驱动一批演练消息。"""
    consumer = make_consumer(args.shop_pk, str(shop.shop_id), args.user_id)
    uid = f"TIK020-DRILL-{datetime.now().strftime('%H%M%S')}-{suffix}"
    return await run_stage(consumer, uid, str(shop.shop_id), total)


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="风控频率断路器配置演练（TIK-020）")
    parser.add_argument("--shop-pk", type=int, default=1, help="店铺主键 shop.id（默认 1）")
    parser.add_argument("--drill-limit", type=int, default=3, help="演练用的窗口回复上限（默认 3）")
    parser.add_argument("--window", type=int, default=3600, help="统计窗口秒数（默认 3600）")
    parser.add_argument("--final-limit", type=int, default=20, help="演练后固化的窗口回复上限（默认 20）")
    parser.add_argument("--messages", type=int, default=0, help="每阶段消息条数（默认 上限+2）")
    parser.add_argument("--user-id", type=int, default=1, help="操作人/归属用户 ID（默认 1）")
    parser.add_argument("--no-final", action="store_true", help="演练后不固化规则（保持演练取值）")
    return parser.parse_args()


def main() -> int:
    """执行两阶段演练并输出中文结论报告；结论不符返回 1。"""
    args = parse_args()
    total = args.messages or (args.drill_limit + 2)

    from common.db.session import get_session_factory

    factory: sessionmaker = get_session_factory()
    with factory() as session:
        shop = Repository(Shop, session).get(args.shop_pk)
        if shop is None:
            print(f"演练中止：店铺不存在 shop_pk={args.shop_pk}")
            return 1
        if not ensure_business_hours(session, args.shop_pk):
            print("演练中止：当前不在营业时间内，消息会先被「非营业时间」拦截，测不到风控。")
            return 1

        before = read_rule(session, args.shop_pk)
        print("=" * 72)
        print(
            f"风控频率断路器演练｜店铺 {shop.shop_name}"
            f"（shop_pk={args.shop_pk}, 平台={shop.platform}）"
        )
        print(f"当前规则：{_fmt_rule(before)}")
        print(f"演练参数：每阶段 {total} 条消息，窗口 {args.window}s，演练上限 {args.drill_limit}")
        print("=" * 72)

        # 阶段 A：风控不生效 → 全部照常回复（验收 ②）
        if before is not None:
            upsert_rule(
                session,
                args.shop_pk,
                session_limit=before.session_reply_limit,
                shop_limit=before.shop_reply_limit,
                window_seconds=before.window_seconds or args.window,
                enabled=False,
                user_id=args.user_id,
            )
            print("（该店原已配置规则：阶段 A 临时置为「未启用」，其行为等价于未配置）")
        since_a = stage_since()
        stage_a = asyncio.run(drive_stage(shop, args, total, "A"))
        logs_a = count_logs_since(session, args.shop_pk, since_a)
        ok_a = (
            len(stage_a["replied"]) == total
            and not stage_a["blocked"]
            and logs_a == 0
        )
        print(
            f"[阶段A-不限流] 回复 {len(stage_a['replied'])}/{total} 条，"
            f"风控暂停 {len(stage_a['blocked'])} 条，新增风控日志 {logs_a} 条"
            f" → {'符合预期' if ok_a else '不符合预期'}"
        )

        # 阶段 B：限流命中 → 前 N 条回复，之后暂停并落风控日志（验收 ①）
        upsert_rule(
            session,
            args.shop_pk,
            session_limit=None,
            shop_limit=args.drill_limit,
            window_seconds=args.window,
            enabled=True,
            user_id=args.user_id,
        )
        since_b = stage_since()
        stage_b = asyncio.run(drive_stage(shop, args, total, "B"))
        # 结束上一事务快照：MySQL 默认 REPEATABLE READ，不重开事务读不到刚落库的日志
        session.commit()
        logs_b = recent_risk_logs(session, args.shop_pk, since_b, limit=total)
        ok_b = (
            len(stage_b["replied"]) == args.drill_limit
            and len(stage_b["blocked"]) == total - args.drill_limit
            and len(logs_b) >= total - args.drill_limit
        )
        print(
            f"[阶段B-限流] 回复 {len(stage_b['replied'])}/{total} 条，"
            f"风控暂停 {len(stage_b['blocked'])} 条，新增风控日志 {len(logs_b)} 条"
            f" → {'符合预期' if ok_b else '不符合预期'}"
        )
        print(f"  被暂停的消息序号：{stage_b['blocked']}")
        for row in logs_b[:3]:
            print(
                f"  risk_log#{row.id} type={row.risk_type} "
                f"time={row.log_time} reason={row.trigger_reason}"
            )

        # 收尾：固化正式取值
        if not args.no_final:
            upsert_rule(
                session,
                args.shop_pk,
                session_limit=None,
                shop_limit=args.final_limit,
                window_seconds=args.window,
                enabled=True,
                user_id=args.user_id,
            )
            print(f"[收尾] 已固化规则：{_fmt_rule(read_rule(session, args.shop_pk))}")

        print("=" * 72)
        print(f"结论：阶段A {'通过' if ok_a else '未通过'}，阶段B {'通过' if ok_b else '未通过'}")
        return 0 if (ok_a and ok_b) else 1


def _fmt_rule(rule: Optional[RiskRule]) -> str:
    """格式化输出风控规则。"""
    if rule is None:
        return "未配置（恒放行）"
    return (
        f"单会话上限={rule.session_reply_limit}, 单店铺上限={rule.shop_reply_limit}, "
        f"窗口={rule.window_seconds}s, 启用={bool(rule.enabled)}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
