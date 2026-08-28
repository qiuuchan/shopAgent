# -*- coding: utf-8 -*-
"""
channel_tiktok —— TikTok 泰国站卖家后台通道基础件包
==================================================
本包用途：承载 TikTok 卖家客服通道（泰国站 ``seller.tiktokshopglobalselling.com``，
中文界面）的基础件，为后续 TIK-012（消息解析/发送/会话守卫）/ TIK-013（主循环监控与
登录失效检测）提供「选择器常量 / 浏览器会话 / 登录」三块底层能力。

依赖与边界：
- 复用 TIK-009 提取的公共浏览器启动逻辑 ``login.browser_launcher``（不重复实现，
  开发规范 52）。
- 选择器以 TIK-001 spike 实测清单（``spike/tiktok/selectors.md``）为唯一事实来源。
- 本包**只**做浏览器层交互与选择器常量声明，不涉及数据库、店铺信息抓取与消息决策
  （后者由上层 TIK-012/013 编排），保持单一职责。

实现约束（开发规范）：文件名用下划线（40）、导入置顶（51）、中文注释（37/50）、
单文件 ≤500 行（35）、日志禁用 debug（38）、地址经环境变量或常量管理（21）。
"""
from __future__ import annotations

# 包内模块在本文件统一对外导出，便于上层按 ``from channel_tiktok import ...`` 引用。
from channel_tiktok.browser_session import BrowserSession
from channel_tiktok.selectors import (
    LOGIN_PAGE_MARKERS,
    TIKTOK_CHAT_PATH,
    TIKTOK_HOME_PATH,
    TIKTOK_LOGIN_PATH,
    TIKTOK_SELLER_URL,
)
from channel_tiktok.tiktok_login import login_tiktok, refresh_tiktok_session

__all__ = [
    "BrowserSession",
    "LOGIN_PAGE_MARKERS",
    "TIKTOK_CHAT_PATH",
    "TIKTOK_HOME_PATH",
    "TIKTOK_LOGIN_PATH",
    "TIKTOK_SELLER_URL",
    "login_tiktok",
    "refresh_tiktok_session",
]
