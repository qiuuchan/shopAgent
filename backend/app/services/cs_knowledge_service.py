# -*- coding: utf-8 -*-
"""
backend.app.services.cs_knowledge_service —— 客服知识管理业务服务
==================================================================
本文件用途：实现 backend 服务的「客服知识库」业务逻辑，供 knowledge 路由复用，
满足需求 10（客服知识库管理）中的「配置管理」部分：

- ``create_cs_knowledge(...)``：新增客服知识（标题 / 内容 / 标签，需求 10.1）。
- ``update_cs_knowledge(...)``：按主键更新客服知识字段。
- ``get_cs_knowledge(...)``：查询单条客服知识。
- ``list_cs_knowledge(...)``：后端分页查询客服知识列表（需求 10.6）。
- ``set_cs_knowledge_status(...)`` / ``delete_cs_knowledge(...)``：启停用与逻辑
  删除（经状态字段 enabled，禁止物理删除，需求 24.6）。
- ``import_cs_knowledge(...)``：批量导入客服知识，跳过「同店铺内 (标题, 内容)
  完全相同」的重复项，返回成功数量与跳过数量（需求 10.2，Property 16）。

说明：需求 10.3 / 10.4 / 10.5 的「检索（kb.search）」由 common.services.kb_service
实现并供 websocket 复用，本文件只负责 backend 侧的客服知识 CRUD、分页与批量导入。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3）。
- 所有数据访问经 common.db.repository 的参数化查询，禁止拼接 SQL（规范 16）。
- 数据范围隔离复用 app.core.data_scope：非管理员仅能操作本人 / 被授权店铺下的
  客服知识（需求 3.7 / Property 8）。
- 禁止物理删除业务数据，停用 / 删除经状态字段 enabled 实现（规范 11 / 需求 24.6）。
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.business_codes import (
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    CODE_PARAM_ERROR,
    MSG_FORBIDDEN,
)
from app.core.data_scope import build_data_scope, is_in_scope
from common.db.repository import Repository
from common.models.knowledge_models import CustomerServiceKnowledge
from common.models.shop_models import Shop
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.services.kb_indexing import index_knowledge_content
from common.utils.time_utils import safe_isoformat

logger = logging.getLogger("backend.cs_knowledge")


def _index_cs_knowledge(session: Session, item: CustomerServiceKnowledge) -> None:
    """best-effort 为客服知识生成向量索引（POL-004，失败不阻断写操作）。

    组合标题 / 内容 / 标签为待向量化文本；embedding 未启用、缺密钥或网络失败
    均由 ``index_knowledge_content`` 内部吞掉并记 warning，绝不向上抛。此处再
    兜底一层 try/except，确保任何异常都不影响知识库写操作（best-effort）。
    """
    try:
        content_text = " ".join(
            part for part in (item.title, item.content, item.tags) if part
        )
        index_knowledge_content(
            session,
            shop_pk=item.shop_pk,
            source_type="cs_knowledge",
            source_id=item.id,
            content_text=content_text,
        )
    except Exception:  # noqa: BLE001 - 索引失败绝不阻断写操作，交由回填 CLI 补建
        logger.warning(
            "客服知识向量索引异常已被抑制（id=%s），交由回填 CLI 补建", item.id
        )


# ----------------------------------------------------------------------
# 序列化辅助
# ----------------------------------------------------------------------
def serialize_cs_knowledge(item: CustomerServiceKnowledge) -> Dict[str, Any]:
    """将客服知识模型序列化为对外字典。

    客服知识不含敏感字段，直接输出展示所需字段；时间字段为北京时间 ISO 串。

    Args:
        item: 客服知识模型实例。

    Returns:
        客服知识信息字典。
    """
    return {
        "id": item.id,
        "shop_pk": item.shop_pk,
        "title": item.title,
        "content": item.content,
        "tags": item.tags,
        "enabled": bool(item.enabled),
        "created_at": safe_isoformat(item.created_at),
        "updated_at": safe_isoformat(item.updated_at),
    }


# ----------------------------------------------------------------------
# 数据范围校验辅助
# ----------------------------------------------------------------------
def _resolve_shop_in_scope(
    session: Session, user: SysUser, shop_pk: int
) -> Tuple[Optional[Shop], Optional[ApiResponse]]:
    """校验店铺存在且在当前用户数据范围内，返回 (店铺, 失败响应)。

    依据需求 3.7 / Property 8：管理员不受限；非管理员仅可操作本人创建或被授权
    的店铺。店铺不存在返回 NOT_FOUND；越权返回「无访问权限」。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。

    Returns:
        二元组 (店铺实例, 失败响应)：校验通过时第二项为 None。
    """
    shop = Repository(Shop, session).get(shop_pk)
    if shop is None:
        return None, error_response(CODE_NOT_FOUND, "店铺不存在")
    scope = build_data_scope(user, session=session)
    if not is_in_scope(scope, shop.owner_user_id):
        return None, error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)
    return shop, None


def _validate_title_content(
    title: Optional[str], content: Optional[str], *, require_all: bool
) -> Optional[ApiResponse]:
    """校验标题与内容：必填或显式提供时不可为空白。

    Args:
        title: 知识标题。
        content: 知识内容。
        require_all: True 时两者必填（创建）；False 时仅校验已提供项（更新）。

    Returns:
        校验不通过时返回失败响应；通过返回 None。
    """
    if title is not None:
        if not title.strip():
            return error_response(CODE_PARAM_ERROR, "知识标题不能为空")
    elif require_all:
        return error_response(CODE_PARAM_ERROR, "知识标题不能为空")

    if content is not None:
        if not content.strip():
            return error_response(CODE_PARAM_ERROR, "知识内容不能为空")
    elif require_all:
        return error_response(CODE_PARAM_ERROR, "知识内容不能为空")
    return None


# ----------------------------------------------------------------------
# 新增（需求 10.1）
# ----------------------------------------------------------------------
def create_cs_knowledge(
    session: Session,
    user: SysUser,
    shop_pk: int,
    title: str,
    content: str,
    *,
    tags: Optional[str] = None,
    enabled: bool = True,
) -> ApiResponse:
    """新增客服知识（需求 10.1）。

    标题与内容不可为空；店铺归属经数据范围隔离校验。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。
        title: 知识标题。
        content: 知识内容。
        tags: 标签（逗号分隔），可空。
        enabled: 是否启用，默认 True。

    Returns:
        统一响应体：成功返回创建后的客服知识。
    """
    _, denied = _resolve_shop_in_scope(session, user, shop_pk)
    if denied is not None:
        return denied
    invalid = _validate_title_content(title, content, require_all=True)
    if invalid is not None:
        return invalid

    item = Repository(CustomerServiceKnowledge, session).create(
        shop_pk=shop_pk,
        title=title.strip(),
        content=content.strip(),
        tags=tags.strip() if tags else None,
        enabled=bool(enabled),
        created_by=user.id,
    )
    _index_cs_knowledge(session, item)
    return success_response(
        data=serialize_cs_knowledge(item), message="创建成功"
    )


# ----------------------------------------------------------------------
# 修改（按主键）
# ----------------------------------------------------------------------
def update_cs_knowledge(
    session: Session,
    user: SysUser,
    item_id: int,
    *,
    title: Optional[str] = None,
    content: Optional[str] = None,
    tags: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> ApiResponse:
    """按主键更新客服知识字段（需求 10.1 配套）。

    仅更新显式提供（非 None）的字段；店铺归属经数据范围隔离校验。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        item_id: 客服知识主键。
        title/content/tags/enabled: 各待更新字段，None 表示不修改。

    Returns:
        统一响应体：成功返回更新后的客服知识。
    """
    repo = Repository(CustomerServiceKnowledge, session)
    item = repo.get(item_id)
    if item is None:
        return error_response(CODE_NOT_FOUND, "客服知识不存在")
    _, denied = _resolve_shop_in_scope(session, user, item.shop_pk)
    if denied is not None:
        return denied
    invalid = _validate_title_content(title, content, require_all=False)
    if invalid is not None:
        return invalid

    values: Dict[str, Any] = {}
    if title is not None:
        values["title"] = title.strip()
    if content is not None:
        values["content"] = content.strip()
    if tags is not None:
        values["tags"] = tags.strip() if tags.strip() else None
    if enabled is not None:
        values["enabled"] = bool(enabled)

    if not values:
        return error_response(CODE_PARAM_ERROR, "未提供任何待更新字段")
    repo.update(item_id, **values)
    _index_cs_knowledge(session, item)
    return success_response(
        data=serialize_cs_knowledge(item), message="更新成功"
    )


# ----------------------------------------------------------------------
# 查询单条
# ----------------------------------------------------------------------
def get_cs_knowledge(
    session: Session, user: SysUser, item_id: int
) -> ApiResponse:
    """查询单条客服知识（受数据范围隔离约束）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        item_id: 客服知识主键。

    Returns:
        统一响应体：成功返回客服知识；不存在 / 越权返回失败。
    """
    item = Repository(CustomerServiceKnowledge, session).get(item_id)
    if item is None:
        return error_response(CODE_NOT_FOUND, "客服知识不存在")
    _, denied = _resolve_shop_in_scope(session, user, item.shop_pk)
    if denied is not None:
        return denied
    return success_response(
        data=serialize_cs_knowledge(item), message="查询成功"
    )


# ----------------------------------------------------------------------
# 列表（后端分页，需求 10.6）
# ----------------------------------------------------------------------
def list_cs_knowledge(
    session: Session,
    user: SysUser,
    shop_pk: int,
    page: Any = 1,
    page_size: Any = 20,
    enabled: Optional[bool] = None,
) -> ApiResponse:
    """后端分页查询某店铺的客服知识列表（需求 10.6）。

    默认按创建时间倒序（仓储层自动探测时间字段）。可按启停用筛选。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        enabled: 按启停用筛选；None 表示不筛选。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    _, denied = _resolve_shop_in_scope(session, user, shop_pk)
    if denied is not None:
        return denied

    filters: Dict[str, Any] = {"shop_pk": shop_pk}
    if enabled is not None:
        filters["enabled"] = bool(enabled)

    page_result = Repository(CustomerServiceKnowledge, session).paginate(
        page=page, page_size=page_size, filters=filters
    )
    serialized: List[Dict[str, Any]] = [
        serialize_cs_knowledge(item) for item in page_result.items
    ]
    data = {
        "list": serialized,
        "total": page_result.total,
        "page": page_result.page,
        "page_size": page_result.page_size,
    }
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 启停用与逻辑删除（经状态字段 enabled，禁止物理删除，需求 24.6）
# ----------------------------------------------------------------------
def set_cs_knowledge_status(
    session: Session, user: SysUser, item_id: int, enabled: bool
) -> ApiResponse:
    """启用 / 停用客服知识（停用项检索时不返回，需求 10.5）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        item_id: 客服知识主键。
        enabled: True=启用，False=停用。

    Returns:
        统一响应体：成功返回更新后的客服知识。
    """
    repo = Repository(CustomerServiceKnowledge, session)
    item = repo.get(item_id)
    if item is None:
        return error_response(CODE_NOT_FOUND, "客服知识不存在")
    _, denied = _resolve_shop_in_scope(session, user, item.shop_pk)
    if denied is not None:
        return denied
    repo.update(item_id, enabled=bool(enabled))
    # 重新启用时刷新向量（停用记录不参与检索，status 变更后重索引保证可用）。
    _index_cs_knowledge(session, item)
    message = "已启用" if enabled else "已停用"
    return success_response(
        data=serialize_cs_knowledge(item), message=message
    )


def delete_cs_knowledge(
    session: Session, user: SysUser, item_id: int
) -> ApiResponse:
    """逻辑删除客服知识（禁止物理删除，需求 24.6）。

    经状态字段 enabled=False 标记失效，记录保留、总数不变。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        item_id: 客服知识主键。

    Returns:
        统一响应体：成功返回 data=None。
    """
    repo = Repository(CustomerServiceKnowledge, session)
    item = repo.get(item_id)
    if item is None:
        return error_response(CODE_NOT_FOUND, "客服知识不存在")
    _, denied = _resolve_shop_in_scope(session, user, item.shop_pk)
    if denied is not None:
        return denied
    repo.soft_delete(item_id, field="enabled")
    return success_response(data=None, message="已删除")


# ----------------------------------------------------------------------
# 批量导入去重（需求 10.2，Property 16）
# ----------------------------------------------------------------------
def import_cs_knowledge(
    session: Session,
    user: SysUser,
    shop_pk: int,
    items: List[Dict[str, Any]],
) -> ApiResponse:
    """批量导入客服知识，跳过同店铺内 (标题, 内容) 完全相同的重复项（需求 10.2）。

    去重规则（Property 16）：以同店铺内 (title, content) 完全相同视为重复，
    与已存在记录重复或与本批次内已成功导入的项重复均跳过；返回成功数量与跳过
    数量。空标题 / 空内容的条目视为非法，计入跳过。导入项的店铺统一为
    ``shop_pk``，经数据范围隔离校验。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id（所有导入项归属此店铺）。
        items: 待导入条目列表，每项含 title / content / tags（可选）/ enabled（可选）。

    Returns:
        统一响应体：data={imported, skipped, total}（成功 / 跳过 / 总数）。
    """
    _, denied = _resolve_shop_in_scope(session, user, shop_pk)
    if denied is not None:
        return denied
    if not isinstance(items, list):
        return error_response(CODE_PARAM_ERROR, "导入数据格式非法")

    repo = Repository(CustomerServiceKnowledge, session)
    # 预载本店铺已有 (title, content) 去重集合（精确完全相同判定）。
    existing = repo.list(filters={"shop_pk": shop_pk}, order_by=False)
    seen_pairs: set[Tuple[str, str]] = {
        (rec.title, rec.content) for rec in existing
    }

    imported = 0
    skipped = 0
    total = len(items)
    created_items: list[CustomerServiceKnowledge] = []
    for raw in items:
        title = (raw.get("title") if isinstance(raw, dict) else None) or ""
        content = (raw.get("content") if isinstance(raw, dict) else None) or ""
        title = title.strip()
        content = content.strip()
        # 非法（空标题 / 空内容）计入跳过，不入库。
        if not title or not content:
            skipped += 1
            continue
        pair = (title, content)
        # 与已存在记录或本批次已导入项重复：跳过（需求 10.2）。
        if pair in seen_pairs:
            skipped += 1
            continue

        tags = raw.get("tags")
        enabled = raw.get("enabled", True)
        new_item = repo.create(
            shop_pk=shop_pk,
            title=title,
            content=content,
            tags=tags.strip() if isinstance(tags, str) and tags.strip() else None,
            enabled=bool(enabled),
            created_by=user.id,
        )
        created_items.append(new_item)
        seen_pairs.add(pair)
        imported += 1

    # 批量导入后 best-effort 补齐向量（POL-004；失败仅记日志不阻断导入）。
    for item in created_items:
        _index_cs_knowledge(session, item)

    data = {"imported": imported, "skipped": skipped, "total": total}
    return success_response(
        data=data, message=f"导入完成：成功 {imported} 条，跳过 {skipped} 条"
    )


__all__ = [
    "serialize_cs_knowledge",
    "create_cs_knowledge",
    "update_cs_knowledge",
    "get_cs_knowledge",
    "list_cs_knowledge",
    "set_cs_knowledge_status",
    "delete_cs_knowledge",
    "import_cs_knowledge",
]
