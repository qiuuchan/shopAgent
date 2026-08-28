# -*- coding: utf-8 -*-
"""
common.services.dict_seed_data —— 数据字典枚举初始数据
======================================================
本文件用途：集中登记「拼多多自动回复」系统全部枚举值及其中文文案，作为
``sys_dict`` 数据字典表的初始数据来源（规范 15：枚举值必须入数据字典表，
前端展示从字典表查出中文）。

设计依据：design.md「字典类型（入 sys_dict）」章节，覆盖：
- conn_state        连接状态（需求 5.7）
- login_state       账号登录态（需求 4.9 / 5.7）
- match_type        关键词匹配方式（需求 6.x / 24.7）
- reply_type        回复类型（需求 7.x / 24.7）
- filter_condition  消息过滤条件类型（需求 12.x）
- risk_type         风控类型（需求 13.4）
- aftersale_status  售后状态（需求 17.4）
- aftersale_type    售后类型（需求 17.4）
- process_result    消息处理结果（需求 19.1 / 24.7）
- channel_type      通知渠道类型（需求 18.x）
- msg_direction     消息方向（需求 17.x）
- msg_type          消息类型（需求 17.x）
- feedback_status   反馈处理状态（需求 21.5）
- schedule_type     定时任务调度方式（需求 21.2）
- run_result        定时任务执行结果（需求 21.2）
- menu_key          菜单键（需求 24.7）
- ai_provider_type  AI 接口类型 / 服务商协议（需求 8）
- permission_resource 权限资源键中文名（需求 2.3 / 2.4）
- permission_action   权限操作中文名（需求 2.3 / 2.4）

数据结构说明：
- ``DICT_SEED_DATA`` 为「字典类型 -> 字典项列表」的有序映射；
- 每个字典项为 ``(dict_key, dict_label, order_no)`` 三元组；
  其中 ``dict_key`` 与各业务模型中存储的枚举键一致（如 match_type 的
  full/contains/regex），``dict_label`` 为前端展示的中文文案，
  ``order_no`` 为同类型内的升序排序号。

本文件仅定义数据，不含任何数据库操作；幂等补齐逻辑由 dict_service 提供，
供启动自检迁移器（任务 2.12）调用。
"""
from __future__ import annotations

# 字典项三元组类型：(字典键, 中文标签, 排序号)
DictItem = tuple[str, str, int]


# ----------------------------------------------------------------------
# 全部枚举字典初始数据：dict_type -> [(dict_key, dict_label, order_no), ...]
# 键、值与各业务模型注释中的枚举约定保持一致，前端从本表查中文展示。
# ----------------------------------------------------------------------
DICT_SEED_DATA: dict[str, list[DictItem]] = {
    # 平台类型：店铺所属平台（需求 24.x 多平台分派前置）。
    # 与 Shop.platform 模型枚举一致；前端据本字典渲染平台中文名与徽标。
    "platform": [
        ("pdd", "拼多多", 1),
        ("tiktok", "TikTok Shop", 2),
    ],
    # 连接状态：连接状态管理器为每个店铺维护（需求 5.7）
    "conn_state": [
        ("connected", "已连接", 1),
        ("connecting", "连接中", 2),
        ("disconnected", "断开", 3),
        ("reconnecting", "重连中", 4),
        ("error", "错误", 5),
    ],
    # 账号登录态：店铺登录状态（需求 4.9 / 5.7）
    "login_state": [
        ("unverified", "未验证", 1),
        ("online", "在线", 2),
        ("offline", "离线", 3),
        ("relogin", "需重新登录", 4),
    ],
    # 关键词匹配方式（需求 6.x）
    "match_type": [
        ("full", "全匹配", 1),
        ("contains", "包含", 2),
        ("regex", "正则", 3),
    ],
    # 回复类型（需求 7.x）
    "reply_type": [
        ("text", "文本", 1),
        ("image", "图片", 2),
    ],
    # 消息过滤条件类型（需求 12.x）
    "filter_condition": [
        ("contains", "包含关键词", 1),
        ("regex", "正则匹配", 2),
        ("msg_type", "消息类型", 3),
    ],
    # 风控类型（需求 13.4）
    "risk_type": [
        ("frequency_limit", "频率限制", 1),
        ("risk_message", "风险消息", 2),
        ("reconnect_fail", "重连失败", 3),
    ],
    # 售后状态（需求 17.4）
    "aftersale_status": [
        ("none", "无售后", 1),
        ("applying", "申请中", 2),
        ("processing", "处理中", 3),
        ("refunded", "已退款", 4),
        ("closed", "已关闭", 5),
    ],
    # 售后类型（需求 17.4）
    "aftersale_type": [
        ("refund_only", "仅退款", 1),
        ("return_refund", "退货退款", 2),
        ("exchange", "换货", 3),
        ("repair", "维修", 4),
    ],
    # 消息处理结果（需求 19.1）
    "process_result": [
        ("auto_reply", "自动回复", 1),
        ("ai_reply", "AI回复", 2),
        ("ai_reply_failed", "AI回复失败", 3),
        ("filtered", "已过滤", 4),
        ("blacklisted", "黑名单拦截", 5),
        ("non_business_hours", "非营业时间", 6),
        ("risk_paused", "风控暂停", 7),
        ("transferred", "已转人工", 8),
        ("no_match", "无匹配规则", 9),
    ],
    # 通知渠道类型（需求 18.x）
    "channel_type": [
        ("email", "邮件", 1),
        ("webhook", "Webhook", 2),
        ("wecom", "企业微信", 3),
    ],
    # 消息方向（需求 17.x）
    "msg_direction": [
        ("in", "接收", 1),
        ("out", "发送", 2),
    ],
    # 消息类型（需求 17.x）
    "msg_type": [
        ("text", "文本", 1),
        ("image", "图片", 2),
        ("video", "视频", 3),
        ("emotion", "表情", 4),
        ("withdraw", "撤回", 5),
        ("goods_inquiry", "商品咨询", 6),
        ("goods_spec", "商品规格", 7),
        ("order", "订单", 8),
        ("transfer", "转接", 9),
    ],
    # 反馈处理状态（需求 21.5）
    "feedback_status": [
        ("pending", "待处理", 1),
        ("processing", "处理中", 2),
        ("done", "已处理", 3),
        ("closed", "已关闭", 4),
    ],
    # 定时任务调度方式（需求 21.2）
    "schedule_type": [
        ("cron", "Cron 表达式", 1),
        ("interval", "固定间隔", 2),
    ],
    # 定时任务执行结果（需求 21.2）
    "run_result": [
        ("success", "成功", 1),
        ("failed", "失败", 2),
    ],
    # 菜单键（需求 24.7）：与系统主要菜单一一对应，前端展示中文菜单名
    "menu_key": [
        ("dashboard", "仪表盘", 1),
        ("shop_manage", "店铺管理", 2),
        ("keyword_reply", "关键词回复", 3),
        ("default_reply", "默认回复", 4),
        ("goods_reply", "商品专属回复", 5),
        ("knowledge_base", "知识库", 6),
        ("ai_setting", "AI 设置", 7),
        ("business_hours", "营业时间", 8),
        ("message_filter", "消息过滤", 9),
        ("blacklist", "黑名单", 10),
        ("risk_control", "风控管理", 11),
        ("transfer_human", "转人工", 12),
        ("online_chat", "在线聊天", 13),
        ("goods_manage", "商品管理", 14),
        ("message_log", "消息日志", 15),
        ("risk_log", "风控日志", 16),
        ("system_log", "系统日志", 17),
        ("notify", "通知", 18),
        ("data_analysis", "数据分析", 19),
        ("user_manage", "用户管理", 20),
        ("system_setting", "系统设置", 21),
        ("announcement", "公告管理", 22),
        ("feedback", "意见反馈", 23),
        ("scheduled_task", "定时任务", 24),
        ("profile", "个人设置", 25),
        ("about", "关于", 26),
    ],
    # AI 接口类型（需求 8）：供 AI 设置选择服务商协议，前端从字典查中文展示
    "ai_provider_type": [
        ("openai_compatible", "OpenAI 兼容", 1),
        ("anthropic", "Anthropic Claude", 2),
        ("gemini", "Google Gemini", 3),
        ("dashscope_app", "DashScope 应用", 4),
    ],
    # 权限资源键（需求 2.3/2.4）：角色权限分配界面据此展示资源中文名
    "permission_resource": [
        ("user", "用户管理", 1),
        ("role", "角色权限", 2),
        ("shop", "店铺管理", 3),
        ("keyword", "关键词回复", 4),
        ("reply", "默认/商品回复", 5),
        ("business_hours", "营业时间", 6),
        ("message_filter", "消息过滤", 7),
        ("blacklist", "黑名单", 8),
        ("product_knowledge", "商品知识库", 9),
        ("cs_knowledge", "客服知识库", 10),
        ("risk_control", "风控管理", 11),
        ("product", "商品管理", 12),
        ("chat", "在线聊天", 13),
        ("dashboard", "仪表盘", 14),
        ("message_log", "消息日志", 15),
        ("risk_log", "风控日志", 16),
        ("system_log", "系统日志", 17),
        ("notify", "通知管理", 18),
        ("profile", "个人设置", 19),
        ("tutorial", "使用教程", 20),
        ("feedback", "意见反馈", 21),
        ("disclaimer", "免责声明", 22),
        ("about", "关于", 23),
    ],
    # 权限操作（需求 2.3/2.4）：角色权限分配界面据此展示操作中文名
    "permission_action": [
        ("view", "查看", 1),
        ("create", "新增", 2),
        ("update", "修改", 3),
        ("disable", "停用/删除", 4),
        ("send", "发送", 5),
    ],
}


__all__ = [
    "DictItem",
    "DICT_SEED_DATA",
]
