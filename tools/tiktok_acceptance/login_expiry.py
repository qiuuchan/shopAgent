# -*- coding: utf-8 -*-
"""
tools.tiktok_acceptance.login_expiry —— TIK-023 登录态过期周期观测 CLI
=====================================================================
本工具用途（TIK-023 验收标准 ①②的取数侧工具）：
- 统计某 TikTok 店铺在观测窗口内 ``login_expired`` 告警事件的时间点与相邻间隔，
  给出「登录态过期周期」的样本结论（min / 中位 / max）；
- 按结论推算建议的 cookie_refresh 巡检周期，供管理端配置时参考；
- 同时核对巡检覆盖率与巡检异常打点条数，证明观测窗口本身是完整可信的。

数据来源与口径（北京时间，与规范 17 一致）：
- 过期事件：``pdd_notify_record``（event_type='login_expired'，按 shop_pk 过滤）；
- 巡检打点：``pdd_task_run_log``（task_key='cookie_refresh'），TIK-023 起每周期对
  TikTok 店铺做一次只读登录态巡检，异常时把明细写进 message；
- 参考项：同期 ``connection_disconnected`` 事件次数（浏览器死亡 / 断开与登录态
  问题常伴生，但不计入过期周期样本）。

口径限制（务必知悉，直接影响结论可信度）：
- AlertDedup 静默窗口（默认 30 分钟）会压制重复告警，故「过期事件次数」可能少于
  真实发生次数，据此得出的间隔是**上界**而非精确值；
- 只有「已过期」时间点、没有「登录生效」时间点（登录态不入库），故本工具给出的是
  **相邻过期事件的间隔**，真正的「登录 → 过期」时长需结合巡检时间线人工复核；
- 样本 <3 次过期事件（即 <2 个间隔）时不出周期结论，只给保守建议值。

运行方式（使用仓库根目录 .venv，common 已以 editable 方式安装）：
    python tools/tiktok_acceptance/login_expiry.py --shop 3
    python tools/tiktok_acceptance/login_expiry.py --shop 3 --days 14 --json
    python tools/tiktok_acceptance/login_expiry.py --shop 3 \
        --since 2026-08-29T00:00:00 --until 2026-09-12T23:59:59

本地无 MySQL 时的验证方式（--db 指定 sqlite 文件/内存库，需先建表，见
tools/tests/conftest.py 的方言适配）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.models.log_models import NotifyRecord
from common.models.task_models import TaskRunLog

# 同目录模块：脚本直跑（sys.path[0]=本目录）与包导入（pytest）两种场景均需可达
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from reconcile import make_session_factory  # noqa: E402 —— 复用对账 CLI 的会话工厂

# 登录态过期告警事件类型（与 websocket channel_tiktok._on_login_expired 约定一致）。
EVENT_LOGIN_EXPIRED: str = "login_expired"
# 连接断开事件类型（参考项，不计入过期周期样本）。
EVENT_CONNECTION_DISCONNECTED: str = "connection_disconnected"
# 巡检打点所在的任务键（TIK-023：TikTok 侧跳过刷新、仅作巡检触发点）。
COOKIE_REFRESH_TASK_KEY: str = "cookie_refresh"
# scheduler 执行日志中标记「TikTok 登录态巡检异常」的关键词（见 task_runners）。
PROBE_ALERT_KEYWORD: str = "巡检异常"

# 建议巡检周期的下界 / 上界（秒）：下界取当前 cookie_refresh 默认周期（600 秒），
# 低于此值无观测收益；上界 6 小时，超过则过期发现延迟不可接受。
MIN_PROBE_INTERVAL_SECONDS: int = 600
MAX_PROBE_INTERVAL_SECONDS: int = 21600
# 建议周期 = 最短观测间隔 / 该除数（留出足够采样冗余，避免整周期错配）。
SUGGEST_DIVISOR: int = 4
# 样本不足时的保守建议值（秒）：维持可观测性，不因样本少而贸然放宽。
DEFAULT_SUGGEST_SECONDS: int = 1800
# 判定「周期样本可信」所需的最少间隔数（对应 ≥3 次过期事件）。
MIN_INTERVAL_SAMPLES: int = 2
# 巡检覆盖率达到该比例即认为观测窗口完整（服务重启 / 调度抖动允许一定缺失）。
COVERAGE_OK_RATIO: float = 0.8

# 默认的 cookie_refresh 巡检周期（秒）：用于按窗口推算「应有巡检次数」。
DEFAULT_PROBE_INTERVAL_SECONDS: int = 600


@dataclass(frozen=True)
class ExpiryAnalysis:
    """登录态过期周期的样本分析结论（纯逻辑产物，可单测）。"""

    event_count: int                        # 窗口内过期事件次数
    intervals_seconds: List[float]          # 相邻过期事件的间隔（秒，升序时间）
    min_seconds: Optional[float]            # 最短间隔（None=无样本）
    median_seconds: Optional[float]         # 间隔中位数
    max_seconds: Optional[float]            # 最长间隔
    sample_sufficient: bool                 # 样本是否足以出周期结论
    suggested_interval_seconds: int         # 由此推算的建议巡检周期（秒）
    conclusion: str                         # 中文结论（含样本不足的提示）


def suggest_probe_interval(intervals_seconds: List[float]) -> int:
    """按观测到的间隔样本推算建议巡检周期（秒，纯函数）。

    取「最短间隔 / SUGGEST_DIVISOR」作为目标（最短间隔决定巡检必须多密才能不放过
    一次过期），向上取整到整分钟，再夹到 [下界, 上界] 之间。无样本时返回保守值。

    Args:
        intervals_seconds: 相邻过期事件的间隔列表（秒）。

    Returns:
        建议巡检周期（秒）。
    """
    if not intervals_seconds:
        return DEFAULT_SUGGEST_SECONDS
    shortest = min(intervals_seconds)
    raw = shortest / SUGGEST_DIVISOR
    rounded = int(math.ceil(raw / 60.0) * 60)
    return max(MIN_PROBE_INTERVAL_SECONDS, min(MAX_PROBE_INTERVAL_SECONDS, rounded))


def analyse_expiry(times: List[datetime]) -> ExpiryAnalysis:
    """分析登录态过期事件时间点，给出周期样本结论（纯函数，不查库）。

    时间点按升序去重后求相邻间隔；样本数（间隔数）< ``MIN_INTERVAL_SAMPLES`` 时不
    出周期结论（``sample_sufficient=False``），只给保守建议值，避免以个别样本误导
    调参。

    Args:
        times: ``login_expired`` 事件时间列表（北京时间，允许乱序传入）。

    Returns:
        ``ExpiryAnalysis`` 分析结论。
    """
    ordered = sorted({t for t in times if t is not None})
    intervals = [
        (later - earlier).total_seconds()
        for earlier, later in zip(ordered, ordered[1:])
    ]
    suggested = suggest_probe_interval(intervals)
    sample_sufficient = len(intervals) >= MIN_INTERVAL_SAMPLES

    if not ordered:
        conclusion = (
            "窗口内无 login_expired 事件：登录态在此期间保持有效，"
            "或告警未触发（需结合巡检覆盖率确认是前者还是漏报）"
        )
    elif len(intervals) < MIN_INTERVAL_SAMPLES:
        conclusion = (
            f"仅 {len(ordered)} 次过期事件（{len(intervals)} 个间隔样本），"
            f"不足以判定周期性；保守建议维持 ≤{suggested} 秒巡检，继续观测"
        )
    else:
        conclusion = (
            f"观测到 {len(ordered)} 次过期事件，间隔中位 "
            f"{statistics.median(intervals):.0f}s、最短 {min(intervals):.0f}s、"
            f"最长 {max(intervals):.0f}s；建议巡检周期 ≤{suggested} 秒"
        )

    return ExpiryAnalysis(
        event_count=len(ordered),
        intervals_seconds=intervals,
        min_seconds=min(intervals) if intervals else None,
        median_seconds=statistics.median(intervals) if intervals else None,
        max_seconds=max(intervals) if intervals else None,
        sample_sufficient=sample_sufficient,
        suggested_interval_seconds=suggested,
        conclusion=conclusion,
    )


def probe_coverage(
    run_times: List[datetime], since: datetime, until: datetime, interval_seconds: int
) -> Dict[str, Any]:
    """核对巡检覆盖率：窗口内实际巡检次数 / 应有次数（纯函数）。

    覆盖率偏低说明观测窗口不完整（服务重启、调度未启用、misfire 丢弃等），此时
    得出的过期周期结论不可信，需先补齐观测再下结论。

    Args:
        run_times: 窗口内 cookie_refresh 的执行时间列表。
        since: 观测窗口起点。
        until: 观测窗口终点。
        interval_seconds: 巡检周期（秒），用于推算应有次数。

    Returns:
        含应有/实际次数、覆盖率与是否完整的字典。
    """
    span = (until - since).total_seconds()
    expected = int(span // interval_seconds) if interval_seconds > 0 and span > 0 else 0
    actual = len(run_times)
    ratio = min(1.0, actual / expected) if expected > 0 else 0.0
    return {
        "expected_runs": expected,
        "actual_runs": actual,
        "coverage_ratio": round(ratio, 3),
        "complete": ratio >= COVERAGE_OK_RATIO,
    }


def count_probe_alerts(messages: List[Optional[str]]) -> int:
    """统计执行日志中含「巡检异常」的条数（纯函数）。

    该计数 = 巡检发现登录态异常的周期数，与巡检周期相乘可粗估「累计失效时长」，
    也可与告警事件次数交叉校验（告警受静默去重压制，条数通常不多于本计数）。

    Args:
        messages: 执行日志 message 列表。

    Returns:
        含巡检异常关键词的条数。
    """
    return sum(1 for m in messages if PROBE_ALERT_KEYWORD in (m or ""))


# ----------------------------------------------------------------------
# 数据库访问
# ----------------------------------------------------------------------
def query_notify_events(
    session: Session, shop_pk: int, since: datetime, until: datetime, event_type: str
) -> List[NotifyRecord]:
    """按店铺 + 时间窗口 + 事件类型查询通知记录（升序）。"""
    stmt = (
        select(NotifyRecord)
        .where(
            NotifyRecord.shop_pk == shop_pk,
            NotifyRecord.event_type == event_type,
            NotifyRecord.log_time.is_not(None),
            NotifyRecord.log_time >= since,
            NotifyRecord.log_time <= until,
        )
        .order_by(NotifyRecord.log_time, NotifyRecord.id)
    )
    return list(session.execute(stmt).scalars())


def count_notify_events(
    session: Session, shop_pk: int, since: datetime, until: datetime, event_type: str
) -> int:
    """统计指定事件类型在窗口内的条数（参数化查询，不加载实体）。"""
    stmt = (
        select(func.count())
        .select_from(NotifyRecord)
        .where(
            NotifyRecord.shop_pk == shop_pk,
            NotifyRecord.event_type == event_type,
            NotifyRecord.log_time.is_not(None),
            NotifyRecord.log_time >= since,
            NotifyRecord.log_time <= until,
        )
    )
    return int(session.execute(stmt).scalar() or 0)


def query_cookie_refresh_runs(
    session: Session, since: datetime, until: datetime
) -> List[TaskRunLog]:
    """查询窗口内 cookie_refresh 任务的执行日志（升序）。"""
    stmt = (
        select(TaskRunLog)
        .where(
            TaskRunLog.task_key == COOKIE_REFRESH_TASK_KEY,
            TaskRunLog.log_time.is_not(None),
            TaskRunLog.log_time >= since,
            TaskRunLog.log_time <= until,
        )
        .order_by(TaskRunLog.log_time, TaskRunLog.id)
    )
    return list(session.execute(stmt).scalars())


# ----------------------------------------------------------------------
# 报告构建
# ----------------------------------------------------------------------
def _fmt_dt(value: Optional[datetime]) -> Optional[str]:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def _fmt_duration(seconds: Optional[float]) -> str:
    """把秒数渲染为「Xd Yh Zm」的可读时长。"""
    if seconds is None:
        return "-"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def build_report(
    session: Session,
    shop_pk: int,
    since: datetime,
    until: datetime,
    probe_interval_seconds: int = DEFAULT_PROBE_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """构建登录态过期周期观测报告（JSON 结构，--json 直接输出）。"""
    expired_events = query_notify_events(
        session, shop_pk, since, until, EVENT_LOGIN_EXPIRED
    )
    disconnect_count = count_notify_events(
        session, shop_pk, since, until, EVENT_CONNECTION_DISCONNECTED
    )
    runs = query_cookie_refresh_runs(session, since, until)

    analysis = analyse_expiry([e.log_time for e in expired_events])
    coverage = probe_coverage(
        [r.log_time for r in runs], since, until, probe_interval_seconds
    )

    return {
        "shop_pk": shop_pk,
        "since": since.isoformat(sep=" "),
        "until": until.isoformat(sep=" "),
        "probe_interval_seconds": probe_interval_seconds,
        "expiry": {
            "event_count": analysis.event_count,
            "intervals_seconds": [round(x) for x in analysis.intervals_seconds],
            "min_seconds": analysis.min_seconds,
            "median_seconds": analysis.median_seconds,
            "max_seconds": analysis.max_seconds,
            "sample_sufficient": analysis.sample_sufficient,
            "suggested_interval_seconds": analysis.suggested_interval_seconds,
            "conclusion": analysis.conclusion,
        },
        "events": [
            {"time": _fmt_dt(e.log_time), "send_result": e.send_result}
            for e in expired_events
        ],
        "disconnect_event_count": disconnect_count,
        "probe_coverage": coverage,
        "probe_alert_runs": count_probe_alerts([r.message for r in runs]),
    }


# ----------------------------------------------------------------------
# 文本渲染
# ----------------------------------------------------------------------
def render_text(report: Dict[str, Any]) -> str:
    """渲染为对齐文本报告（默认输出）。"""
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(
        f"店铺 shop_pk={report['shop_pk']}  观测窗口 "
        f"[{report['since']} ~ {report['until']}]"
    )
    lines.append("=" * 72)

    expiry = report["expiry"]
    lines.append("【登录态过期事件（login_expired）】")
    if report["events"]:
        lines.append("  时间                 告警发送结果")
        for e in report["events"]:
            lines.append(f"  {e['time']}   {e['send_result'] or '-'}")
    else:
        lines.append("  （窗口内无 login_expired 事件）")

    lines.append("")
    lines.append("【过期间隔样本】")
    if expiry["intervals_seconds"]:
        lines.append(
            f"  样本 {len(expiry['intervals_seconds'])} 个｜"
            f"最短 {_fmt_duration(expiry['min_seconds'])}｜"
            f"中位 {_fmt_duration(expiry['median_seconds'])}｜"
            f"最长 {_fmt_duration(expiry['max_seconds'])}"
        )
    else:
        lines.append("  无间隔样本（过期事件 < 2 次）")

    lines.append("")
    lines.append("【巡检覆盖与异常打点】")
    cov = report["probe_coverage"]
    lines.append(
        f"  cookie_refresh 执行 {cov['actual_runs']} / 应执行 {cov['expected_runs']} 次"
        f"（覆盖率 {cov['coverage_ratio'] * 100:.1f}%，"
        f"{'窗口完整' if cov['complete'] else '窗口不完整，结论不可信'}）"
    )
    lines.append(f"  巡检异常打点 {report['probe_alert_runs']} 条")
    lines.append(
        f"  同期 connection_disconnected 事件 {report['disconnect_event_count']} 次（参考）"
    )

    lines.append("")
    lines.append("【结论】")
    lines.append(f"  {expiry['conclusion']}")
    lines.append(
        f"  据此建议 cookie_refresh 周期 ≤ {expiry['suggested_interval_seconds']} 秒"
    )
    if not cov["complete"]:
        lines.append("  ⚠ 巡检覆盖率不足，请先补齐观测再据此调参")
    lines.append(
        "  ⚠ 调参前请复核：cookie_refresh 为 PDD / TikTok 共用任务，"
        "调大周期会同步降低 PDD 侧 Cookie 保活频率（详见 README「周期调参规则」）"
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
        description="TIK-023 观测：统计 TikTok 店铺登录态过期周期并推算建议巡检周期"
    )
    parser.add_argument("--shop", type=int, required=True, help="店铺主键 shop_pk")
    parser.add_argument(
        "--since", type=str, default=None, help="窗口起点（ISO，北京时间）"
    )
    parser.add_argument(
        "--until", type=str, default=None, help="窗口终点（ISO，北京时间），默认当前时刻"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="默认窗口天数（无 --since/--until 时生效），默认 30",
    )
    parser.add_argument(
        "--probe-interval",
        type=int,
        default=DEFAULT_PROBE_INTERVAL_SECONDS,
        help="当前 cookie_refresh 巡检周期（秒），用于推算覆盖率，默认 600",
    )
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

    factory = make_session_factory(args.db)
    with factory() as session:
        report = build_report(
            session, args.shop, since, until, args.probe_interval
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
