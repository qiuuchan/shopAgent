# -*- coding: utf-8 -*-
"""
backend.app.services.notify_service —— 通知渠道与消息通知业务服务
================================================================
本文件用途：实现 backend 服务的「通知渠道与消息通知」业务逻辑，供 notify 路由
复用，满足需求 18（通知渠道与消息通知）：

- ``create_notify_channel(...)`` / ``update_notify_channel(...)``：配置通知渠道
  （渠道类型 + 目标地址 + 启停用），持久化并返回统一响应体（需求 18.1）。渠道
  类型须为合法枚举（email/webhook/wecom），枚举入数据字典 ``channel_type``。
- ``list_notify_channels(...)``：通知渠道列表后端分页查询（需求 18.5 配套）。
- ``test_notify_channel(...)``：对某通知渠道发起测试发送并返回发送结果，写入
  通知记录（需求 18.2）；发送失败记日志不中断（需求 18.4）。
- ``push_system_event(...)``：发生需通知的系统事件（连接断开 / 登录态失效 /
  风控触发）时，经「该店铺已启用」渠道推送通知（需求 18.3，店铺级方案 A）；逐渠道
  发送，单个渠道失败仅记通知失败日志与系统日志，不中断对其它渠道的推送与主流程
  （需求 18.4）。
- ``list_notify_records(...)``：通知记录后端分页查询（需求 18.5），按北京时间
  倒序返回。
- ``list_channel_types(...)``：查询「通知渠道类型」枚举字典（需求 18.x / 24.7）。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3 / 需求 24.1）。
- 所有数据访问经 common.db.repository 参数化查询，禁止拼接 SQL（规范 16）。
- 通知渠道类型枚举入数据字典 ``channel_type``，前端展示中文（规范 15 / 需求 18.x）。
- 通知 / 系统日志禁止物理删除；记录保留（规范 11 / 需求 19.5）。
- 失败记日志不中断主流程：渠道发送以 try/except 兜底，异常转为「失败」记录并
  写系统日志，绝不向上抛出打断推送主流程（需求 18.4 / 26）。
- 时间字段统一北京时间（规范 17 / 需求 24.8）；导入置顶（规范 51）；
  中文注释（规范 37）；单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.business_codes import (
    CODE_EXTERNAL_ERROR,
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    CODE_PARAM_ERROR,
    MSG_FORBIDDEN,
)
from app.core.data_scope import (
    build_data_scope,
    is_in_scope,
    paginate_shop_scoped,
)
from common.db.repository import Repository
from common.models.log_models import NotifyRecord, SystemLog
from common.models.setting_models import NotifyChannel
from common.models.shop_models import Shop
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.services.dict_service import DictService
from common.utils.time_utils import now_beijing_naive, safe_isoformat

logger = logging.getLogger(__name__)

# 通知渠道类型字典分组键（见 dict_seed_data 的 channel_type，需求 18.x）。
DICT_CHANNEL_TYPE: str = "channel_type"

# 合法的通知渠道类型枚举键（与 sys_dict 的 channel_type 字典一致）。
ALLOWED_CHANNEL_TYPES: tuple[str, ...] = ("email", "webhook", "wecom")

# 系统事件类型与其中文文案（需求 18.3：连接断开 / 登录态失效 / 风控触发）。
EVENT_CONNECTION_DISCONNECTED: str = "connection_disconnected"
EVENT_LOGIN_EXPIRED: str = "login_expired"
EVENT_RISK_TRIGGERED: str = "risk_triggered"
EVENT_TYPE_LABELS: Dict[str, str] = {
    EVENT_CONNECTION_DISCONNECTED: "连接断开",
    EVENT_LOGIN_EXPIRED: "登录态失效",
    EVENT_RISK_TRIGGERED: "风控触发",
}

# 通知发送结果取值（写入 notify_record.send_result）。
SEND_RESULT_SUCCESS: str = "success"
SEND_RESULT_FAILED: str = "failed"

# 系统日志来源模块标识（需求 18.4 记系统日志）。
_LOG_MODULE: str = "notify"

# 渠道推送 HTTP 超时（秒）：尽力发送，避免长时间阻塞推送主流程。
_SEND_TIMEOUT_SECONDS: float = 5.0


# ----------------------------------------------------------------------
# 序列化
# ----------------------------------------------------------------------
def serialize_channel(channel: NotifyChannel) -> Dict[str, Any]:
    """将通知渠道模型序列化为对外字典（时间为北京时间 ISO 串）。

    Args:
        channel: 通知渠道模型实例。

    Returns:
        通知渠道信息字典。
    """
    return {
        "id": channel.id,
        "shop_pk": channel.shop_pk,
        "channel_type": channel.channel_type,
        "target": channel.target,
        "enabled": bool(channel.enabled),
        "created_at": safe_isoformat(channel.created_at),
        "updated_at": safe_isoformat(channel.updated_at),
    }


def serialize_record(record: NotifyRecord) -> Dict[str, Any]:
    """将通知发送记录序列化为对外字典（时间为北京时间 ISO 串）。

    Args:
        record: 通知发送记录模型实例。

    Returns:
        通知记录信息字典。
    """
    return {
        "id": record.id,
        "shop_pk": record.shop_pk,
        "channel_id": record.channel_id,
        "event_type": record.event_type,
        "event_label": EVENT_TYPE_LABELS.get(record.event_type or "", record.event_type),
        "content": record.content,
        "send_result": record.send_result,
        "log_time": safe_isoformat(record.log_time),
    }


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------
def _write_system_log(session: Session, level: str, content: str) -> None:
    """写入一条系统日志（北京时间，禁止 debug 级别，规范 38）。

    Args:
        session: 数据库会话。
        level: 日志级别（info/warning/error）。
        content: 日志内容（中文）。
    """
    Repository(SystemLog, session).create(
        level=level,
        module=_LOG_MODULE,
        content=content,
        log_time=now_beijing_naive(),
    )


def send_via_channel(
    channel_type: str, target: Optional[str], content: str
) -> tuple[bool, str]:
    """经指定渠道实际发送通知，返回 (是否成功, 结果描述)。

    本函数为「实际投递」的统一入口，便于单元测试以打桩替换外部 IO：
    - webhook / wecom：以 HTTP POST 推送 JSON 文本到目标地址（urllib，短超时）；
    - email：当前阶段 SMTP 设置由系统设置模块（任务 8.2）提供，此处尚未接入，
      统一返回失败说明，由调用方记录失败不中断（需求 18.4）。

    本函数自身不抛网络异常（捕获后转为失败返回值），保证调用方推送主流程不被
    打断（需求 18.4 / 26）。

    Args:
        channel_type: 渠道类型（email/webhook/wecom）。
        target: 目标地址（邮箱 / Webhook URL 等）。
        content: 通知内容（中文）。

    Returns:
        二元组 (是否成功, 结果描述中文)。
    """
    if not target or not str(target).strip():
        return False, "通知目标地址未配置"

    if channel_type in ("webhook", "wecom"):
        # 企业微信群机器人 webhook 要求 msgtype/text 协议；通用 webhook 保持
        # 纯文本 {"content": ...} 兼容常见消息网关（2026-08-28 实测企微要求）。
        body: Dict[str, Any] = {"content": content}
        if channel_type == "wecom":
            body = {"msgtype": "text", "text": {"content": content}}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            str(target).strip(),
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=_SEND_TIMEOUT_SECONDS
            ) as resp:
                status = getattr(resp, "status", resp.getcode())
                if not (200 <= int(status) < 300):
                    return False, f"渠道返回非 2xx 状态码：{status}"
                # 企微等网关对业务失败（key 失效 / 消息格式错）仍返回 2xx，
                # 读取响应体 errcode 判别真实成败，避免落库误报成功（2026-08-28
                # 实测企微正常返回 {"errcode":0,"errmsg":"ok"}）。
                body_json: Optional[Dict[str, Any]] = None
                try:
                    body_json = json.loads(resp.read().decode("utf-8"))
                except (UnicodeDecodeError, ValueError, OSError):
                    body_json = None
                if isinstance(body_json, dict):
                    errcode = body_json.get("errcode")
                    if errcode is not None and int(errcode) != 0:
                        errmsg = body_json.get("errmsg")
                        return False, f"渠道返回业务错误：{errmsg or errcode}"
                return True, "发送成功"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # 网络不可达 / 超时 / 解析错误：转为失败返回，绝不向上抛出。
            return False, f"渠道发送失败：{exc}"

    if channel_type == "email":
        # SMTP 设置由系统设置模块（任务 8.2）提供，此处尚未接入。
        return False, "邮件渠道尚未配置 SMTP，暂无法发送"

    return False, f"不支持的渠道类型：{channel_type}"


def _record_send(
    session: Session,
    channel_id: Optional[int],
    event_type: Optional[str],
    content: str,
    ok: bool,
    detail: str,
    shop_pk: Optional[int] = None,
) -> NotifyRecord:
    """写入一条通知发送记录，并在失败时补记系统日志（需求 18.4）。

    Args:
        session: 数据库会话。
        channel_id: 通知渠道 ID（普通列，无外键）。
        event_type: 事件类型；测试发送时可为 None。
        content: 通知内容。
        ok: 是否发送成功。
        detail: 结果描述（失败原因或成功提示）。
        shop_pk: 归属店铺主键（店铺级渠道记录其归属，便于按店铺查询）。

    Returns:
        已持久化的通知发送记录实例。
    """
    record = Repository(NotifyRecord, session).create(
        shop_pk=shop_pk,
        channel_id=channel_id,
        event_type=event_type,
        content=content,
        send_result=SEND_RESULT_SUCCESS if ok else SEND_RESULT_FAILED,
        log_time=now_beijing_naive(),
    )
    if not ok:
        # 失败记日志不中断主流程（需求 18.4）。
        _write_system_log(
            session,
            level="warning",
            content=f"通知发送失败（渠道 {channel_id}）：{detail}",
        )
    return record


# ----------------------------------------------------------------------
# 数据范围隔离辅助（需求 3.7：非管理员仅可操作本人 / 被授权店铺）
# ----------------------------------------------------------------------
def _ensure_shop_in_scope(
    session: Session, shop_pk: Optional[int], operator_id: Optional[int]
) -> Optional[ApiResponse]:
    """校验店铺存在且在当前用户数据范围内，越权返回失败响应（需求 3.7）。

    管理员不受限；非管理员仅可操作本人创建或被授权的店铺。与 reply / filter /
    risk_control 等店铺级服务保持一致的隔离语义。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键 shop.id。
        operator_id: 操作人用户 ID；为空（如系统内部调用）时不做隔离校验。

    Returns:
        校验通过返回 None；店铺不存在或越权返回对应失败响应。
    """
    if operator_id is None:
        # 系统内部调用（如事件推送）无操作人上下文，按既有语义不做用户级隔离。
        return None
    if shop_pk is None:
        return error_response(CODE_PARAM_ERROR, "缺少归属店铺")
    shop = Repository(Shop, session).get(int(shop_pk))
    if shop is None:
        return error_response(CODE_NOT_FOUND, "店铺不存在")
    user = Repository(SysUser, session).get(operator_id)
    scope = build_data_scope(user, session=session)
    if not is_in_scope(scope, shop.owner_user_id):
        return error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)
    return None


# ----------------------------------------------------------------------
# 通知渠道配置（需求 18.1）
# ----------------------------------------------------------------------
def create_notify_channel(
    session: Session,
    channel_type: str,
    target: Optional[str],
    *,
    shop_pk: Optional[int] = None,
    enabled: bool = True,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """创建通知渠道并持久化（需求 18.1）。

    Args:
        session: 数据库会话。
        channel_type: 渠道类型（email/webhook/wecom）。
        target: 通知目标地址（邮箱 / Webhook URL 等）。
        shop_pk: 归属店铺主键（店铺级渠道必填）。
        enabled: 是否启用，默认启用。
        operator_id: 操作人用户 ID，作为创建人审计字段。

    Returns:
        统一响应体：成功返回 data=渠道信息；失败返回对应中文提示。
    """
    if channel_type not in ALLOWED_CHANNEL_TYPES:
        return error_response(
            CODE_PARAM_ERROR,
            f"渠道类型非法，可选值为 {list(ALLOWED_CHANNEL_TYPES)}",
        )
    if not target or not str(target).strip():
        return error_response(CODE_PARAM_ERROR, "通知目标地址不能为空")
    if shop_pk is None:
        return error_response(CODE_PARAM_ERROR, "缺少归属店铺")
    # 数据范围隔离：非管理员仅可为本人 / 被授权店铺配置渠道（需求 3.7）。
    denied = _ensure_shop_in_scope(session, shop_pk, operator_id)
    if denied is not None:
        return denied

    channel = Repository(NotifyChannel, session).create(
        shop_pk=int(shop_pk),
        channel_type=channel_type,
        target=str(target).strip(),
        enabled=bool(enabled),
        created_by=operator_id,
    )
    return success_response(
        data=serialize_channel(channel),
        message="创建成功",
    )


def update_notify_channel(
    session: Session,
    channel_id: int,
    *,
    channel_type: Optional[str] = None,
    target: Optional[str] = None,
    enabled: Optional[bool] = None,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """修改通知渠道字段（需求 18.1 配套）。

    仅更新显式提供（非 None）的字段。渠道类型显式提供时须为合法枚举。

    Args:
        session: 数据库会话。
        channel_id: 目标渠道 ID。
        channel_type: 新渠道类型；None 表示不修改。
        target: 新目标地址；None 表示不修改。
        enabled: 新启停用状态；None 表示不修改。
        operator_id: 操作人用户 ID，用于店铺归属数据范围隔离（需求 3.7）。

    Returns:
        统一响应体：成功返回更新后的渠道信息。
    """
    repo = Repository(NotifyChannel, session)
    channel = repo.get(channel_id)
    if channel is None:
        return error_response(CODE_NOT_FOUND, "目标通知渠道不存在")
    # 数据范围隔离：非管理员仅可操作本人 / 被授权店铺的渠道（需求 3.7）。
    denied = _ensure_shop_in_scope(session, channel.shop_pk, operator_id)
    if denied is not None:
        return denied

    values: Dict[str, Any] = {}
    if channel_type is not None:
        if channel_type not in ALLOWED_CHANNEL_TYPES:
            return error_response(
                CODE_PARAM_ERROR,
                f"渠道类型非法，可选值为 {list(ALLOWED_CHANNEL_TYPES)}",
            )
        values["channel_type"] = channel_type
    if target is not None:
        if not str(target).strip():
            return error_response(CODE_PARAM_ERROR, "通知目标地址不能为空")
        values["target"] = str(target).strip()
    if enabled is not None:
        values["enabled"] = bool(enabled)

    if not values:
        return error_response(CODE_PARAM_ERROR, "未提供任何待更新字段")

    repo.update(channel_id, **values)
    return success_response(
        data=serialize_channel(channel),
        message="更新成功",
    )


def list_notify_channels(
    session: Session,
    page: Any = 1,
    page_size: Any = 20,
    enabled: Optional[bool] = None,
    shop_pk: Optional[int] = None,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """分页查询通知渠道列表（需求 18.5 配套，后端分页）。

    默认按创建时间倒序（仓储层自动探测时间字段）。可按店铺与启用状态筛选。

    Args:
        session: 数据库会话。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        enabled: 按启用状态筛选；None 表示不筛选。
        shop_pk: 按归属店铺筛选；None 表示不筛选。
        operator_id: 操作人用户 ID，用于店铺归属数据范围隔离（需求 3.7）。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    # 数据范围隔离：按店铺查询时校验该店铺是否在当前用户可见范围内（需求 3.7）。
    if shop_pk is not None:
        denied = _ensure_shop_in_scope(session, shop_pk, operator_id)
        if denied is not None:
            return denied

    filters: Dict[str, Any] = {}
    if enabled is not None:
        filters["enabled"] = bool(enabled)
    if shop_pk is not None:
        filters["shop_pk"] = int(shop_pk)

    page_result = Repository(NotifyChannel, session).paginate(
        page=page, page_size=page_size, filters=filters or None
    )
    serialized: List[Dict[str, Any]] = [
        serialize_channel(channel) for channel in page_result.items
    ]
    data = {
        "list": serialized,
        "total": page_result.total,
        "page": page_result.page,
        "page_size": page_result.page_size,
    }
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 测试发送（需求 18.2 / 18.4）
# ----------------------------------------------------------------------
def test_notify_channel(
    session: Session,
    channel_id: int,
    content: Optional[str] = None,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """对某通知渠道发起测试发送并返回发送结果（需求 18.2）。

    经渠道实际投递（``send_via_channel``）并写入通知记录；发送失败时记通知失败
    日志，但仍以统一响应体返回失败原因，不抛异常中断（需求 18.4）。

    Args:
        session: 数据库会话。
        channel_id: 目标渠道 ID。
        content: 测试通知内容；None 时使用默认测试文案。
        operator_id: 操作人用户 ID，用于店铺归属数据范围隔离（需求 3.7）。

    Returns:
        统一响应体：发送成功返回 success=true；失败返回失败原因（HTTP 恒 200）。
    """
    channel = Repository(NotifyChannel, session).get(channel_id)
    if channel is None:
        return error_response(CODE_NOT_FOUND, "目标通知渠道不存在")
    # 数据范围隔离：非管理员仅可操作本人 / 被授权店铺的渠道（需求 3.7）。
    denied = _ensure_shop_in_scope(session, channel.shop_pk, operator_id)
    if denied is not None:
        return denied

    test_content = (content or "").strip() or "这是一条测试通知"
    ok, detail = send_via_channel(channel.channel_type, channel.target, test_content)
    record = _record_send(
        session,
        channel_id=channel.id,
        event_type=None,
        content=test_content,
        ok=ok,
        detail=detail,
        shop_pk=channel.shop_pk,
    )

    result_data = {
        "channel_id": channel.id,
        "send_result": record.send_result,
        "detail": detail,
    }
    if ok:
        return success_response(data=result_data, message="测试通知已发送")
    # 失败：返回失败原因，HTTP 恒 200，不中断（需求 18.4）。
    return error_response(CODE_EXTERNAL_ERROR, f"测试通知发送失败：{detail}")


# ----------------------------------------------------------------------
# 系统事件推送（需求 18.3 / 18.4）
# ----------------------------------------------------------------------
def push_system_event(
    session: Session,
    event_type: str,
    content: str,
    shop_pk: Optional[int] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """发生需通知的系统事件时，经该店铺全部已启用渠道推送通知（需求 18.3）。

    逐个已启用渠道发送，单个渠道失败仅记通知失败日志与系统日志，不中断对其它
    渠道的推送与主流程（需求 18.4）。即便全部失败或无可用渠道，本函数仍返回
    成功响应（推送动作已完成，失败明细已逐条记录），保证不打断触发事件的主流程。

    Args:
        session: 数据库会话。
        event_type: 事件类型（connection_disconnected/login_expired/risk_triggered）。
        content: 通知内容（中文）。
        shop_pk: 事件归属店铺主键；仅推送该店铺的已启用渠道（店铺级通知，方案 A）。
        operator_id: 操作人用户 ID；经由接口触发时做店铺归属数据范围隔离（需求 3.7）；
            系统内部调用（websocket / scheduler）传 None 表示不做用户级隔离。

    Returns:
        统一响应体：data 为推送统计 {total, success, failed}。
    """
    if event_type not in EVENT_TYPE_LABELS:
        return error_response(
            CODE_PARAM_ERROR,
            f"事件类型非法，可选值为 {list(EVENT_TYPE_LABELS)}",
        )

    # 数据范围隔离：经接口触发（operator_id 非空）时，仅可向本人 / 被授权店铺
    # 推送，避免非管理员向任意店铺触发事件推送（需求 3.7 / 规范 42a）。
    if operator_id is not None:
        denied = _ensure_shop_in_scope(session, shop_pk, operator_id)
        if denied is not None:
            return denied

    # 仅经「该店铺 + 已启用」渠道推送（需求 18.3，方案 A 店铺级隔离）。
    channel_filters: Dict[str, Any] = {"enabled": True}
    if shop_pk is not None:
        channel_filters["shop_pk"] = int(shop_pk)
    channels = Repository(NotifyChannel, session).list(
        filters=channel_filters, order_by=False
    )

    total = len(channels)
    success_count = 0
    failed_count = 0
    for channel in channels:
        # 单渠道发送以兜底方式执行，异常转为失败记录，绝不中断其它渠道（需求 18.4）。
        try:
            ok, detail = send_via_channel(
                channel.channel_type, channel.target, content
            )
        except Exception as exc:  # noqa: BLE001 兜底：任何异常均不得中断推送主流程
            ok, detail = False, f"渠道发送异常：{exc}"
            logger.warning("通知渠道发送异常（渠道 %s）：%s", channel.id, exc)
        _record_send(
            session,
            channel_id=channel.id,
            event_type=event_type,
            content=content,
            ok=ok,
            detail=detail,
            shop_pk=channel.shop_pk,
        )
        if ok:
            success_count += 1
        else:
            failed_count += 1

    data = {"total": total, "success": success_count, "failed": failed_count}
    return success_response(data=data, message="事件通知已推送")


# ----------------------------------------------------------------------
# 通知记录后端分页（需求 18.5）
# ----------------------------------------------------------------------
def list_notify_records(
    session: Session,
    page: Any = 1,
    page_size: Any = 20,
    channel_id: Optional[int] = None,
    event_type: Optional[str] = None,
    send_result: Optional[str] = None,
    shop_pk: Optional[int] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """分页查询通知记录（需求 18.5，后端分页）。

    按北京时间（created_at）倒序、后端分页（默认 20，可选 10/20/50/100）返回通知
    发送记录。可按店铺、渠道、事件类型、发送结果筛选。非管理员仅可见其可访问
    店铺下的通知记录（数据范围隔离，需求 3.7 / 规范 42a）。

    Args:
        session: 数据库会话。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        channel_id: 按通知渠道筛选；None 表示不筛选。
        event_type: 按事件类型筛选；None 表示不筛选。
        send_result: 按发送结果筛选（success/failed）；None 表示不筛选。
        shop_pk: 按归属店铺筛选；None 表示该用户可见范围内全部店铺。
        operator_id: 当前操作用户 ID（数据范围校验）。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    # 组装附加等值筛选条件（渠道 / 事件类型 / 发送结果）。
    extra_filters: Dict[str, Any] = {}
    if channel_id is not None:
        extra_filters["channel_id"] = int(channel_id)
    if event_type is not None:
        extra_filters["event_type"] = event_type
    if send_result is not None:
        extra_filters["send_result"] = send_result

    # 经共享辅助做带数据范围隔离的后端分页（管理员可见全部，非管理员限可见店铺）。
    return paginate_shop_scoped(
        session,
        model=NotifyRecord,
        serializer=serialize_record,
        page=page,
        page_size=page_size,
        operator_id=operator_id,
        shop_pk=int(shop_pk) if shop_pk is not None else None,
        extra_filters=extra_filters or None,
    )


# ----------------------------------------------------------------------
# 通知渠道类型字典（需求 18.x / 24.7：枚举入字典并前端中文展示）
# ----------------------------------------------------------------------
def list_channel_types(session: Session) -> ApiResponse:
    """查询「通知渠道类型」枚举字典，供前端展示中文文案（需求 18.x / 24.7）。

    从数据字典 ``channel_type`` 查出启用项（按 order_no 升序），返回 key 与中文
    标签列表，前端据此展示渠道类型中文文案。

    Args:
        session: 数据库会话。

    Returns:
        统一响应体：data 为渠道类型列表 [{key, label}, ...]。
    """
    items = DictService(session).list_by_type(DICT_CHANNEL_TYPE)
    channel_types: List[Dict[str, Any]] = [
        {"key": item.dict_key, "label": item.dict_label} for item in items
    ]
    return success_response(data=channel_types, message="查询成功")


__all__ = [
    "DICT_CHANNEL_TYPE",
    "ALLOWED_CHANNEL_TYPES",
    "EVENT_CONNECTION_DISCONNECTED",
    "EVENT_LOGIN_EXPIRED",
    "EVENT_RISK_TRIGGERED",
    "EVENT_TYPE_LABELS",
    "SEND_RESULT_SUCCESS",
    "SEND_RESULT_FAILED",
    "serialize_channel",
    "serialize_record",
    "send_via_channel",
    "create_notify_channel",
    "update_notify_channel",
    "list_notify_channels",
    "test_notify_channel",
    "push_system_event",
    "list_notify_records",
    "list_channel_types",
]
