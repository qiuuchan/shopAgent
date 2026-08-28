# -*- coding: utf-8 -*-
"""
websocket 测试公共夹具与路径配置
================================
本文件用途：保证测试既能以 `channel_pdd.*` 形式导入 websocket 服务内部模块，
又能以 `common.*` 形式导入公共库。为此把「websocket 服务目录」与「仓库根目录」
均加入 sys.path（与 main.py 中的 sys.path 注入口径一致）。
"""
import os
import sys

# websocket 服务目录（本文件父目录的父目录 = tests 的父目录 = websocket/）
_WS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 仓库根目录 = websocket 目录的父目录（用于 import common.*）
_REPO_ROOT = os.path.dirname(_WS_DIR)

for _path in (_WS_DIR, _REPO_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# 以下导入依赖上方 sys.path 配置，故置于其后（pytest conftest 惯例例外）。
import types  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture()
def tiktok_enabled(monkeypatch: pytest.MonkeyPatch):
    """开启 TikTok 灰度开关并注入最小配置（TIK-017）。

    connection_manager 经 ``get_settings()`` 读取 ``tiktok_shop_enabled`` 等配置；
    默认配置下灰度开关为 false（TikTok 连接被拒），TikTok 装配类测试需本 fixture
    注入开启态的最小配置对象（monkeypatch 模块级引用，仅本测试生效）。
    """
    settings = types.SimpleNamespace(
        tiktok_shop_enabled=True,
        tiktok_max_browser_instances=4,
        tiktok_send_timeout_seconds=15.0,
        tiktok_poll_interval_seconds=5.0,
        tiktok_debounce_seconds=45.0,
    )
    monkeypatch.setattr(
        "channel_pdd.connection_manager.get_settings", lambda: settings
    )
    return settings


@pytest.fixture()
def tiktok_disabled(monkeypatch: pytest.MonkeyPatch):
    """注入「灰度开关关闭」的配置对象（与 tiktok_enabled 对称）。

    说明：不能依赖「默认配置为 false」——根目录 .env 若配置
    TIKTOK_SHOP_ENABLED=true（如验收窗口开启后），真实 get_settings() 会读到
    true，导致禁用态断言失败。禁用态测试必须经本 fixture 显式注入，保持封闭。
    """
    settings = types.SimpleNamespace(
        tiktok_shop_enabled=False,
        tiktok_max_browser_instances=4,
        tiktok_send_timeout_seconds=15.0,
        tiktok_poll_interval_seconds=5.0,
        tiktok_debounce_seconds=45.0,
    )
    monkeypatch.setattr(
        "channel_pdd.connection_manager.get_settings", lambda: settings
    )
    return settings
