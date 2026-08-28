# -*- coding: utf-8 -*-
"""
channel_tiktok.tiktok_login —— TikTok 账号密码登录与 Cookie 刷新（登录流程代码）
=============================================================================
本文件用途：提供 TikTok 泰国站卖家后台的「账号密码登录」与「登录态刷新」浏览器流程
（TIK-011 基础件），上层编排（如 TIK-016 routes / TIK-013 监控复位）调用本模块完成
登录态维护与 Cookie 导出。

重要约束（来自交接单 / spike 实测）：
- **登录表单选择器已实测回填**（2026-08-28，spike s6_login_form_map）：泰国站登录页
  默认激活「手机号」登录 tab，账号 / 密码 / 登录按钮选择器见 ``selectors.py`` 登录节；
  shop_id 当前以账号名占位，oec_seller_id / 店铺标识真实抓取待 TIK-018 实测修正。
- **登录等待超时对齐 PDD 模式**：经环境变量 ``TIKTOK_LOGIN_WAIT_TIMEOUT_MS`` 读取，缺省
  120000ms（120 秒），与 ``login.playwright_login._login_wait_timeout_ms`` 同口径（需求 4.5）。
- **登录成功导出**：Cookie 列表转 JSON 字符串（经 BrowserSession.export_cookies_json），
  并返回实际登录态目录 ``browser_data_dir`` 供 backend 落库、connect 时复用（免二次登录）。
- **复用公共浏览器启动**：经 ``login.browser_launcher`` 启动持久化上下文，与 PDD 同约定。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、导入置顶（51）、
日志禁用 debug（38）、地址经常量/环境变量（21）、不 import channel_pdd 内部。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import zlib
from typing import Any, Dict, List, Optional

from channel_tiktok.browser_session import BrowserSession
from channel_tiktok.selectors import (
    SELECTOR_LOGIN_CODE_INPUT,
    SELECTOR_LOGIN_ERROR_HINT,
    SELECTOR_LOGIN_MOBILE_INPUT,
    SELECTOR_LOGIN_PASSWORD_INPUT,
    SELECTOR_LOGIN_SUBMIT_BUTTON,
    TIKTOK_LOGIN_PATH,
    TIKTOK_SELLER_URL,
)
from common.core.config import get_settings

logger = logging.getLogger("channel_tiktok.tiktok_login")

# 登录等待超时默认值（毫秒）：预留人工完成验证码的时间，对齐 PDD _DEFAULT_LOGIN_WAIT_TIMEOUT_MS。
_DEFAULT_LOGIN_WAIT_TIMEOUT_MS: int = 120_000

# 账号密码登录是否无头：TikTok 登录含人工验证码，默认非无头（与 PDD BROWSER_HEADLESS 口径）。
_DEFAULT_LOGIN_HEADLESS: bool = False

# 「明确未登录 / 登录流程中」的 URL 路径标记（spike s1 同款语义）：URL 命中其一
# 即视为仍在登录页；登录成功后 URL 应跳离这些路径（泰国站实测落点 /homepage）。
_NOT_LOGIN_PATH_MARKERS: tuple[str, ...] = (
    "login",
    "signin",
    "sign-in",
    "auth",
    "register",
    "account/register",
    "signup",
    "sign-up",
    "passport",
    "oidc",
)


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


async def _wait_login_success(page: Any, wait_timeout_ms: int) -> bool:
    """轮询等待登录完成（URL 跳离登录页即成功）。

    等待期间若出现可见错误提示（账号 / 密码错误等）则提前返回 False，避免干等
    满超时；验证码（短信 / 图形）弹出时不做检测，自然等待人工完成。URL 判定
    复用 spike s1 的「登录 / 注册路径标记」语义，与登录失效标记口径一致。

    Args:
        page: Playwright 页面对象。
        wait_timeout_ms: 总等待超时（毫秒）。

    Returns:
        登录成功返回 True；超时 / 出现错误提示返回 False。
    """
    deadline = time.monotonic() + wait_timeout_ms / 1000.0
    code_hinted = False
    while time.monotonic() < deadline:
        # 出现可见错误提示（如「账号或密码错误」）→ 提前判定失败。
        if await _has_visible_error(page):
            return False
        # URL 已跳离登录页 → 登录成功。
        if await _is_logged_in_url(page):
            return True
        # 验证码输入框可见 → 提示人工输入（仅提示一次，避免刷屏）。
        if not code_hinted:
            try:
                if await page.is_visible(SELECTOR_LOGIN_CODE_INPUT):
                    logger.info(
                        "检测到验证码输入框，请在浏览器中人工输入短信/图形验证码"
                    )
                    code_hinted = True
            except Exception:  # noqa: BLE001 - 检测失败忽略，继续等待
                pass
        await page.wait_for_timeout(2000)
    logger.warning("登录等待超时（%d 秒），未检测到登录成功", wait_timeout_ms // 1000)
    return False


async def _is_logged_in_url(page: Any) -> bool:
    """按 URL 判定当前是否已登录（未命中任何登录 / 注册路径标记即视为已登录）。

    Args:
        page: Playwright 页面对象。

    Returns:
        已登录返回 True；求值失败返回 False（保守视为未登录）。
    """
    try:
        low_url = (await page.evaluate("location.href")).lower()
    except Exception:  # noqa: BLE001 - 求值失败视为未登录
        return False
    return not any(marker in low_url for marker in _NOT_LOGIN_PATH_MARKERS)


async def _has_visible_error(page: Any) -> bool:
    """检测登录页是否出现可见的错误提示（宽松选择器，见 SELECTOR_LOGIN_ERROR_HINT）。

    Args:
        page: Playwright 页面对象。

    Returns:
        存在可见错误提示返回 True；无 / 求值失败返回 False。
    """
    try:
        handle = await page.query_selector(SELECTOR_LOGIN_ERROR_HINT)
        if handle is None:
            return False
        return await handle.is_visible()
    except Exception:  # noqa: BLE001 - 检测失败保守视为无错误
        return False


async def _try_fetch_shop_name(page: Any) -> Optional[str]:
    """宽松尝试从登录后页面抓取店铺名称（TikTok 侧店铺标识，待 TIK-018 实测修正）。

    当前实现为「尽力而为」：从页面标题 / 文本中查找店铺名无稳定选择器，抓不到
    返回 None（由上层以账号名兜底），不阻塞登录流程。

    Args:
        page: Playwright 页面对象。

    Returns:
        抓到的店铺名称；抓不到返回 None。
    """
    try:
        title = await page.title()
        if title and "tiktok" not in title.lower():
            return title.strip()[:128]
    except Exception:  # noqa: BLE001 - 抓取失败返回 None
        pass
    return None


async def login_tiktok(name: str, password: str) -> Optional[Dict[str, Any]]:
    """使用账号密码登录 TikTok 泰国站卖家后台并导出 Cookie 与店铺信息（需求 4.1/4.2/4.5 对齐）。

    以非无头模式启动持久化上下文（默认，便于人工完成验证码），打开登录页并填写
    账号密码提交；等待登录成功（URL 跳离登录页 / 出现错误提示提前终止）直至超时。
    超时 / 异常统一降级返回 None（不抛异常）。成功则返回含 ``cookies_json`` /
    ``shop_id`` / ``shop_name`` / ``browser_data_dir`` 的字典。

    **登录页选择器来自 spike s6_login_form_map 实测**（2026-08-28，泰国站中文界面）：
    默认激活「手机号」登录 tab，直接填手机号 + 密码 + 点登录；登录后可能出现短信 /
    图形验证码，需人工完成（等待时间内）；shop_id 当前以账号名占位，oec_seller_id /
    店铺标识的真实抓取待 TIK-018 实测后回填（见模块 docstring 与 selectors.py）。

    Args:
        name: 登录账号名（手机号；作为隔离键派生登录期用户数据目录，并作
            shop_id 占位业务键）。
        password: 账号密码。

    Returns:
        登录成功返回 ``{"cookies_json": str, "shop_id": str, "shop_name":
        Optional[str], "name": str, "browser_data_dir": str}``；失败 / 超时 /
        异常返回 None。
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
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)

        # 已登录复用：登录态有效时打开登录页会前端跳转离开（homepage），免填表
        # 直接导出。先短暂等待 SPA 跳转稳定，未跳离才进入填表流程（幂等重登录 /
        # 掉线后自动恢复的免登路径，2026-08-28 实测登录态目录复用需要）。
        await page.wait_for_timeout(3000)
        if await _is_logged_in_url(page):
            logger.info("账号 '%s' 检测到已有有效登录态，直接复用（免二次验证）", name)
            cookies_json = await session.export_cookies_json()
            return {
                "cookies_json": cookies_json,
                "shop_id": name,
                "shop_name": name,
                "name": name,
                "browser_data_dir": session.user_data_dir,
            }

        # 等待登录表单渲染（SPA 动态渲染），填写账号密码并提交。
        await page.wait_for_selector(SELECTOR_LOGIN_MOBILE_INPUT, timeout=30_000)
        await page.fill(SELECTOR_LOGIN_MOBILE_INPUT, name)
        await page.fill(SELECTOR_LOGIN_PASSWORD_INPUT, password)
        await page.click(SELECTOR_LOGIN_SUBMIT_BUTTON)
        logger.info(
            "账号 '%s' 已提交登录表单，等待登录完成（最多 %d 秒，"
            "若有验证码请在浏览器中人工完成）",
            name,
            wait_timeout // 1000,
        )

        # 等待登录成功（URL 跳离登录页 / 出现错误提示提前终止）。
        if not await _wait_login_success(page, wait_timeout):
            logger.warning("账号 '%s' 登录未成功（超时或账号密码错误）", name)
            return None

        # 登录成功：导出 Cookie 与登录态目录，供 backend 落库与 connect 复用。
        cookies_json = await session.export_cookies_json()
        shop_name = await _try_fetch_shop_name(page) or name
        logger.info("账号 '%s' 登录成功，已导出 %d 个 Cookie", name, len(cookies_json))
        return {
            "cookies_json": cookies_json,
            "shop_id": name,
            "shop_name": shop_name,
            "name": name,
            "browser_data_dir": session.user_data_dir,
        }
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
