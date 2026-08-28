# -*- coding: utf-8 -*-
"""
channel_tiktok.browser_session —— TikTok 每店独立浏览器会话
=========================================================
本文件用途：为 TikTok 通道提供「每店铺一个持久化浏览器会话」的封装（TIK-011 关键路径
基础件），向上层 TikTokChannel / 登录流程提供统一、可注入、易测试的浏览器会话接口。

设计要点（对齐仓库既有约定与 TIK-009 公共逻辑）：
- **每店独立 user-data-dir**：目录命名 ``tiktok_{shop_pk}``（如 ``tiktok_12``），保证
  多店铺登录态彼此隔离、互不串号；基础目录经环境变量 ``PLAYWRIGHT_USER_DATA_DIR``
  配置（缺省回退当前工作目录下 ``browser_data``），禁止写死绝对路径（开发规范 21）。
- **复用公共启动逻辑**：启动前先 ``clean_singleton_lock_files`` 清理残留锁，再经
  ``launch_persistent_context_with_retry`` 启动持久化上下文（失败清锁重试一次），
  不重复实现浏览器启动细节（开发规范 52 复用 login.browser_launcher）。
- **可选 headless / 代理**：``headless`` 默认 False（账号密码登录需人工验证码，同 PDD
  口径）；``proxy_server`` 可选，用于按店铺配置出口代理。
- **可注入 playwright 工厂**：构造器可传入 ``playwright_factory``（缺省走
  ``async_playwright``），便于单测以 mock 替换，避免真实启动浏览器（硬约束：不真实联调）。
- **page 属性 / close()**：提供 ``page``（首个已开页面或新建页面）与 ``close()`` 释放；
  close 安全吞异常，绝不向上抛。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、导入置顶（51）、
日志禁用 debug（38）、不 import channel_pdd 内部（隔离平台依赖）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Optional

from login.browser_launcher import (
    clean_singleton_lock_files,
    get_chromium_args,
    launch_persistent_context_with_retry,
)

logger = logging.getLogger("channel_tiktok.browser_session")

# 用户数据子目录命名前缀：``tiktok_{shop_pk}``（每店独立隔离）。
_USER_DATA_PREFIX: str = "tiktok_"

# playwright 工厂默认实现（延迟导入，避免模块级强依赖；单测可注入桩）。
_DefaultPlaywrightFactory = Callable[[], Any]


def _default_playwright_factory() -> Any:
    """返回默认 playwright 异步启动器（缺省实现，延迟导入避免模块级硬依赖）。"""
    from playwright.async_api import async_playwright

    return async_playwright


def _resolve_user_data_dir(shop_pk: int) -> str:
    """计算指定店铺的 TikTok 浏览器用户数据目录（按 shop_pk 隔离，避免串号）。

    基础目录经环境变量 ``PLAYWRIGHT_USER_DATA_DIR`` 配置，缺省回退当前工作目录下
    的 ``browser_data``（与 PDD ``playwright_login._resolve_user_data_dir`` 同约定，
    保证容器持久化卷挂载点一致）。子目录命名为 ``tiktok_{shop_pk}``，与 PDD 的
    ``user_{name}`` 区分命名空间，互不干扰。

    Args:
        shop_pk: 店铺主键 shop.id（作为子目录名隔离不同店铺的浏览器数据）。

    Returns:
        该店铺专属的用户数据目录绝对路径。
    """
    base_dir = os.environ.get("PLAYWRIGHT_USER_DATA_DIR") or os.path.join(
        os.getcwd(), "browser_data"
    )
    user_data_dir = os.path.join(base_dir, f"{_USER_DATA_PREFIX}{shop_pk}")
    os.makedirs(user_data_dir, exist_ok=True)
    return user_data_dir


class BrowserSession:
    """TikTok 每店铺独立浏览器会话封装（TIK-011 基础件）。

    负责：按 shop_pk 隔离的用户数据目录清理锁 → 启动持久化上下文 → 提供 page →
    释放。所有外部浏览器交互经由本实例，便于上层（TikTokChannel / 登录）解耦并单测。

    典型用法（异步上下文语义由调用方管理，本类仅持有资源）：
        session = BrowserSession(shop_pk=12, headless=False)
        await session.start()
        page = session.page
        ... 使用 page ...
        await session.close()
    """

    def __init__(
        self,
        shop_pk: int,
        *,
        headless: bool = False,
        proxy_server: Optional[str] = None,
        playwright_factory: Optional[_DefaultPlaywrightFactory] = None,
        user_data_dir: Optional[str] = None,
    ) -> None:
        """初始化 TikTok 浏览器会话（不立即启动浏览器）。

        Args:
            shop_pk: 店铺主键 shop.id（用于命名隔离的用户数据目录 ``tiktok_{shop_pk}``）。
            headless: 是否无头模式（默认 False，账号密码登录需人工验证码）。
            proxy_server: 可选出口代理服务器地址（如 ``http://host:port``），None 表示不走代理。
            playwright_factory: 可选 playwright 工厂（缺省走 ``async_playwright``）；注入桩便于单测。
            user_data_dir: 可选显式用户数据目录（注入桩便于单测；缺省按 shop_pk 自动计算）。
        """
        self.shop_pk = shop_pk
        self.headless = headless
        self.proxy_server = proxy_server
        # 用户数据目录：注入优先，否则按 shop_pk 计算（隔离约定）。
        self.user_data_dir: str = user_data_dir or _resolve_user_data_dir(shop_pk)
        self._playwright_factory: _DefaultPlaywrightFactory = (
            playwright_factory or _default_playwright_factory
        )

        # 运行时资源（启动后置位，释放后置空）。
        self._playwright: Optional[Any] = None
        # playwright 异步上下文管理器引用（start 时保留，close 时经 __aexit__ 正确回收）。
        self._playwright_cm: Optional[Any] = None
        self._context: Optional[Any] = None
        self._page: Optional[Any] = None

    @property
    def page(self) -> Any:
        """返回当前会话页面（已开则复用，否则新建）。

        注意：调用前应已完成 ``start()``；未启动时 ``_page`` 为 None，由调用方保证时序。
        本属性直接返回内部持有页面，避免重复 new_page。

        Returns:
            当前会话的 Playwright Page 对象；未启动返回 None。
        """
        return self._page

    async def start(self) -> None:
        """启动浏览器会话：清锁 → 启动持久化上下文 → 开首页（TIK-009 复用）。

        启动前调用 ``clean_singleton_lock_files`` 清理残留 Singleton 锁；经
        ``launch_persistent_context_with_retry`` 启动（失败清锁重试一次）。若配置代理，
        则以 ``--proxy-server`` 启动参数注入（launcher 仅透传 args，见实现）。

        为避免「重复启动」，已启动时直接返回（幂等）。
        """
        if self._context is not None:
            logger.info("店铺 shop_pk=%s 浏览器会话已启动，跳过重复启动", self.shop_pk)
            return

        # 启动前清理残留锁（同一店铺目录受上层信号量串行保护，清理安全）。
        clean_singleton_lock_files(self.user_data_dir, f"tiktok_{self.shop_pk}")

        # 启动 playwright：工厂默认返回 async_playwright() 的异步上下文管理器（CM），
        # 需进入以获取实例；保留 CM 引用，close() 时经 __aexit__ 正确回收，避免泄漏。
        pw_cm = self._playwright_factory()()
        self._playwright_cm = pw_cm if hasattr(pw_cm, "__aenter__") else None
        if self._playwright_cm is not None:
            self._playwright = await self._playwright_cm.__aenter__()
        else:
            self._playwright = pw_cm

        # 组装启动参数：在公共统一参数基础上追加代理（launcher 传入 args 即替换默认
        # 参数，故先取基参再追加）。代理以 --proxy-server 开关注入，等价生效。
        launch_args: list[str] = get_chromium_args()
        if self.proxy_server:
            launch_args.append(f"--proxy-server={self.proxy_server}")

        self._context = await launch_persistent_context_with_retry(
            self._playwright,
            self.user_data_dir,
            f"tiktok_{self.shop_pk}",
            headless=self.headless,
            chromium_args=launch_args,
        )
        self._page = await self._context.new_page()
        logger.info(
            "店铺 shop_pk=%s 浏览器会话已启动（headless=%s, proxy=%s）",
            self.shop_pk, self.headless, bool(self.proxy_server),
        )

    async def close(self) -> None:
        """释放浏览器会话资源（安全吞异常，绝不向上抛）。

        关闭顺序：page → context → playwright，任一步异常均忽略，确保资源尽力释放。
        """
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:  # noqa: BLE001 - 关闭阶段异常不影响后续清理
                pass
            self._page = None

        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001
                pass
            self._context = None

        if self._playwright is not None:
            try:
                if self._playwright_cm is not None:
                    # 经保留的上下文管理器正确退出（async_playwright 标准回收路径）。
                    await self._playwright_cm.__aexit__(None, None, None)
                elif hasattr(self._playwright, "stop"):
                    # 非 CM 工厂（如测试桩）：stop 可能为协程，兼容同步/异步调用。
                    result = self._playwright.stop()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
            self._playwright_cm = None

        logger.info("店铺 shop_pk=%s 浏览器会话已释放", self.shop_pk)


__all__ = ["BrowserSession", "_resolve_user_data_dir"]
