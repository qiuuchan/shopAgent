# -*- coding: utf-8 -*-
"""
test_browser_launcher —— 公共浏览器启动逻辑单元测试（TIK-009）
============================================================
本文件用途：验证从 playwright_login 提取的公共浏览器启动逻辑：
- get_chromium_args：返回统一启动参数且为副本（调用方修改不影响原值）。
- clean_singleton_lock_files：清理残留 Singleton 锁文件（普通文件 / 符号链接）。
- launch_persistent_context_with_retry：首次失败时清锁重试一次并最终成功 /
  彻底失败时抛出末次异常；目标用户数据目录不可用时安全跳过。

不依赖真实浏览器与网络：通过 monkeypatch 注入 fake playwright / 文件系统辅助。
"""
from __future__ import annotations

import asyncio
import os

import pytest

import login.browser_launcher as bl


# ----------------------------------------------------------------------
# get_chromium_args
# ----------------------------------------------------------------------
def test_get_chromium_args_returns_copy():
    """返回的参数应为副本，外部修改不影响后续调用。"""
    args1 = bl.get_chromium_args()
    args1.append("--injected-by-test")
    args2 = bl.get_chromium_args()
    assert "--injected-by-test" not in args2
    assert args1 != args2


# ----------------------------------------------------------------------
# clean_singleton_lock_files
# ----------------------------------------------------------------------
def test_clean_removes_plain_lock_files(tmp_path):
    """残留普通锁文件应被删除。"""
    name = "shopA"
    for fname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (tmp_path / fname).write_text("x")
    bl.clean_singleton_lock_files(str(tmp_path), name)
    for fname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert not (tmp_path / fname).exists()


def test_clean_skips_when_dir_missing():
    """用户数据目录不存在时不应抛异常。"""
    bl.clean_singleton_lock_files("/nonexistent/path/xyz", "shopX")


def test_clean_ignores_unrelated_files(tmp_path):
    """不相关文件应被保留。"""
    (tmp_path / "keep.txt").write_text("data")
    bl.clean_singleton_lock_files(str(tmp_path), "shopA")
    assert (tmp_path / "keep.txt").exists()


# ----------------------------------------------------------------------
# launch_persistent_context_with_retry
# ----------------------------------------------------------------------
class _FakeContext:
    def __init__(self, tag=""):
        self.tag = tag


class _FakeChromium:
    def __init__(self, fail_first=False):
        self.fail_first = fail_first
        self.attempts = 0

    async def launch_persistent_context(self, user_data_dir, *, headless, args):
        self.attempts += 1
        if self.fail_first and self.attempts == 1:
            raise RuntimeError("PROFILE_IN_USE")
        return _FakeContext(tag=f"attempt{self.attempts}")


class _FakePlaywright:
    def __init__(self, fail_first=False):
        self.chromium = _FakeChromium(fail_first=fail_first)


def _make_fake_playwright(monkeypatch, fail_first=False):
    """构造注入用的 fake playwright，并吞掉 time.sleep。"""
    monkeypatch.setattr(bl.time, "sleep", lambda *_a, **_k: None)
    return _FakePlaywright(fail_first=fail_first)


def test_launch_success_first_try(monkeypatch, tmp_path):
    """首次即成功：返回上下文且只尝试一次。"""
    pw = _make_fake_playwright(monkeypatch, fail_first=False)

    def _run():
        return asyncio.run(
            bl.launch_persistent_context_with_retry(
                pw, str(tmp_path), "shopA", headless=True
            )
        )

    ctx = _run()
    assert isinstance(ctx, _FakeContext)
    assert pw.chromium.attempts == 1


def test_launch_retries_once_then_succeeds(monkeypatch, tmp_path):
    """首次失败清锁重试后成功。"""
    pw = _make_fake_playwright(monkeypatch, fail_first=True)

    def _run():
        return asyncio.run(
            bl.launch_persistent_context_with_retry(
                pw, str(tmp_path), "shopA", headless=True
            )
        )

    ctx = _run()
    assert isinstance(ctx, _FakeContext)
    assert pw.chromium.attempts == 2


def test_launch_raises_when_always_fails(monkeypatch, tmp_path):
    """两次均失败：抛出末次异常。"""
    pw = _make_fake_playwright(monkeypatch, fail_first=True)

    async def _always_fail(*_a, **_k):
        raise RuntimeError("PROFILE_IN_USE")

    pw.chromium.launch_persistent_context = _always_fail
    with pytest.raises(RuntimeError):
        asyncio.run(
            bl.launch_persistent_context_with_retry(
                pw, str(tmp_path), "shopA", headless=True
            )
        )


def test_launch_passes_custom_args(monkeypatch, tmp_path):
    """自定义启动参数应透传给底层 launch。"""
    captured = {}

    class _CaptureChromium:
        async def launch_persistent_context(self, user_data_dir, *, headless, args):
            captured["args"] = args
            captured["headless"] = headless
            return _FakeContext()

    class _CapturePW:
        chromium = _CaptureChromium()

    monkeypatch.setattr(bl.time, "sleep", lambda *_a, **_k: None)
    ctx = asyncio.run(
        bl.launch_persistent_context_with_retry(
            _CapturePW(), str(tmp_path), "shopA", headless=False,
            chromium_args=["--custom"],
        )
    )
    assert isinstance(ctx, _FakeContext)
    assert captured["args"] == ["--custom"]
    assert captured["headless"] is False


def test_module_importable():
    """模块可正常导入且导出预期符号。"""
    assert hasattr(bl, "get_chromium_args")
    assert hasattr(bl, "clean_singleton_lock_files")
    assert hasattr(bl, "launch_persistent_context_with_retry")
