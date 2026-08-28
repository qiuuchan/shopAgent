# -*- coding: utf-8 -*-
"""
common.tests.test_tiktok_config —— TikTok 通道配置默认值与覆盖测试
================================================================
本文件用途：验证 common.core.config.Settings 中 TikTok 通道配置项
（TIK-017）的默认值与环境变量覆盖行为：

- 默认值：灰度总开关默认关闭（false）、轮询 5.0s、去抖 45.0s、发送超时 15.0s、
  登录等待 120000ms、最大浏览器实例 4；
- 覆盖：经环境变量（或 .env）可覆盖各项，pydantic-settings 按字段名大小写
  不敏感地映射（如 tiktok_shop_enabled ↔ TIKTOK_SHOP_ENABLED）。

测试隔离：以 ``Settings(_env_file=None)`` 直接构造，不读取仓库根目录 .env、
不触碰 lru_cache 单例，避免受本机 .env 实际值影响。
"""
from __future__ import annotations

from common.core.config import Settings


def test_tiktok_config_defaults():
    """TikTok 配置项默认值与 PLAN §5.6 / TIK-017 一致。"""
    settings = Settings(_env_file=None)
    assert settings.tiktok_shop_enabled is False
    assert settings.tiktok_poll_interval_seconds == 5.0
    assert settings.tiktok_debounce_seconds == 45.0
    assert settings.tiktok_send_timeout_seconds == 15.0
    assert settings.tiktok_login_wait_timeout_ms == 120_000
    assert settings.tiktok_max_browser_instances == 4


def test_tiktok_config_env_override(monkeypatch):
    """环境变量可覆盖各项默认值（字段名大小写不敏感映射）。"""
    monkeypatch.setenv("TIKTOK_SHOP_ENABLED", "true")
    monkeypatch.setenv("TIKTOK_POLL_INTERVAL_SECONDS", "7.5")
    monkeypatch.setenv("TIKTOK_DEBOUNCE_SECONDS", "60")
    monkeypatch.setenv("TIKTOK_SEND_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("TIKTOK_LOGIN_WAIT_TIMEOUT_MS", "180000")
    monkeypatch.setenv("TIKTOK_MAX_BROWSER_INSTANCES", "6")

    settings = Settings(_env_file=None)
    assert settings.tiktok_shop_enabled is True
    assert settings.tiktok_poll_interval_seconds == 7.5
    assert settings.tiktok_debounce_seconds == 60.0
    assert settings.tiktok_send_timeout_seconds == 20.0
    assert settings.tiktok_login_wait_timeout_ms == 180_000
    assert settings.tiktok_max_browser_instances == 6


__all__ = [
    "test_tiktok_config_defaults",
    "test_tiktok_config_env_override",
]
