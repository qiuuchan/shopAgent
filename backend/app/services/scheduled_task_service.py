# -*- coding: utf-8 -*-
"""
backend.app.services.scheduled_task_service —— 定时任务与执行日志业务服务
========================================================================
本文件用途：实现 backend 服务的「定时任务」管理端业务逻辑（任务 17.6 配套，
满足需求 21.2），供 scheduled_tasks 路由复用：

- ``ensure_default_tasks(...)``：幂等补齐四项内置定时任务（Cookie 刷新 /
  商品同步 / 文件日志清理 / TikTok 营业时间窗），缺失时按业务键 ``task_key``
  upsert 创建（规范 14：缺失初始数据自动补齐，不影响历史数据）。
- ``list_tasks(...)``：定时任务列表后端分页（需求 21.2 配套）。
- ``update_task(...)``：更新调度方式 / 调度配置 / 启停用（需求 21.2）。
- ``set_task_enabled(...)``：启用 / 停用定时任务。
- ``list_run_logs(...)``：定时任务执行日志后端分页，可按任务键筛选（需求
  21.2 / 21.4，执行日志禁止物理删除）。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3 / 需求 24.1）。
- 所有数据访问经 common.db.repository 的参数化查询，禁止拼接 SQL（规范 16）。
- 调度方式 / 执行结果枚举入字典，前端展示中文（需求 21.2 / 24.7）。
- 时间字段统一北京时间（规范 17 / 需求 24.8）。
- 禁止物理删除业务数据（规范 11 / 需求 19.5）：执行日志仅查询不删除。
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）；全中文。
- 「定时任务为管理员专属」由路由层统一拦截，本服务专注业务逻辑。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.business_codes import CODE_NOT_FOUND, CODE_PARAM_ERROR
from common.db.repository import Repository
from common.models.task_models import ScheduledTask, TaskRunLog
from common.schemas.common import ApiResponse, error_response, success_response

# 内置定时任务键与默认配置（与 scheduler 服务 tasks.constants 约定一致，需求 21.2）。
# 每项：(task_key, task_name, schedule_type, schedule_config, enabled)
# tiktok_window：TikTok 营业时间窗控制（TIK-015），管理端页面仅支持编辑/启停，
# 无法新建任务，故随内置任务种子幂等补齐；无 TikTok 店铺时执行体直接跳过，无害。
DEFAULT_TASKS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("cookie_refresh", "Cookie 刷新", "interval", "600", True),
    ("product_sync", "商品同步", "cron", "0 3 * * *", False),
    ("log_file_cleanup", "文件日志清理", "cron", "0 4 * * *", True),
    ("tiktok_window", "TikTok 营业时间窗", "interval", "60", True),
)

# 受支持的调度方式（与 sys_dict 的 schedule_type 一致）。
SCHEDULE_TYPES: frozenset[str] = frozenset({"cron", "interval"})


# ----------------------------------------------------------------------
# 序列化辅助
# ----------------------------------------------------------------------
def serialize_task(task: ScheduledTask) -> Dict[str, Any]:
    """将定时任务模型序列化为对外字典（时间字段为北京时间）。

    Args:
        task: 定时任务模型实例。

    Returns:
        定时任务信息字典。
    """
    return {
        "id": task.id,
        "task_key": task.task_key,
        "task_name": task.task_name,
        "schedule_type": task.schedule_type,
        "schedule_config": task.schedule_config,
        "enabled": bool(task.enabled),
        "last_run_at": task.last_run_at,
        "next_run_at": task.next_run_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def serialize_run_log(log: TaskRunLog) -> Dict[str, Any]:
    """将执行日志模型序列化为对外字典（时间字段为北京时间）。

    Args:
        log: 执行日志模型实例。

    Returns:
        执行日志信息字典。
    """
    return {
        "id": log.id,
        "task_key": log.task_key,
        "run_result": log.run_result,
        "message": log.message,
        "log_time": log.log_time,
        "created_at": log.created_at,
    }


# ----------------------------------------------------------------------
# 内置任务幂等补齐（规范 14：缺失初始数据自动补齐，不影响历史数据）
# ----------------------------------------------------------------------
def ensure_default_tasks(session: Session) -> None:
    """缺失时按业务键补齐内置定时任务，已存在则不改动（幂等）。

    依据规范 14，仅在对应 ``task_key`` 不存在时创建默认配置，绝不覆盖管理员
    已修改的历史配置，确保不影响历史数据。

    Args:
        session: 数据库会话。
    """
    repo = Repository(ScheduledTask, session)
    for task_key, task_name, schedule_type, schedule_config, enabled in DEFAULT_TASKS:
        existing = repo.get_by(task_key=task_key)
        if existing is None:
            repo.create(
                task_key=task_key,
                task_name=task_name,
                schedule_type=schedule_type,
                schedule_config=schedule_config,
                enabled=enabled,
            )


# ----------------------------------------------------------------------
# 列表（后端分页，需求 21.2 配套）
# ----------------------------------------------------------------------
def list_tasks(
    session: Session,
    page: Any = 1,
    page_size: Any = 20,
) -> ApiResponse:
    """定时任务列表后端分页（需求 21.2）。

    首次查询时幂等补齐内置任务，便于管理端直接查看与配置。

    Args:
        session: 数据库会话。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    ensure_default_tasks(session)
    page_result = Repository(ScheduledTask, session).paginate(
        page=page, page_size=page_size
    )
    serialized: List[Dict[str, Any]] = [
        serialize_task(task) for task in page_result.items
    ]
    data = {
        "list": serialized,
        "total": page_result.total,
        "page": page_result.page,
        "page_size": page_result.page_size,
    }
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 更新调度配置（需求 21.2）
# ----------------------------------------------------------------------
def update_task(
    session: Session,
    task_id: int,
    *,
    schedule_type: Optional[str] = None,
    schedule_config: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> ApiResponse:
    """更新定时任务的调度方式 / 调度配置 / 启停用（仅更新显式字段，需求 21.2）。

    Args:
        session: 数据库会话。
        task_id: 目标任务 ID。
        schedule_type: 新调度方式（cron/interval）；None 表示不修改。
        schedule_config: 新调度配置；None 表示不修改。
        enabled: 新启停用状态；None 表示不修改。

    Returns:
        统一响应体：成功返回更新后的任务信息。
    """
    repo = Repository(ScheduledTask, session)
    task = repo.get(task_id)
    if task is None:
        return error_response(CODE_NOT_FOUND, "目标定时任务不存在")

    values: Dict[str, Any] = {}
    if schedule_type is not None:
        if schedule_type not in SCHEDULE_TYPES:
            return error_response(CODE_PARAM_ERROR, "调度方式仅支持 cron 或 interval")
        values["schedule_type"] = schedule_type
    if schedule_config is not None:
        if not schedule_config.strip():
            return error_response(CODE_PARAM_ERROR, "调度配置不能为空")
        values["schedule_config"] = schedule_config.strip()
    if enabled is not None:
        values["enabled"] = bool(enabled)
    if not values:
        return error_response(CODE_PARAM_ERROR, "未提供任何待更新字段")

    repo.update(task_id, **values)
    return success_response(data=serialize_task(task), message="更新成功")


# ----------------------------------------------------------------------
# 启停用
# ----------------------------------------------------------------------
def set_task_enabled(
    session: Session,
    task_id: int,
    enabled: bool,
) -> ApiResponse:
    """启用或停用定时任务（需求 21.2 配套）。

    Args:
        session: 数据库会话。
        task_id: 目标任务 ID。
        enabled: True=启用，False=停用。

    Returns:
        统一响应体：成功返回更新后的任务信息。
    """
    repo = Repository(ScheduledTask, session)
    task = repo.get(task_id)
    if task is None:
        return error_response(CODE_NOT_FOUND, "目标定时任务不存在")

    repo.update(task_id, enabled=bool(enabled))
    return success_response(
        data=serialize_task(task),
        message="已启用" if enabled else "已停用",
    )


# ----------------------------------------------------------------------
# 执行日志列表（后端分页，需求 21.2 / 21.4，禁止物理删除）
# ----------------------------------------------------------------------
def list_run_logs(
    session: Session,
    task_key: Optional[str] = None,
    page: Any = 1,
    page_size: Any = 20,
) -> ApiResponse:
    """定时任务执行日志后端分页，可按任务键筛选（需求 21.2 / 21.4）。

    执行日志为业务日志，禁止物理删除（规范 11 / 需求 19.5），本服务仅查询。

    Args:
        session: 数据库会话。
        task_key: 按任务键筛选；None 表示不筛选。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    filters: Dict[str, Any] = {}
    if task_key:
        filters["task_key"] = task_key

    page_result = Repository(TaskRunLog, session).paginate(
        page=page, page_size=page_size, filters=filters or None
    )
    serialized: List[Dict[str, Any]] = [
        serialize_run_log(log) for log in page_result.items
    ]
    data = {
        "list": serialized,
        "total": page_result.total,
        "page": page_result.page,
        "page_size": page_result.page_size,
    }
    return success_response(data=data, message="查询成功")


__all__ = [
    "DEFAULT_TASKS",
    "SCHEDULE_TYPES",
    "serialize_task",
    "serialize_run_log",
    "ensure_default_tasks",
    "list_tasks",
    "update_task",
    "set_task_enabled",
    "list_run_logs",
]
