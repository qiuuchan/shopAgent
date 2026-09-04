# -*- coding: utf-8 -*-
"""
backend.app.services.product_knowledge_service —— 商品知识管理业务服务
======================================================================
本文件用途：实现 backend 服务的「商品知识库」业务逻辑，供 knowledge 路由复用，
满足需求 9（商品知识库管理）中的「配置管理」部分：

- ``upsert_product_knowledge(...)``：按 (shop_pk, goods_id) upsert 幂等保存商品
  知识（需求 9.2），相同业务键多次写入记录数恒为 1 且内容为最后一次写入。
- ``update_product_knowledge(...)``：按主键更新商品知识字段。
- ``get_product_knowledge(...)``：查询单条商品知识。
- ``list_product_knowledge(...)``：后端分页查询商品知识列表（需求 9.3）。
- ``set_product_knowledge_status(...)`` / ``delete_product_knowledge(...)``：
  启停用与逻辑删除（经状态字段 status，禁止物理删除，需求 9.5 / 24.6）。

说明：需求 9.4 的「检索（kb.search）」由 common.services.kb_service 实现并供
websocket 复用，本文件只负责 backend 侧的商品知识 CRUD、查询与分页。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3）。
- 所有数据访问经 common.db.repository 的参数化查询，禁止拼接 SQL（规范 16）。
- 数据范围隔离复用 app.core.data_scope：非管理员仅能操作本人 / 被授权店铺下的
  商品知识（需求 3.7 / Property 8）。
- 禁止物理删除业务数据，停用 / 删除经状态字段 status 实现（规范 11 / 需求 24.6）。
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
from common.models.knowledge_models import ProductKnowledge
from common.models.shop_models import Shop
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.services.kb_indexing import index_knowledge_content
from common.utils.time_utils import safe_isoformat

logger = logging.getLogger("backend.product_knowledge")


def _index_product_knowledge(session: Session, item: ProductKnowledge) -> None:
    """best-effort 为商品知识生成向量索引（POL-004，失败不阻断写操作）。

    商品知识的向量化文本取自抽取内容（extracted_content），其次为规格 / 商品
    名称。参数化调用内部静默处理 embedding 未启用 / 缺密钥 / 网络失败；此处再
    兜底一层 try/except，确保任何异常都不影响知识库写操作（best-effort）。
    """
    try:
        content_text = item.extracted_content or ""
        if not content_text.strip():
            content_text = " ".join(
                part for part in (item.goods_name, item.specifications) if part
            )
        if not content_text.strip():
            return
        index_knowledge_content(
            session,
            shop_pk=item.shop_pk,
            source_type="product_knowledge",
            source_id=item.id,
            content_text=content_text,
        )
    except Exception:  # noqa: BLE001 - 索引失败绝不阻断写操作，交由回填 CLI 补建
        logger.warning(
            "商品知识向量索引异常已被抑制（id=%s），交由回填 CLI 补建", item.id
        )


# ----------------------------------------------------------------------
# 序列化辅助
# ----------------------------------------------------------------------
def _to_float(value: Any) -> Optional[float]:
    """将数值（含 Decimal）安全转换为 float；None 原样返回。

    商品知识价格字段为 Numeric（Decimal），序列化为前端友好的 float。

    Args:
        value: 待转换的数值或 None。

    Returns:
        转换后的 float；输入为 None 时返回 None。
    """
    if value is None:
        return None
    return float(value)


def serialize_product_knowledge(item: ProductKnowledge) -> Dict[str, Any]:
    """将商品知识模型序列化为对外字典。

    商品知识不含敏感字段，直接输出展示所需字段；时间字段为北京时间 ISO 串。

    Args:
        item: 商品知识模型实例。

    Returns:
        商品知识信息字典。
    """
    return {
        "id": item.id,
        "shop_pk": item.shop_pk,
        "goods_id": item.goods_id,
        "goods_name": item.goods_name,
        "price": _to_float(item.price),
        "price_min": _to_float(item.price_min),
        "price_max": _to_float(item.price_max),
        "sold_quantity": item.sold_quantity,
        "thumb_url": item.thumb_url,
        "specifications": item.specifications,
        "extracted_content": item.extracted_content,
        "last_extracted_at": safe_isoformat(item.last_extracted_at),
        "status": item.status,
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


# ----------------------------------------------------------------------
# 新增 / upsert（需求 9.1 / 9.2：按 (shop_pk, goods_id) 幂等）
# ----------------------------------------------------------------------
def upsert_product_knowledge(
    session: Session,
    user: SysUser,
    shop_pk: int,
    goods_id: str,
    *,
    goods_name: Optional[str] = None,
    price: Optional[float] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    sold_quantity: Optional[int] = None,
    thumb_url: Optional[str] = None,
    specifications: Optional[str] = None,
    extracted_content: Optional[str] = None,
) -> ApiResponse:
    """新增 / upsert 商品知识（按 (shop_pk, goods_id) 幂等，需求 9.1 / 9.2）。

    同 (shop_pk, goods_id) 多次写入记录数恒为 1 且内容为最后一次写入。goods_id
    不可为空；店铺归属经数据范围隔离校验。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。
        goods_id: 拼多多商品业务标识（业务键一部分）。
        goods_name: 商品名称。
        price/price_min/price_max: 价格相关字段。
        sold_quantity: 已售数量。
        thumb_url: 缩略图 URL。
        specifications: 商品规格（JSON 文本）。
        extracted_content: 抽取内容（供 AI 检索）。

    Returns:
        统一响应体：成功返回保存后的商品知识。
    """
    _, denied = _resolve_shop_in_scope(session, user, shop_pk)
    if denied is not None:
        return denied
    if not goods_id or not goods_id.strip():
        return error_response(CODE_PARAM_ERROR, "商品 goods_id 不能为空")

    # 仅写入显式提供的非键字段，避免 upsert 命中时把未提供字段覆盖为 None。
    values: Dict[str, Any] = {"created_by": user.id}
    if goods_name is not None:
        values["goods_name"] = goods_name
    if price is not None:
        values["price"] = price
    if price_min is not None:
        values["price_min"] = price_min
    if price_max is not None:
        values["price_max"] = price_max
    if sold_quantity is not None:
        values["sold_quantity"] = int(sold_quantity)
    if thumb_url is not None:
        values["thumb_url"] = thumb_url
    if specifications is not None:
        values["specifications"] = specifications
    if extracted_content is not None:
        values["extracted_content"] = extracted_content

    item = Repository(ProductKnowledge, session).upsert(
        biz_keys={"shop_pk": shop_pk, "goods_id": goods_id.strip()},
        values=values,
    )
    _index_product_knowledge(session, item)
    return success_response(
        data=serialize_product_knowledge(item), message="保存成功"
    )


# ----------------------------------------------------------------------
# 修改（按主键）
# ----------------------------------------------------------------------
def update_product_knowledge(
    session: Session,
    user: SysUser,
    item_id: int,
    *,
    goods_name: Optional[str] = None,
    price: Optional[float] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    sold_quantity: Optional[int] = None,
    thumb_url: Optional[str] = None,
    specifications: Optional[str] = None,
    extracted_content: Optional[str] = None,
) -> ApiResponse:
    """按主键更新商品知识字段（需求 9.1 配套）。

    仅更新显式提供（非 None）的字段；店铺归属经数据范围隔离校验。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        item_id: 商品知识主键。
        其余: 各待更新字段，None 表示不修改。

    Returns:
        统一响应体：成功返回更新后的商品知识。
    """
    repo = Repository(ProductKnowledge, session)
    item = repo.get(item_id)
    if item is None:
        return error_response(CODE_NOT_FOUND, "商品知识不存在")
    _, denied = _resolve_shop_in_scope(session, user, item.shop_pk)
    if denied is not None:
        return denied

    values: Dict[str, Any] = {}
    if goods_name is not None:
        values["goods_name"] = goods_name
    if price is not None:
        values["price"] = price
    if price_min is not None:
        values["price_min"] = price_min
    if price_max is not None:
        values["price_max"] = price_max
    if sold_quantity is not None:
        values["sold_quantity"] = int(sold_quantity)
    if thumb_url is not None:
        values["thumb_url"] = thumb_url
    if specifications is not None:
        values["specifications"] = specifications
    if extracted_content is not None:
        values["extracted_content"] = extracted_content

    if not values:
        return error_response(CODE_PARAM_ERROR, "未提供任何待更新字段")
    repo.update(item_id, **values)
    _index_product_knowledge(session, item)
    return success_response(
        data=serialize_product_knowledge(item), message="更新成功"
    )


# ----------------------------------------------------------------------
# 查询单条
# ----------------------------------------------------------------------
def get_product_knowledge(
    session: Session, user: SysUser, item_id: int
) -> ApiResponse:
    """查询单条商品知识（受数据范围隔离约束）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        item_id: 商品知识主键。

    Returns:
        统一响应体：成功返回商品知识；不存在 / 越权返回失败。
    """
    item = Repository(ProductKnowledge, session).get(item_id)
    if item is None:
        return error_response(CODE_NOT_FOUND, "商品知识不存在")
    _, denied = _resolve_shop_in_scope(session, user, item.shop_pk)
    if denied is not None:
        return denied
    return success_response(
        data=serialize_product_knowledge(item), message="查询成功"
    )


# ----------------------------------------------------------------------
# 列表（后端分页，需求 9.3）
# ----------------------------------------------------------------------
def list_product_knowledge(
    session: Session,
    user: SysUser,
    shop_pk: int,
    page: Any = 1,
    page_size: Any = 20,
    goods_id: Optional[str] = None,
    status: Optional[int] = None,
) -> ApiResponse:
    """后端分页查询某店铺的商品知识列表（需求 9.3）。

    默认按创建时间倒序（仓储层自动探测时间字段）。可按 goods_id 与状态筛选。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        shop_pk: 店铺主键 shop.id。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        goods_id: 按商品标识筛选；None 表示不筛选。
        status: 按状态筛选（1=启用，0=停用）；None 表示不筛选。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    _, denied = _resolve_shop_in_scope(session, user, shop_pk)
    if denied is not None:
        return denied

    filters: Dict[str, Any] = {"shop_pk": shop_pk}
    if goods_id is not None and goods_id.strip():
        filters["goods_id"] = goods_id.strip()
    if status is not None:
        filters["status"] = int(status)

    page_result = Repository(ProductKnowledge, session).paginate(
        page=page, page_size=page_size, filters=filters
    )
    serialized: List[Dict[str, Any]] = [
        serialize_product_knowledge(item) for item in page_result.items
    ]
    data = {
        "list": serialized,
        "total": page_result.total,
        "page": page_result.page,
        "page_size": page_result.page_size,
    }
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 启停用与逻辑删除（经状态字段 status，禁止物理删除，需求 9.5 / 24.6）
# ----------------------------------------------------------------------
def set_product_knowledge_status(
    session: Session, user: SysUser, item_id: int, status: int
) -> ApiResponse:
    """启用 / 停用商品知识（经状态字段 status）。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        item_id: 商品知识主键。
        status: 1=启用，0=停用。

    Returns:
        统一响应体：成功返回更新后的商品知识。
    """
    repo = Repository(ProductKnowledge, session)
    item = repo.get(item_id)
    if item is None:
        return error_response(CODE_NOT_FOUND, "商品知识不存在")
    _, denied = _resolve_shop_in_scope(session, user, item.shop_pk)
    if denied is not None:
        return denied
    repo.update(item_id, status=1 if int(status) == 1 else 0)
    message = "已启用" if int(status) == 1 else "已停用"
    return success_response(
        data=serialize_product_knowledge(item), message=message
    )


def delete_product_knowledge(
    session: Session, user: SysUser, item_id: int
) -> ApiResponse:
    """逻辑删除商品知识（禁止物理删除，需求 9.5 / 24.6）。

    经状态字段 status=0 标记失效，记录保留、总数不变。

    Args:
        session: 数据库会话。
        user: 当前登录用户。
        item_id: 商品知识主键。

    Returns:
        统一响应体：成功返回 data=None。
    """
    repo = Repository(ProductKnowledge, session)
    item = repo.get(item_id)
    if item is None:
        return error_response(CODE_NOT_FOUND, "商品知识不存在")
    _, denied = _resolve_shop_in_scope(session, user, item.shop_pk)
    if denied is not None:
        return denied
    repo.soft_delete(item_id, field="status")
    return success_response(data=None, message="已删除")


__all__ = [
    "serialize_product_knowledge",
    "upsert_product_knowledge",
    "update_product_knowledge",
    "get_product_knowledge",
    "list_product_knowledge",
    "set_product_knowledge_status",
    "delete_product_knowledge",
]
