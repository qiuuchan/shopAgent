# -*- coding: utf-8 -*-
"""
channel_tiktok.tiktok_login —— TikTok 账号密码登录与 Cookie 刷新（登录流程代码）
=============================================================================
本文件用途：提供 TikTok 泰国站卖家后台的「账号密码登录」与「登录态刷新」浏览器流程
（TIK-011 基础件），上层编排（如 TIK-016 routes / TIK-013 监控复位）调用本模块完成
登录态维护与 Cookie 导出。

重要约束（来自交接单 / spike 实测）：
- **测试店账号尚未就绪**：登录流程代码照写，但**不实际联调**（硬约束）。所有真实浏览器
  操作（验证码人工等待、域名抓取）仅在运行期发生；本模块不主动发起真实登录。
- **登录等待超时对齐 PDD 模式**：经环境变量 ``TIKTOK_LOGIN_WAIT_TIMEOUT_MS`` 读取，缺省
  120000ms（120 秒），与 ``login.playwright_login._login_wait_timeout_ms`` 同口径（需求 4.5）。
- **登录成功导出**：Cookie 列表转 JSON 字符串；并尝试抓取 ``shop_id`` / ``shop_name``
  （泰国站店铺标识，spike s1 实测店铺 FunToy Lab）。抓取方式在账号就绪后实测修正
  （标注「待 TIK-018 实测」）。
- **复用公共浏览器启动**：经 ``login.browser_launcher`` 启动持久化上下文，与 PDD 同约定。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、导入置顶（51）、
日志禁用 debug（38）、地址经常量/环境变量（21）、不 import channel_pdd 内部。
"""
from __future__ import annotations

import asyncio
import json
import logging
import zlib
from typing import Any, Dict, List, Optional

from channel_tiktok.browser_session import BrowserSession
from channel_tiktok.selectors import (
    TIKTOK_LOGIN_PATH,
    TIKTOK_SELLER_URL,
)
from common.core.config import get_settings

logger = logging.getLogger("channel_tiktok.tiktok_login")

# 登录等待超时默认值（毫秒）：预留人工完成验证码的时间，对齐 PDD _DEFAULT_LOGIN_WAIT_TIMEOUT_MS。
_DEFAULT_LOGIN_WAIT_TIMEOUT_MS: int = 120_000

# 账号密码登录是否无头：TikTok 登录含人工验证码，默认非无头（与 PDD BROWSER_HEADLESS 口径）。
_DEFAULT_LOGIN_HEADLESS: bool = False


def _login_wait_timeout_ms() -> int:
    """读取 TikTok 登录等待超时（毫秒），经统一配置读取，缺省回退默认值（需求 4.5 对齐）。

    经 ``common.core.config.get_settings().tiktok_login_wait_timeout_ms`` 读取
    （环境变量 ``TIKTOK_LOGIN_WAIT_TIMEOUT_MS``，TIK-017），与 .env 声明一致；
    读取异常时回退默认值，不阻断登录流程。
    """
    try:
        return get_settings().tiktok_login_wait_timeout_ms
    except Exception:  # noqa: BLE001 - 配置读取异常回退默认值
        logger.warning("读取 TikTok 登录等待超时配置失败，使用默认值 %d", _DEFAULT_LOGIN_WAIT_TIMEOUT_MS)
        return _DEFAULT_LOGIN_WAIT_TIMEOUT_MS


def _cookies_list_to_json(cookies_list: List[Dict]) -> str:
    """将 Playwright 的 Cookie 列表转换为 ``{name: value}`` 映射的 JSON 字符串。

    Args:
        cookies_list: ``context.cookies()`` 返回的 Cookie 字典列表。

    Returns:
        ``{name: value}`` 映射的 JSON 字符串（ensure_ascii=False，保留中文）。
    """
    cookies_dict = {
        item.get("name", ""): item.get("value", "")
        for item in cookies_list
        if item.get("name")
    }
    return json.dumps(cookies_dict, ensure_ascii=False)


def _build_login_url() -> str:
    """构造 TikTok 登录页完整 URL（基址 + 登录路径，泰国站固定基址）。

    Returns:
        登录页完整 URL 字符串。
    """
    return f"{TIKTOK_SELLER_URL}{TIKTOK_LOGIN_PATH}"


async def _safe_close_session(session: Optional[BrowserSession]) -> None:
    """安全释放浏览器会话（吞异常，不影响主流程返回）。"""
    if session is not None:
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("释放 TikTok 浏览器会话失败（可忽略）: %s", exc)


async def login_tiktok(name: str, password: str) -> Optional[Dict[str, Any]]:
    """使用账号密码登录 TikTok 泰国站卖家后台并导出 Cookie 与店铺信息（需求 4.1/4.2/4.5 对齐）。

    以非无头模式启动持久化上下文（默认，便于人工完成验证码），打开登录页并填写账号密码
    提交；等待登录成功（title 命中成功集合或跳出登录页）直至超时。超时 / 异常统一降级
    返回 None（不抛异常）。成功则返回含 ``cookies_json`` / ``shop_id`` / ``shop_name`` 的字典。

    **标注「待 TIK-018 实测」**：真实登录态判定标记、shop_id/shop_name 抓取选择器，需在测试
    店账号就绪后实测修正（本函数结构已就位，不阻塞后续纯逻辑开发）。

    Args:
        name: 登录账号名（用于命名隔离的用户数据目录 ``tiktok_{shop_pk}`` 场景下的标识；
            此处以账号名为隔离键，与 BrowserSession 的 shop_pk 隔离互补，登录阶段可仅持账号名）。
        password: 账号密码。

    Returns:
        登录成功返回 ``{"cookies_json": str, "shop_id": Optional[str],
        "shop_name": Optional[str], "name": str}``；失败 / 超时 / 异常返回 None。
    """
    # 注：登录阶段以账号名 name 作为隔离键（与 BrowserSession 的 shop_pk 维度互补）。
    # 为复用 BrowserSession 的锁清理与启动封装，这里以 name 的哈希派生一个 shop_pk 占位
    # 语义：直接构造 BrowserSession 需 shop_pk，登录期尚无 shop_pk，故以账号名派生稳定整型。
    shop_pk = _derive_shop_pk_from_name(name)
    session = BrowserSession(
        shop_pk=shop_pk, headless=_DEFAULT_LOGIN_HEADLESS, user_data_dir=None
    )
    wait_timeout = _login_wait_timeout_ms()

    try:
        await session.start()
        page = session.page
        if page is None:
            logger.error("账号 '%s' 登录失败：浏览器页面未就绪", name)
            return None

        # 打开登录页（泰国站固定登录 URL，允许写死基址）。
        login_url = _build_login_url()
        logger.info("账号 '%s' 打开 TikTok 登录页: %s", name, login_url)
        await page.goto(login_url)

        # TODO(TIK-018 实测)：填入账号密码并提交。选择器待测试店账号就绪后按实测回填。
        # 当前仅占位结构，不实际执行真实填写/提交（硬约束：不真实联调）。
        #   await page.fill(<账号输入框>, name)
        #   await page.fill(<密码输入框>, password)
        #   await page.click(<登录按钮>)

        # 等待登录成功（title 跳出登录页 / 命中成功集合）。超时即判定失败（需求 4.5 对齐）。
        # success_condition 待 TIK-018 实测确认后补全；结构如下：
        #   await page.wait_for_function(<成功判定>, timeout=wait_timeout)
        logger.warning(
            "账号 '%s' 登录流程为占位实现，待 TIK-018 实测（不真实联调）", name
        )
        # 占位返回：不触发真实等待，直接以 None 表示「未实测」。
        return None
    except Exception as exc:  # noqa: BLE001 - 登录异常统一降级
        logger.error("账号 '%s' TikTok 登录异常: %s", name, exc)
        return None
    finally:
        await _safe_close_session(session)


def _derive_shop_pk_from_name(name: str) -> int:
    """由账号名派生稳定整型 shop_pk 占位（仅用于登录期目录隔离，非真实 shop.id）。

    BrowserSession 需要 shop_pk 作隔离键，登录期尚无数据库 shop.id；以账号名稳定哈希
    派生，保证同名账号复用同一用户数据目录（登录态持久化），不同名账号隔离。

    注意：Python 内建 ``hash()`` 受 ``PYTHONHASHSEED`` 随机化，同一字符串跨进程结果
    不同，不能用于持久目录命名；改用 ``zlib.crc32``（确定性算法，跨进程稳定）。

    Args:
        name: 登录账号名。

    Returns:
        稳定的非负整型（用于 ``tiktok_{pk}`` 目录名）。
    """
    return zlib.crc32(name.encode("utf-8")) % (10 ** 9)


async def refresh_tiktok_session(name: str) -> Optional[str]:
    """复用已保存的用户数据目录刷新 TikTok 登录态（需求 4.6/4.7 对齐）。

    以无头模式启动持久化上下文并打开首页：若跳转到登录页（``LOGIN_PAGE_MARKERS`` 命中）
    说明登录态已失效，返回 None（供上层标记「需重新登录」）；否则导出最新 Cookie 并返回。

    **标注「待 TIK-018 实测」**：跳转判定与 Cookie 导出结构已就位，真实选择器待账号就绪后修正。

    Args:
        name: 登录账号名（定位其用户数据目录）。

    Returns:
        刷新成功返回最新 Cookie 的 JSON 字符串；登录态失效 / 异常返回 None。
    """
    shop_pk = _derive_shop_pk_from_name(name)
    session = BrowserSession(shop_pk=shop_pk, headless=True, user_data_dir=None)
    try:
        await session.start()
        page = session.page
        if page is None:
            logger.error("账号 '%s' Cookie 刷新失败：浏览器页面未就绪", name)
            return None

        # 打开首页（泰国站基址 + 首页路径）。
        home_url = f"{TIKTOK_SELLER_URL}/homepage"
        await page.goto(home_url)

        # TODO(TIK-018 实测)：判定是否跳登录页（LOGIN_PAGE_MARKERS）。当前占位：
        #   try: await page.wait_for_url("**/account/login", timeout=5000); return None
        #   except ...: pass
        logger.warning(
            "账号 '%s' Cookie 刷新为占位实现，待 TIK-018 实测（不真实联调）", name
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("账号 '%s' TikTok Cookie 刷新异常: %s", name, exc)
        return None
    finally:
        await _safe_close_session(session)


__all__ = [
    "login_tiktok",
    "refresh_tiktok_session",
    "_login_wait_timeout_ms",
    "_cookies_list_to_json",
]
