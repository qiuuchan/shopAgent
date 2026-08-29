# -*- coding: utf-8 -*-
"""
tools.tiktok_acceptance.first_response_drill —— TIK-025 首响统计验收演练 CLI
==========================================================================
本工具用途（TIK-025 验收标准 ①/② 的实测工具）：
- 用**真实库 + 真实 backend 统计服务**跑一遍首响统计（真实链路，非 mock）；
- 与 TIK-018 验收对账工具（reconcile.py，同一窗口、同一店铺）逐项抽查口径是否一致；
- 输出首响分布、超 5 分钟占比、回复率与分店铺明细，供人工核对 dashboard 展示。

隔离边界（重要，避免污染对账数据）：
- **只读**：仅查询 ``pdd_chat_message`` / ``pdd_shop`` / ``sys_user`` / ``sys_role``，
  不写入任何业务表，不落 ``pdd_message_log``，不触发真实发送；
- **无对外副作用**：不拉起浏览器、不连平台、不发企微通知、不启 WebSocket；
- 统计逻辑走 backend 的 ``app.services.first_response_service``（与线上接口同一实现）。

运行方式（使用仓库根目录 .venv，common 已以 editable 方式安装）：
    python tools/tiktok_acceptance/first_response_drill.py --shop 1 --days 7
    python tools/tiktok_acceptance/first_response_drill.py --shop 1 --platform tiktok --json
    python tools/tiktok_acceptance/first_response_drill.py --since 2026-08-23 --until 2026-08-29

退出码：口径全部一致为 0；任一项不一致或查询失败为 1。

口径说明（与 reconcile.py 完全一致，均来自 common.utils.latency）：
- 周期 = 一段买家连续消息 + 其后本店首次回复；首响 = 回复时间 − 该段首条买家消息；
- 窗口结束仍无回复 → 待回复（pending）；首响 >300 秒 → 超时；
- 回复率 = (已回复 − 超时) / (已回复 + 待回复)（超时与待回复均计未达标）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

# 同目录模块：脚本直跑（sys.path[0]=本目录）与包导入（pytest）两种场景均需可达
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# backend 服务目录与仓库根目录：使 `import app.*` 与 `import common.*` 可用
_REPO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
for _path in (_REPO_ROOT, _BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from common.models.shop_models import Shop  # noqa: E402
from common.models.user_models import SysRole, SysUser  # noqa: E402

from reconcile import build_report, make_session_factory, query_messages  # noqa: E402

from app.services import first_response_service  # noqa: E402

# 参与口径抽查的指标项：backend 统计字段 -> reconcile 对账报告字段
COMPARE_FIELDS: tuple[str, ...] = (
    "responded_cycles",
    "pending_cycles",
    "pending_conversations",
    "over_threshold_count",
    "over_threshold_ratio",
    "mean_seconds",
    "p50_seconds",
    "p90_seconds",
)


# ----------------------------------------------------------------------
# 数据库访问
# ----------------------------------------------------------------------
def load_admin_user(session: Session) -> Optional[SysUser]:
    """取一个管理员用户作为统计操作者（管理员可见全部店铺，便于跨店抽查）。

    演练关注「统计口径是否与对账工具一致」，故用管理员身份避开数据范围隔离的
    干扰；数据范围隔离本身由 backend 单测（test_first_response_api.py）覆盖。
    """
    stmt = (
        select(SysUser)
        .join(SysRole, SysUser.role_id == SysRole.id)
        .where(SysRole.is_admin.is_(True), SysUser.status == 1)
        .order_by(SysUser.id)
    )
    return session.execute(stmt).scalars().first()


def load_shop(session: Session, shop_pk: int) -> Optional[Shop]:
    """按主键读取店铺（用于报告展示店铺名与平台）。"""
    return session.get(Shop, shop_pk)


# ----------------------------------------------------------------------
# 演练主体
# ----------------------------------------------------------------------
def _fmt_seconds(value: Optional[float]) -> str:
    """秒数格式化（空值显示占位符）。"""
    if value is None:
        return "-"
    return f"{value:.0f}s"


def _fmt_percent(value: Optional[float]) -> str:
    """比例格式化（0~1 小数转百分比，空值显示占位符）。"""
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _fmt_value(value: Any, as_float: bool) -> str:
    """口径比对列的取值格式化：空值占位，浮点保留 2 位小数。"""
    if value is None:
        return "-"
    return f"{value:.2f}" if as_float else str(value)


def run_drill(
    session: Session,
    shop_pk: int,
    since: datetime,
    until: datetime,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    """跑一次演练：真实 backend 统计 + 对账工具口径抽查。

    Args:
        session: 数据库会话（真实库）。
        shop_pk: 店铺主键。
        since: 窗口起点（含，北京时间）。
        until: 窗口终点（含，北京时间）。
        platform: 平台筛选；None 表示全部平台。

    Returns:
        演练报告字典（--json 直接输出）。

    Raises:
        RuntimeError: 未找到管理员用户 / 店铺不存在 / backend 查询失败时抛出。
    """
    admin = load_admin_user(session)
    if admin is None:
        raise RuntimeError("库中无启用的管理员用户，无法以全量视角演练（请先创建管理员）")
    shop = load_shop(session, shop_pk)
    if shop is None:
        raise RuntimeError(f"店铺 shop_pk={shop_pk} 不存在")

    start_key = since.strftime("%Y-%m-%d")
    end_key = until.strftime("%Y-%m-%d")

    # ① 真实链路：backend 统计服务（与 GET /dashboard/first-response 同一实现）
    resp = first_response_service.get_first_response_stats(
        session,
        admin,
        start_date=start_key,
        end_date=end_key,
        platform=platform,
        shop_pk=shop_pk,
    )
    if not resp.success:
        raise RuntimeError(f"backend 统计查询失败：{resp.message}")
    stats = resp.data

    # ② 对账工具同窗口同店铺再算一遍（reconcile 口径，用于抽查一致性）
    messages = query_messages(session, shop_pk, since, until)
    reconcile_report = build_report(messages, shop_pk, since, until)
    reconcile_stats = reconcile_report["first_response_stats"]

    # ③ 逐项比对（抽查口径一致）
    comparisons: List[Dict[str, Any]] = []
    for field in COMPARE_FIELDS:
        backend_value = stats["summary"].get(field)
        reconcile_value = reconcile_stats.get(field)
        comparisons.append(
            {
                "field": field,
                "backend": backend_value,
                "reconcile": reconcile_value,
                "match": backend_value == reconcile_value,
            }
        )
    mismatches = [c for c in comparisons if not c["match"]]

    return {
        "shop": {
            "shop_pk": shop_pk,
            "shop_name": shop.shop_name,
            "platform": shop.platform,
        },
        "window": {
            "since": since.isoformat(sep=" "),
            "until": until.isoformat(sep=" "),
            "start_date": start_key,
            "end_date": end_key,
        },
        "platform_filter": platform,
        "threshold_seconds": stats["threshold_seconds"],
        "backend": stats,
        "reconcile": reconcile_stats,
        "comparisons": comparisons,
        "consistent": not mismatches,
        "mismatched_fields": [c["field"] for c in mismatches],
    }


# ----------------------------------------------------------------------
# 文本渲染
# ----------------------------------------------------------------------
def render_text(report: Dict[str, Any]) -> str:
    """渲染为对齐文本报告（默认输出）。"""
    lines: List[str] = []
    shop = report["shop"]
    window = report["window"]
    summary = report["backend"]["summary"]

    lines.append("=" * 72)
    lines.append(
        f"店铺 shop_pk={shop['shop_pk']}（{shop['shop_name'] or '-'} / {shop['platform']}）"
        f"  窗口 [{window['since']} ~ {window['until']}]"
    )
    lines.append(
        f"平台筛选 {report['platform_filter'] or '全部平台'}｜"
        f"阈值 {report['threshold_seconds']:.0f} 秒"
    )
    lines.append("=" * 72)

    lines.append("【首响汇总（backend 真实链路）】")
    lines.append(
        f"  已回复周期 {summary['responded_cycles']}｜待回复周期 {summary['pending_cycles']}"
        f"（涉及会话 {summary['pending_conversations']} 个）"
    )
    lines.append(
        f"  平均 {_fmt_seconds(summary['mean_seconds'])}｜"
        f"P50 {_fmt_seconds(summary['p50_seconds'])}｜"
        f"P90 {_fmt_seconds(summary['p90_seconds'])}｜"
        f"最长 {_fmt_seconds(summary['max_seconds'])}"
    )
    lines.append(
        f"  超 5 分钟 {summary['over_threshold_count']} 个"
        f"（占比 {_fmt_percent(summary['over_threshold_ratio'])}）"
    )
    lines.append(f"  回复率 {_fmt_percent(summary['reply_rate'])}")

    lines.append("")
    lines.append("【首响时长分布】")
    lines.append("  桶            条数    占比")
    for bucket in report["backend"]["distribution"]:
        lines.append(
            f"  {bucket['label']:<12} {bucket['count']:>5}  {_fmt_percent(bucket['ratio']):>8}"
        )

    lines.append("")
    lines.append("【分店铺明细】")
    lines.append("  店铺                 平台     已回复  待回复  超时  P50    回复率")
    for row in report["backend"]["shops"]:
        name = (row["shop_name"] or f"店铺#{row['shop_pk']}")[:18]
        lines.append(
            f"  {name:<20} {row['platform']:<8} {row['responded_cycles']:>5} "
            f"{row['pending_cycles']:>6} {row['over_threshold_count']:>5} "
            f"{_fmt_seconds(row['p50_seconds']):>6} {_fmt_percent(row['reply_rate']):>8}"
        )

    lines.append("")
    lines.append("【口径抽查（backend vs reconcile 对账工具）】")
    lines.append("  指标                 backend      reconcile    一致")
    for item in report["comparisons"]:
        backend_value = item["backend"]
        # 浮点类指标（比率 / 秒数均值）保留 2 位小数，其余原样输出
        use_float = isinstance(backend_value, float)
        lines.append(
            f"  {item['field']:<20} {_fmt_value(backend_value, use_float):>11}"
            f"  {_fmt_value(item['reconcile'], use_float):>13}"
            f"    {'是' if item['match'] else '否'}"
        )
    lines.append(
        "  结论："
        + (
            "口径完全一致 ✅"
            if report["consistent"]
            else f"存在不一致 ❌：{report['mismatched_fields']}"
        )
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _parse_day_start(value: str) -> datetime:
    """解析 --since（YYYY-MM-DD 取当日零点；也接受 ISO 日期时间，按北京时间处理）。"""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.fromisoformat(text + "T00:00:00")
    if len(text) == 10:
        # 纯日期：起点对齐当日零点，与 backend 的自然日窗口一致
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return parsed


def _parse_day_end(value: str) -> datetime:
    """解析 --until（YYYY-MM-DD 取当日 23:59:59；也接受 ISO 日期时间）。"""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.fromisoformat(text + "T23:59:59")
    if len(text) == 10:
        # 纯日期：终点对齐当日最后一秒，确保包含结束日全天
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=0)
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TIK-025 首响统计验收演练：真实库跑 backend 统计并与对账工具抽查口径"
    )
    parser.add_argument("--shop", type=int, required=True, help="店铺主键 shop_pk")
    parser.add_argument(
        "--since", type=str, default=None, help="窗口起点（YYYY-MM-DD，北京时间）"
    )
    parser.add_argument(
        "--until", type=str, default=None, help="窗口终点（YYYY-MM-DD，北京时间），默认今日"
    )
    parser.add_argument(
        "--days", type=int, default=7, help="默认窗口天数（无 --since/--until 时生效），默认 7"
    )
    parser.add_argument(
        "--platform",
        type=str,
        default=None,
        choices=["pdd", "tiktok"],
        help="平台筛选（缺省=全部平台）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 全量报告")
    parser.add_argument("--db", type=str, default=None, help="数据库 URL 覆盖（默认系统配置）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    until = (
        _parse_day_end(args.until)
        if args.until
        else datetime.now().replace(hour=23, minute=59, second=59, microsecond=0)
    )
    if args.since:
        since = _parse_day_start(args.since)
    else:
        since = (until - timedelta(days=args.days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    factory = make_session_factory(args.db)
    with factory() as session:
        try:
            report = run_drill(session, args.shop, since, until, args.platform)
        except RuntimeError as exc:
            print(f"演练失败：{exc}")
            return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0 if report["consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())
