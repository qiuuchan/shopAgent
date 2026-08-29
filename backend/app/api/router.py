# -*- coding: utf-8 -*-
"""
backend.app.api.router —— 业务路由聚合
======================================
本文件用途：将各业务域 REST 路由（auth / users / shops / ...）聚合为统一的
``api_router``，由 _bootstrap.py 以统一前缀（如 /api/v1）挂载到 FastAPI 应用。

说明：当前阶段已实现认证路由（auth），后续任务在此按业务域陆续 include 各路由
模块。集中聚合便于统一管理前缀、标签与挂载顺序。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    ai_config,
    announcements,
    auth,
    backup,
    blacklist,
    business_hours,
    captcha,
    chat,
    chat_context,
    chat_ws,
    dashboard,
    feedback,
    internal_chat,
    internal_notify,
    keywords,
    knowledge,
    logs,
    message_filters,
    notify,
    products,
    profile,
    replies,
    risk_control,
    roles,
    scheduled_tasks,
    settings,
    shops,
    transfer,
    users,
)

# 业务路由聚合器：各业务域路由统一并入，再由引导层挂载。
api_router = APIRouter()

# 认证接口：登录 / 登出（需求 1）。
api_router.include_router(auth.router)

# 登录滑块验证码接口：开关查询 / 生成 / 校验（登录前公开，需求 21.6）。
api_router.include_router(captcha.router)

# 用户与角色管理接口（需求 2）。
api_router.include_router(users.router)

# 角色与权限分配接口：角色增删改 / 权限分配 / 权限点列表（需求 2.3 / 2.4）。
api_router.include_router(roles.router)

# 店铺与账号管理接口（需求 3）。
api_router.include_router(shops.router)

# 关键词规则管理接口（需求 6）。
api_router.include_router(keywords.router)

# 默认回复与商品专属回复接口（需求 7）。
api_router.include_router(replies.router)

# AI（LLM）配置接口：接口类型选择 / 测试连接 / 配置保存（需求 8）。
api_router.include_router(ai_config.router)

# 营业时间配置接口（需求 11.1）。
api_router.include_router(business_hours.router)

# 消息过滤规则接口（需求 12）。
api_router.include_router(message_filters.router)

# 黑名单接口（需求 12）。
api_router.include_router(blacklist.router)

# 知识库管理接口：商品知识 / 客服知识（需求 9 / 10）。
api_router.include_router(knowledge.router)

# 风控规则配置接口（需求 13.1 / 13.4）。
api_router.include_router(risk_control.router)

# 商品管理接口（需求 15）。
api_router.include_router(products.router)

# 转人工设置接口：客服列表 / 转人工关键词（需求 16.1）。
api_router.include_router(transfer.router)

# 在线聊天接口：会话列表 / 历史消息 / 手动收发 / 新消息提示（需求 14）。
api_router.include_router(chat.router)

# 在线聊天前端实时推送 WebSocket（按店铺订阅，需求 14，方案 2）。
api_router.include_router(chat_ws.router)

# 在线聊天内部事件接收（websocket 服务回调，密钥鉴权，需求 14）。
api_router.include_router(internal_chat.router)

# 系统事件通知内部接收（websocket 告警回调，密钥鉴权，需求 18.3，TIK-018 补链路）。
api_router.include_router(internal_notify.router)

# 会话订单/商品上下文接口：记录与展示（需求 17）。
api_router.include_router(chat_context.router)

# 仪表盘与数据分析接口（需求 20）。
api_router.include_router(dashboard.router)

# 消息/风控/系统日志查询接口（需求 19 / 21.4）。
api_router.include_router(logs.router)

# 通知渠道与消息通知接口（需求 18）。
api_router.include_router(notify.router)

# 个人设置接口：账户信息 / 修改密码 / 联系方式（需求 22）。
api_router.include_router(profile.router)

# 系统设置接口：主题 / 基础设置 / 品牌 / 免责声明 / 二维码 / 菜单显隐（需求 21）。
api_router.include_router(settings.router)

# 数据库备份导出与导入恢复接口（需求 21.16）。
api_router.include_router(backup.router)

# 公告管理与用户端展示接口（需求 21.3）。
api_router.include_router(announcements.router)

# 意见反馈提交与管理员处理回复接口（需求 21.5）。
api_router.include_router(feedback.router)

# 定时任务与执行日志接口（需求 21.2）。
api_router.include_router(scheduled_tasks.router)


__all__ = ["api_router"]
