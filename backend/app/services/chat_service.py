# -*- coding: utf-8 -*-
"""
backend.app.services.chat_service —— 在线聊天会话与手动收发业务服务
==================================================================
本文件用途：实现 backend 服务的「在线聊天」业务逻辑，供 chat 路由复用，满足
需求 14（在线聊天）：

- ``list_conversations(...)``：会话列表（需求 14.1）。按最近消息时间（北京时间）
  倒序、后端分页（默认 20，可选 10/20/50/100），并做数据范围隔离（非管理员仅见
  本人 / 被授权店铺的会话，需求 14.1 / 3.7）。
- ``list_messages(...)``：某会话历史消息（需求 14.2）。按消息时间（北京时间）正序
  返回该会话的聊天记录，受数据范围隔离约束。
- ``send_manual_message(...)``：手动发送消息（需求 14.3）。经 HTTP 调用 websocket
  服务通过 WebSocket 将消息下发至对应客户，并记录消息日志（发送成功 / 失败均记）。
- ``new_message_hints(...)``：新消息提示数据（需求 14.4）。返回当前用户可见会话的
  未读消息汇总（按会话维度的未读数与总未读数），供前端在会话列表提示新消息。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3）。
- 所有数据访问经 common.db.repository 参数化查询，禁止拼接 SQL（规范 16）。
- 数据范围隔离统一经 app.core.data_scope（规范 42 集中判权 / 需求 14.1 / 3.7）。
- 手动发送经环境变量配置的服务地址 HTTP 调用 websocket，禁止写死 localhost
  （规范 21）；发送结果无论成败均记消息日志（需求 14.3 / 19.1）。
- 时间统一北京时间（规范 17 / 需求 24.8）；导入置顶（规范 51）；中文注释（规范 37）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.business_codes import (
    CODE_EXTERNAL_ERROR,
    CODE_NOT_FOUND,
    CODE_PARAM_ERROR,
)
from app.core.data_scope import (
    DataScope,
    build_data_scope,
    build_owner_condition,
    is_in_scope,
)
from app.services import chat_send_client, chat_sync_client, connection_notify
from common.db.repository import Repository
from common.models.log_models import ChatMessage, Conversation, MessageLog
from common.models.shop_models import Shop
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.utils.pagination import (
    build_page_result,
    calc_offset,
    normalize_pagination,
)
from common.utils.time_utils import now_beijing_naive, safe_isoformat, utc_to_beijing

# 手动发送消息的处理结果文案（消息日志 process_result，枚举入字典口径）。
_RESULT_MANUAL_SENT: str = "manual_sent"
_RESULT_MANUAL_FAILED: str = "manual_send_failed"

# 消息方向：发（out）/ 收（in）。
_DIRECTION_OUT: str = "out"


# ----------------------------------------------------------------------
# 序列化
# ----------------------------------------------------------------------
def serialize_conversation(conv: Conversation) -> Dict[str, Any]:
    """将会话模型序列化为对外字典（需求 14.1 / 14.4）。

    Args:
        conv: 会话模型实例。

    Returns:
        会话信息字典（含未读数与北京时间最近消息时间）。
    """
    return {
        "id": conv.id,
        "shop_pk": conv.shop_pk,
        "customer_uid": conv.customer_uid,
        "nickname": conv.nickname,
        "last_msg_at": safe_isoformat(conv.last_msg_at),
        "unread_count": conv.unread_count,
        "created_at": safe_isoformat(conv.created_at),
        "updated_at": safe_isoformat(conv.updated_at),
    }


def serialize_message(message: ChatMessage) -> Dict[str, Any]:
    """将聊天消息模型序列化为对外字典（需求 14.2，北京时间）。

    Args:
        message: 聊天消息模型实例。

    Returns:
        聊天消息字典（含订单 / 商品上下文与北京时间消息时间）。
    """
    return {
        "id": message.id,
        "shop_pk": message.shop_pk,
        "customer_uid": message.customer_uid,
        "direction": message.direction,
        "msg_type": message.msg_type,
        "content": message.content,
        "order_context": message.order_context,
        "goods_context": message.goods_context,
        "msg_time": safe_isoformat(message.msg_time),
        "created_at": safe_isoformat(message.created_at),
    }


# ----------------------------------------------------------------------
# 数据范围辅助
# ----------------------------------------------------------------------
def _allowed_shop_pks(
    session: Session, scope: DataScope, shop_pk: Optional[int] = None
) -> Tuple[Optional[List[int]], Optional[ApiResponse]]:
    """计算当前用户在数据范围内可见的店铺主键集合（需求 14.1 / 3.7）。

    管理员返回 (None, None) 表示不受店铺归属限制；非管理员据其可见归属用户集合
    解析出可见店铺主键列表（可能为空）。若显式传入 ``shop_pk``，则校验该店铺在
    范围内：越权返回 (None, 失败响应)，合法则返回仅含该店铺的列表。

    Args:
        session: 数据库会话。
        scope: 用户数据范围。
        shop_pk: 可选的指定店铺主键（仅看该店铺的会话）。

    Returns:
        二元组 (可见店铺主键列表或 None, 失败响应或 None)。
        - (None, None)：管理员且未指定店铺，不附加店铺限制；
        - ([...], None)：受限的可见店铺主键列表；
        - (None, 响应)：指定店铺越权 / 不存在。
    """
    # 指定了具体店铺：校验存在且在范围内。
    if shop_pk is not None:
        shop = Repository(Shop, session).get(shop_pk)
        if shop is None:
            return None, error_response(CODE_NOT_FOUND, "店铺不存在")
        if not is_in_scope(scope, shop.owner_user_id):
            return None, error_response(CODE_NOT_FOUND, "店铺不存在")
        return [shop.id], None

    allowed_owner_ids = scope.allowed_owner_ids()
    if allowed_owner_ids is None:
        # 管理员：不受店铺归属限制。
        return None, None

    if not allowed_owner_ids:
        # 非管理员且无任何可见归属：可见店铺为空。
        return [], None

    # 非管理员：解析可见归属用户名下的全部店铺主键。
    stmt = select(Shop.id).where(
        Shop.owner_user_id.in_(sorted(allowed_owner_ids))
    )
    shop_pks = [int(row) for row in session.execute(stmt).scalars().all()]
    return shop_pks, None


# ----------------------------------------------------------------------
# 会话列表（北京时间倒序 + 后端分页 + 数据范围隔离 —— 需求 14.1 / 3.7）
# ----------------------------------------------------------------------
def list_conversations(
    session: Session,
    user: SysUser,
    *,
    shop_pk: Optional[int] = None,
    page: Any = 1,
    page_size: Any = 20,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """分页查询当前用户有权访问店铺的会话列表（需求 14.1）。

    按最近消息时间（last_msg_at，北京时间）倒序、后端分页返回会话；受数据范围
    隔离约束（非管理员仅见本人 / 被授权店铺的会话，需求 14.1 / 3.7）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 可选的指定店铺主键（仅看该店铺会话）。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    scope = build_data_scope(
        user, session=session, authorized_owner_ids=authorized_owner_ids
    )
    allowed_pks, denied = _allowed_shop_pks(session, scope, shop_pk)
    if denied is not None:
        return denied

    norm_page, norm_size = normalize_pagination(page, page_size)

    # 非管理员且可见店铺为空：返回空分页（不可见任何会话，需求 14.1）。
    if allowed_pks is not None and not allowed_pks:
        data = {"list": [], "total": 0, "page": norm_page, "page_size": norm_size}
        return success_response(data=data, message="查询成功")

    # 组装查询条件：受限时按 shop_pk IN 下推隔离条件（参数化绑定）。
    conditions = []
    if allowed_pks is not None:
        conditions.append(Conversation.shop_pk.in_(allowed_pks))

    count_stmt = select(func.count()).select_from(Conversation)
    list_stmt = select(Conversation)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        list_stmt = list_stmt.where(*conditions)

    total = int(session.execute(count_stmt).scalar_one())
    offset = calc_offset(norm_page, norm_size)
    # 最近消息时间倒序，id 倒序作为稳定二级排序键。
    list_stmt = (
        list_stmt.order_by(Conversation.last_msg_at.desc(), Conversation.id.desc())
        .offset(offset)
        .limit(norm_size)
    )
    conversations = list(session.execute(list_stmt).scalars().all())

    page_result = build_page_result(
        items=conversations, total=total, page=norm_page, page_size=norm_size
    )
    data = {
        "list": [serialize_conversation(conv) for conv in conversations],
        "total": page_result.total,
        "page": page_result.page,
        "page_size": page_result.page_size,
    }
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 会话历史消息（北京时间正序 —— 需求 14.2）
# ----------------------------------------------------------------------
def list_messages(
    session: Session,
    user: SysUser,
    *,
    conversation_id: int,
    page: Any = 1,
    page_size: Any = 20,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """查询某会话的历史消息记录（需求 14.2，北京时间）。

    先校验会话存在且在当前用户数据范围内（需求 3.7），再按消息时间（msg_time）
    正序、后端分页返回该会话客户的聊天消息记录。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        conversation_id: 会话主键 conversation.id。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    conv, denied = _resolve_conversation_in_scope(
        session, user, conversation_id, authorized_owner_ids
    )
    if denied is not None:
        return denied

    norm_page, norm_size = normalize_pagination(page, page_size)
    conditions = [
        ChatMessage.shop_pk == conv.shop_pk,
        ChatMessage.customer_uid == conv.customer_uid,
    ]

    count_stmt = select(func.count()).select_from(ChatMessage).where(*conditions)
    total = int(session.execute(count_stmt).scalar_one())
    offset = calc_offset(norm_page, norm_size)
    # 历史消息按消息时间正序（旧→新），id 正序作为稳定二级排序键。
    list_stmt = (
        select(ChatMessage)
        .where(*conditions)
        .order_by(ChatMessage.msg_time.asc(), ChatMessage.id.asc())
        .offset(offset)
        .limit(norm_size)
    )
    messages = list(session.execute(list_stmt).scalars().all())

    data = {
        "list": [serialize_message(message) for message in messages],
        "total": total,
        "page": norm_page,
        "page_size": norm_size,
    }
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 手动发送消息（HTTP 调用 websocket 经 WebSocket 发送 + 记消息日志 —— 需求 14.3）
# ----------------------------------------------------------------------
def send_manual_message(
    session: Session,
    user: SysUser,
    *,
    conversation_id: int,
    content: str,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """在某会话中手动发送一条消息（需求 14.3）。

    流程：
    1. 数据范围隔离校验会话归属（需求 3.7）；
    2. 校验消息内容非空；
    3. 经 HTTP 调用 websocket 服务通过 WebSocket 下发至对应客户（需求 14.3）；
    4. 无论发送成功 / 失败均记录消息日志（需求 14.3 / 19.1）；
    5. 发送成功时追加一条「发」方向聊天消息并刷新会话最近消息时间。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        conversation_id: 会话主键 conversation.id。
        content: 待发送的消息内容。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：发送成功返回成功提示；失败返回外部依赖错误提示。
    """
    if not content or not str(content).strip():
        return error_response(CODE_PARAM_ERROR, "消息内容不能为空")

    conv, denied = _resolve_conversation_in_scope(
        session, user, conversation_id, authorized_owner_ids
    )
    if denied is not None:
        return denied

    shop = Repository(Shop, session).get(conv.shop_pk)
    if shop is None:
        return error_response(CODE_NOT_FOUND, "店铺不存在")

    content = str(content).strip()
    # 经 HTTP 调用 websocket 服务发送（地址经环境变量配置，禁止写死 localhost）。
    result = chat_send_client.send_manual_message(
        shop_pk=shop.id,
        shop_id=shop.shop_id,
        owner_user_id=shop.owner_user_id,
        recipient_uid=conv.customer_uid,
        content=content,
    )

    # 记录消息日志（发送成功 / 失败均记，需求 14.3 / 19.1）。
    _write_message_log(
        session,
        shop_pk=shop.id,
        customer_uid=conv.customer_uid,
        process_result=_RESULT_MANUAL_SENT if result.ok else _RESULT_MANUAL_FAILED,
        reply_content=content,
        message_content=None,
    )

    if not result.ok:
        return error_response(
            CODE_EXTERNAL_ERROR, result.message or "消息发送失败"
        )

    # 发送成功：追加一条「发」方向聊天消息并刷新会话最近消息时间。
    now = now_beijing_naive()
    Repository(ChatMessage, session).create(
        shop_pk=shop.id,
        customer_uid=conv.customer_uid,
        direction=_DIRECTION_OUT,
        msg_type="text",
        content=content,
        msg_time=now,
    )
    Repository(Conversation, session).update(conv.id, last_msg_at=now)

    return success_response(data={"sent": True}, message="发送成功")


# ----------------------------------------------------------------------
# 新消息提示数据（未读汇总 —— 需求 14.4）
# ----------------------------------------------------------------------
def new_message_hints(
    session: Session,
    user: SysUser,
    *,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """返回当前用户可见会话的新消息提示数据（需求 14.4）。

    汇总各会话未读数与总未读数，供前端在会话列表提示有新消息。受数据范围隔离
    约束（非管理员仅统计本人 / 被授权店铺的会话）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：data 为 {total_unread, conversations:[{id, shop_pk,
        customer_uid, unread_count}]}。
    """
    scope = build_data_scope(
        user, session=session, authorized_owner_ids=authorized_owner_ids
    )
    allowed_pks, _ = _allowed_shop_pks(session, scope)

    # 仅统计有未读的会话（unread_count > 0）。
    conditions = [Conversation.unread_count > 0]
    if allowed_pks is not None:
        if not allowed_pks:
            data = {"total_unread": 0, "conversations": []}
            return success_response(data=data, message="查询成功")
        conditions.append(Conversation.shop_pk.in_(allowed_pks))

    stmt = (
        select(Conversation)
        .where(*conditions)
        .order_by(Conversation.last_msg_at.desc(), Conversation.id.desc())
    )
    conversations = list(session.execute(stmt).scalars().all())

    hints = [
        {
            "id": conv.id,
            "shop_pk": conv.shop_pk,
            "customer_uid": conv.customer_uid,
            "unread_count": conv.unread_count,
        }
        for conv in conversations
    ]
    total_unread = sum(int(conv.unread_count or 0) for conv in conversations)
    data = {"total_unread": total_unread, "conversations": hints}
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------
def _resolve_conversation_in_scope(
    session: Session,
    user: SysUser,
    conversation_id: int,
    authorized_owner_ids: Optional[List[int]],
) -> Tuple[Optional[Conversation], Optional[ApiResponse]]:
    """校验会话存在且在当前用户数据范围内，返回 (会话, 失败响应)。

    会话归属由其所属店铺的归属用户决定（需求 3.7）。会话不存在 / 店铺越权统一
    返回「会话不存在」，避免泄露其它用户会话的存在性。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        conversation_id: 会话主键。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        二元组 (会话实例, 失败响应)：校验通过时第二项为 None。
    """
    conv = Repository(Conversation, session).get(conversation_id)
    if conv is None:
        return None, error_response(CODE_NOT_FOUND, "会话不存在")
    shop = Repository(Shop, session).get(conv.shop_pk)
    if shop is None:
        return None, error_response(CODE_NOT_FOUND, "会话不存在")
    scope = build_data_scope(
        user, session=session, authorized_owner_ids=authorized_owner_ids
    )
    if not is_in_scope(scope, shop.owner_user_id):
        return None, error_response(CODE_NOT_FOUND, "会话不存在")
    return conv, None


# ----------------------------------------------------------------------
# 在线聊天店铺列表 + 连接管理（参照闲鱼版「账号列表」：多店铺可同时连接）
# ----------------------------------------------------------------------
# 店铺启用状态值（与 common.models.shop_models.Shop.status 约定一致：1=启用）。
_SHOP_STATUS_ENABLED: int = 1


def list_chat_shops(
    session: Session,
    user: SysUser,
    *,
    page: Any = 1,
    page_size: Any = 20,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """在线聊天「店铺列表」：分页返回当前用户可见店铺及其连接状态（参照闲鱼版账号列表）。

    支持多店铺同时连接：列表每项附带 ``connected`` 实时连接状态（经 websocket 查询），
    前端据此展示「已连接 / 未连接」并提供连接 / 断开操作。受数据范围隔离约束。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        page: 页码（从 1 开始）。
        page_size: 每页条数（10/20/50/100）。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：data 为分页结构 {list:[{shop_pk, shop_id, shop_name, owner,
        connected, status}], total, page, page_size}。
    """
    scope = build_data_scope(
        user, session=session, authorized_owner_ids=authorized_owner_ids
    )
    norm_page, norm_size = normalize_pagination(page, page_size)

    # 仅展示「已启用」店铺（停用店铺连接已断开，不可在线聊天）。
    conditions = [Shop.status == _SHOP_STATUS_ENABLED]
    owner_cond = build_owner_condition(scope, Shop.owner_user_id)
    if owner_cond is not None:
        conditions.append(owner_cond)

    count_stmt = select(func.count()).select_from(Shop).where(*conditions)
    total = int(session.execute(count_stmt).scalar_one())
    offset = calc_offset(norm_page, norm_size)
    list_stmt = (
        select(Shop)
        .where(*conditions)
        .order_by(Shop.created_at.desc(), Shop.id.desc())
        .offset(offset)
        .limit(norm_size)
    )
    shops = list(session.execute(list_stmt).scalars().all())

    # 批量查询连接状态（一次 HTTP，避免逐店铺串行查询；websocket 不可达时降级为
    # 全部未连接，不影响列表展示）。
    status_map = connection_notify.query_connected_batch(
        [(shop.shop_id, shop.owner_user_id) for shop in shops]
    )

    items: List[Dict[str, Any]] = []
    for shop in shops:
        items.append(
            {
                "shop_pk": shop.id,
                "shop_id": shop.shop_id,
                "shop_name": shop.shop_name or shop.shop_id,
                "connected": bool(status_map.get(str(shop.shop_id), False)),
                "status": shop.status,
            }
        )

    data = {
        "list": items,
        "total": total,
        "page": norm_page,
        "page_size": norm_size,
    }
    return success_response(data=data, message="查询成功")


def connect_shop(
    session: Session,
    user: SysUser,
    *,
    shop_pk: int,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """连接指定店铺的拼多多长连接（在线聊天，参照闲鱼版「连接账号」）。

    经 websocket 服务启动该店铺连接；多店铺可各自独立连接互不影响。受数据范围隔离。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：连接成功返回成功；失败返回外部依赖错误提示。
    """
    shop, denied = _resolve_shop_in_scope_for_chat(
        session, user, shop_pk, authorized_owner_ids
    )
    if denied is not None:
        return denied

    ok = connection_notify.notify_connect(
        shop_pk=shop.id,
        shop_id=shop.shop_id,
        owner_user_id=shop.owner_user_id,
        proxy_server=shop.proxy_server,
    )
    if not ok:
        return error_response(CODE_EXTERNAL_ERROR, "连接失败，请稍后重试")
    return success_response(data={"connected": True}, message="连接成功")


def disconnect_shop(
    session: Session,
    user: SysUser,
    *,
    shop_pk: int,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """断开指定店铺的拼多多长连接（在线聊天，参照闲鱼版「断开账号」）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：断开成功返回成功；失败返回外部依赖错误提示。
    """
    shop, denied = _resolve_shop_in_scope_for_chat(
        session, user, shop_pk, authorized_owner_ids
    )
    if denied is not None:
        return denied

    ok = connection_notify.notify_disconnect(
        shop_pk=shop.id, shop_id=shop.shop_id, owner_user_id=shop.owner_user_id
    )
    if not ok:
        return error_response(CODE_EXTERNAL_ERROR, "断开失败，请稍后重试")
    return success_response(data={"connected": False}, message="已断开连接")


def send_message_by_uid(
    session: Session,
    user: SysUser,
    *,
    shop_pk: int,
    customer_uid: str,
    content: str,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """按 (店铺, 客户 uid) 手动发送消息（在线聊天实时会话直接发送，需求 14.3）。

    不依赖本地会话主键：直接按拼多多店铺 + 客户 uid 经 websocket 下发，适配「会话
    列表为实时数据」的场景。发送成功 / 失败均记消息日志（需求 14.3 / 19.1）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。
        customer_uid: 客户唯一标识。
        content: 待发送的消息内容。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：发送成功返回成功；失败返回外部依赖错误提示。
    """
    if not content or not str(content).strip():
        return error_response(CODE_PARAM_ERROR, "消息内容不能为空")
    if not customer_uid or not str(customer_uid).strip():
        return error_response(CODE_PARAM_ERROR, "客户标识不能为空")

    shop, denied = _resolve_shop_in_scope_for_chat(
        session, user, shop_pk, authorized_owner_ids
    )
    if denied is not None:
        return denied

    content = str(content).strip()
    customer_uid = str(customer_uid).strip()
    result = chat_send_client.send_manual_message(
        shop_pk=shop.id,
        shop_id=shop.shop_id,
        owner_user_id=shop.owner_user_id,
        recipient_uid=customer_uid,
        content=content,
    )

    # 记录消息日志（发送成功 / 失败均记，需求 14.3 / 19.1）。
    _write_message_log(
        session,
        shop_pk=shop.id,
        customer_uid=customer_uid,
        process_result=_RESULT_MANUAL_SENT if result.ok else _RESULT_MANUAL_FAILED,
        reply_content=content,
        message_content=None,
    )

    if not result.ok:
        return error_response(CODE_EXTERNAL_ERROR, result.message or "消息发送失败")
    return success_response(data={"sent": True}, message="发送成功")


# ----------------------------------------------------------------------
# 实时同步：会话列表 / 历史记录（方案 A —— 实时调拼多多接口 + 落库）
# ----------------------------------------------------------------------
def sync_conversations(
    session: Session,
    user: SysUser,
    *,
    shop_pk: int,
    fetch_all: bool = False,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """实时拉取指定店铺的拼多多会话列表（方案 A，需求 14.1）。

    经 websocket 服务实时调拼多多 ``latest_conversations`` 拉取该店铺当前会话列表，
    直接返回拼多多侧的会话（含未读数 / 最近消息 / 客户信息），不依赖本地表。受数据
    范围隔离约束：非管理员仅可同步本人 / 被授权店铺。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id（必填，按店铺维度实时拉取）。
        fetch_all: 是否翻页拉取全部会话（默认仅首页）。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：data 为 {shop_pk, conversations:[...拼多多侧规整后...]}。
    """
    shop, denied = _resolve_shop_in_scope_for_chat(
        session, user, shop_pk, authorized_owner_ids
    )
    if denied is not None:
        return denied

    result = chat_sync_client.fetch_conversations(
        shop_id=shop.shop_id,
        owner_user_id=shop.owner_user_id,
        fetch_all=fetch_all,
    )
    if not result.ok:
        return error_response(CODE_EXTERNAL_ERROR, result.message or "获取会话列表失败")

    # 落库（后台留存）：将拼多多侧会话 upsert 进本地会话表，按 (shop_pk, customer_uid)
    # 幂等刷新昵称 / 未读数 / 最近消息时间，供数据统计与历史留存使用。
    # 但「聊天界面展示」只用下方返回的实时数据（result.items），不读本地库混合展示。
    _persist_synced_conversations(session, shop.id, result.items)

    # 会话列表展示「纯实时」：直接透传拼多多侧实时返回的会话（不与本地库混合）。
    # 附带本地店铺主键，供前端后续按 (shop_pk, customer_uid) 拉取历史 / 手动发送。
    data = {"shop_pk": shop.id, "conversations": result.items}
    return success_response(data=data, message="同步成功")


def _persist_synced_conversations(
    session: Session, shop_pk: int, conversations: List[Dict[str, Any]]
) -> int:
    """将拼多多侧会话列表 upsert 进本地会话表（落库留存，幂等），返回处理条数。

    仅用于后台留存（数据统计 / 历史归档），不参与聊天界面会话列表展示。按
    (shop_pk, customer_uid) upsert：刷新昵称、未读数与最近消息时间（北京时间）。
    最近消息时间由拼多多 ``last_ts``（秒级）换算为北京时间 naive。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键。
        conversations: 拼多多侧规整后的会话列表。

    Returns:
        实际 upsert 的会话条数。
    """
    repo = Repository(Conversation, session)
    count = 0
    for conv in conversations:
        customer_uid = conv.get("customer_uid")
        if not customer_uid:
            continue
        values: Dict[str, Any] = {}
        if conv.get("nickname"):
            values["nickname"] = conv.get("nickname")
        unread = conv.get("unread")
        if unread is not None:
            values["unread_count"] = int(unread)
        last_ts = conv.get("last_ts")
        if last_ts:
            try:
                # 拼多多 last_ts 为秒级 epoch（UTC）；换算为北京时间 naive 落库
                # （规范 17，避免依赖服务器本地时区）。
                values["last_msg_at"] = utc_to_beijing(
                    datetime.utcfromtimestamp(int(last_ts))
                ).replace(tzinfo=None)
            except (TypeError, ValueError, OSError):
                pass
        repo.upsert(
            biz_keys={"shop_pk": shop_pk, "customer_uid": str(customer_uid)},
            values=values,
        )
        count += 1
    return count


def sync_history(
    session: Session,
    user: SysUser,
    *,
    shop_pk: int,
    customer_uid: str,
    authorized_owner_ids: Optional[List[int]] = None,
) -> ApiResponse:
    """实时拉取某客户会话的全部历史聊天记录并落库（方案 A，需求 14.2 / 17）。

    经 websocket 服务循环翻页实时调拼多多 ``chat/list`` 拉取该客户会话「接口支持
    范围内」的全部历史消息，并由 websocket 侧按 (shop_pk, customer_uid, msg_id) 去重
    落库到本地 ``conversation`` / ``chat_message`` 表。受数据范围隔离约束。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。
        customer_uid: 客户唯一标识。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        统一响应体：data 为 {messages:[...], persisted: n}。
    """
    if not customer_uid or not str(customer_uid).strip():
        return error_response(CODE_PARAM_ERROR, "客户标识不能为空")

    shop, denied = _resolve_shop_in_scope_for_chat(
        session, user, shop_pk, authorized_owner_ids
    )
    if denied is not None:
        return denied

    result = chat_sync_client.fetch_history(
        shop_pk=shop.id,
        shop_id=shop.shop_id,
        owner_user_id=shop.owner_user_id,
        customer_uid=str(customer_uid).strip(),
        persist=True,
    )
    if not result.ok:
        return error_response(CODE_EXTERNAL_ERROR, result.message or "获取聊天记录失败")

    # 落库后本地会话已存在（由 websocket 侧 upsert）：回查本地会话主键，便于前端
    # 复用既有「按 conversation_id 手动发送 / 查看上下文」的接口（需求 14.3 / 17）。
    conversation = Repository(Conversation, session).get_by(
        shop_pk=shop.id, customer_uid=str(customer_uid).strip()
    )
    data = {
        "conversation_id": conversation.id if conversation is not None else None,
        "messages": result.items,
        "persisted": result.persisted,
    }
    return success_response(data=data, message="同步成功")


def _resolve_shop_in_scope_for_chat(
    session: Session,
    user: SysUser,
    shop_pk: int,
    authorized_owner_ids: Optional[List[int]],
) -> Tuple[Optional[Shop], Optional[ApiResponse]]:
    """校验店铺存在且在当前用户数据范围内，返回 (店铺, 失败响应)。

    用于实时同步接口：店铺不存在 / 越权统一返回「店铺不存在」，避免泄露存在性。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键。
        authorized_owner_ids: 被显式授权可见的归属用户 ID 列表。

    Returns:
        二元组 (店铺实例, 失败响应)：校验通过时第二项为 None。
    """
    shop = Repository(Shop, session).get(shop_pk)
    if shop is None:
        return None, error_response(CODE_NOT_FOUND, "店铺不存在")
    scope = build_data_scope(
        user, session=session, authorized_owner_ids=authorized_owner_ids
    )
    if not is_in_scope(scope, shop.owner_user_id):
        return None, error_response(CODE_NOT_FOUND, "店铺不存在")
    return shop, None


def _write_message_log(
    session: Session,
    *,
    shop_pk: int,
    customer_uid: Optional[str],
    process_result: str,
    reply_content: Optional[str],
    message_content: Optional[str],
) -> None:
    """写入一条消息日志（北京时间，禁止物理删除，需求 19.1 / 19.5）。

    Args:
        session: 数据库会话。
        shop_pk: 关联店铺主键。
        customer_uid: 客户唯一标识。
        process_result: 处理结果（枚举入字典口径）。
        reply_content: 回复 / 发送内容。
        message_content: 原始消息内容。
    """
    Repository(MessageLog, session).create(
        shop_pk=shop_pk,
        customer_uid=customer_uid,
        message_content=message_content,
        process_result=process_result,
        reply_content=reply_content,
        log_time=now_beijing_naive(),
    )


__all__ = [
    "serialize_conversation",
    "serialize_message",
    "list_conversations",
    "list_messages",
    "send_manual_message",
    "new_message_hints",
    "sync_conversations",
    "sync_history",
    "list_chat_shops",
    "connect_shop",
    "disconnect_shop",
    "send_message_by_uid",
]
