# -*- coding: utf-8 -*-
"""
common.utils.alert_dedup —— 告警去重与防抖（跨服务共享组件）
============================================================
本文件用途：为「连接断开 / 登录态失效 / 回复率跌破阈值」等系统事件提供**去重与防抖**
能力，避免事件风暴在短时内把企微 Webhook 刷爆（需求 26 / 关联工单 TIK-005、TIK-026）。

历史：本模块初建于 TIK-005（websocket/engine/alert_dedup.py），供 websocket 侧
连接断开告警链路使用；TIK-026 因 scheduler 侧「回复率跌破阈值」告警需复用同一
去重语义而**上移至 common**（scheduler 不应依赖 websocket 包，跨服务禁止导入），
原 websocket 模块保留为兼容壳（re-export 全部符号，存量调用点零改动）。

设计要点：
- **去重键**：以 ``(shop_pk, event_type)`` 二元组为维度。同一店铺、同一类事件在
  静默期内只发送一次，避免重试风暴持续打告警。
- **静默期（silence_seconds）**：默认 30 分钟（1800 秒，规范 17 全链路北京时间口径
  下计秒）。在静默期内 ``should_send`` 返回 False，同类事件不重复发送；超过静默期
  后自动恢复。
- **resolve 立即恢复**：事件恢复（连接恢复 / 回复率回到阈值之上）后调用 ``resolve``
  清除该键的静默状态，使下一次同类事件可立即再次告警（例如恢复后再次跌破也应能
  重新通知）。
- **零外部依赖**：纯内存结构（datetime 比对），无 I/O，便于单元与属性测试；
  时间统一经 ``common.utils.time_utils.now_beijing``（北京时间，规范 17）。
- **线程 / 协程安全**：各服务为单进程事件循环模型，去重表仅在事件循环内访问，
  无需额外加锁（保持简单，符合「不做过度设计」原则）。

实现约束（开发规范）：单文件 ≤500 行（35）、文件名用下划线（40）、中文注释（37/50）、
导入置顶（51）、复用共通（52）。
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Tuple

from common.utils.time_utils import now_beijing

logger = logging.getLogger("common.alert_dedup")

# 默认静默期（秒）：30 分钟。同类事件在此窗口内仅告警一次。
DEFAULT_SILENCE_SECONDS: float = 1800.0

# 去重键类型别名：(店铺主键 shop_pk, 事件类型 event_type)。
DedupKey = Tuple[int, str]


class AlertDedup:
    """告警去重器：按 (shop_pk, event_type) 维度在静默期内防抖去重。

    典型用法（配合 ``build_alert_notifier``）：达到重连上限置 ERROR 时，经
    ``event_notifier`` 触发 ``connection_disconnected`` 事件；notifier 内部先调用
    ``should_send`` 判断是否已处于静默期，非静默才真正发送并 ``mark_sent`` 记录时间。
    """

    def __init__(self, silence_seconds: float = DEFAULT_SILENCE_SECONDS) -> None:
        """初始化去重器。

        Args:
            silence_seconds: 静默期秒数（同类事件在该时长内不重复发送）。
                须为正数，非正值时回退为默认 1800 秒以保证防抖有效性。
        """
        if silence_seconds <= 0:
            logger.warning(
                "告警静默期配置非法（%s），回退为默认 %s 秒",
                silence_seconds, DEFAULT_SILENCE_SECONDS,
            )
            silence_seconds = DEFAULT_SILENCE_SECONDS
        self.silence_seconds: float = float(silence_seconds)
        # 最近一次发送时间（北京时间）：键 -> datetime（带时区）。
        self._last_sent: Dict[DedupKey, object] = {}

    def _make_key(self, shop_pk: int, event_type: str) -> DedupKey:
        """构造去重键（规范 52：统一二元组维度）。"""
        return (shop_pk, event_type)

    def should_send(self, shop_pk: int, event_type: str) -> bool:
        """判断当前是否应当发送该事件（去重防抖核心）。

        规则：
        - 从未发送过（无静默记录）→ 允许发送。
        - 已发送过，但距上次发送已超过静默期 → 允许发送。
        - 已发送过，且仍在静默期内 → 不允许发送（防抖去重）。

        Args:
            shop_pk: 店铺主键 shop.id。
            event_type: 事件类型（如 ``connection_disconnected``）。

        Returns:
            True 表示应当发送；False 表示处于静默期应跳过。
        """
        key = self._make_key(shop_pk, event_type)
        last = self._last_sent.get(key)
        if last is None:
            return True
        # 北京时间口径比较（规范 17）。now_beijing 返回带时区 datetime。
        elapsed = (now_beijing() - last).total_seconds()
        return elapsed >= self.silence_seconds

    def mark_sent(self, shop_pk: int, event_type: str) -> None:
        """记录该事件已发送，开启静默期（下次同类事件在静默期内被去重）。

        Args:
            shop_pk: 店铺主键 shop.id。
            event_type: 事件类型。
        """
        key = self._make_key(shop_pk, event_type)
        self._last_sent[key] = now_beijing()

    def resolve(self, shop_pk: int, event_type: str) -> None:
        """立即清除该事件的静默状态（事件已恢复 / 人工确认后调用）。

        清除后下一次同类事件可立即再次告警（例如连接已恢复，随后再次断开也应立即
        通知，而非被上一次静默期拦住）。与 ``mark_sent`` 互斥：发送记静默、恢复清静默。

        Args:
            shop_pk: 店铺主键 shop.id。
            event_type: 事件类型。
        """
        key = self._make_key(shop_pk, event_type)
        self._last_sent.pop(key, None)

    def clear(self) -> None:
        """清空全部静默记录（供测试或重启冷状态使用）。"""
        self._last_sent.clear()


# 告警发送回调签名：send_cb(event_type, content, shop_pk) -> None
AlertSendCb = Callable[[str, str, int], None]


def build_alert_notifier(
    dedup: AlertDedup,
    shop_pk: int,
    send_cb: Optional[AlertSendCb] = None,
) -> Callable[[str, str], None]:
    """构造单店铺的告警通知器（去重 + 尽力而为发送）。

    返回 ``notify(event_type, content)`` 可调用对象，供 ``PDDChannel.event_notifier``
    或 cookie 刷新失败分支调用。其内部行为（规范 52 复用 AlertDedup）：

    1. 经 ``dedup.should_send`` 判断是否处于静默期；静默期内直接跳过（防抖）。
    2. 非静默则 ``dedup.mark_sent`` 记录时间，避免风暴期内重复。
    3. 若注入 ``send_cb`` 则调用真实发送（如企微 Webhook）；未注入（Webhook 地址
       暂未提供）仅打 info 日志占位，不影响主流程。

    Args:
        dedup: 去重器实例（建议经 ``get_alert_dedup`` 取全局共享，保证跨连接维度一致）。
        shop_pk: 本通知器归属的店铺主键（闭包捕获，调用方无需每次传入）。
        send_cb: 可选真实发送回调；未提供时仅日志占位。

    Returns:
        ``notify(event_type, content)`` 可调用对象（同步；主流程不阻塞等待发送结果）。
    """

    def _notify(event_type: str, content: str) -> None:
        if not dedup.should_send(shop_pk, event_type):
            # 静默期内：去重跳过，避免刷爆企微（需求 26 防抖）。
            logger.info(
                "告警去重跳过（静默期内）: shop_pk=%s, event_type=%s",
                shop_pk, event_type,
            )
            return

        # 记录发送时间，开启静默期（即便真实发送回调缺失也照常防抖）。
        dedup.mark_sent(shop_pk, event_type)
        if send_cb is not None:
            try:
                send_cb(event_type, content, shop_pk)
            except Exception as exc:  # noqa: BLE001 - 告警发送失败不应影响主链路
                logger.warning(
                    "告警发送回调失败（已记静默）: shop_pk=%s, event_type=%s, %s",
                    shop_pk, event_type, exc,
                )
        else:
            # Webhook 地址暂未提供：仅日志占位，真实联调时补充 send_cb。
            logger.info(
                "告警（未配置 Webhook，仅记录）: shop_pk=%s, event_type=%s, content=%s",
                shop_pk, event_type, content,
            )

    return _notify


# 模块级全局单例：保证跨 channel / cookie 刷新使用同一去重维度（同一 (shop_pk,
# event_type) 在所有入口共享静默状态）。首次访问惰性创建，避免导入期副作用。
_alert_dedup_singleton: Optional[AlertDedup] = None


def get_alert_dedup() -> AlertDedup:
    """获取全局共享的告警去重单例（惰性创建，规范 52 复用）。

    Returns:
        进程内唯一的 ``AlertDedup`` 实例。
    """
    global _alert_dedup_singleton
    if _alert_dedup_singleton is None:
        _alert_dedup_singleton = AlertDedup()
    return _alert_dedup_singleton


__all__ = [
    "AlertDedup",
    "DEFAULT_SILENCE_SECONDS",
    "AlertSendCb",
    "build_alert_notifier",
    "get_alert_dedup",
]
