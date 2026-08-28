# -*- coding: utf-8 -*-
"""
spike.tiktok.s5_headless_detect —— 探查 5：headless 可检测性与资源测量
====================================================================
本文件用途（对齐 PLAN_TIKTOK.md §2.1 s5_headless_detect.py 通过标准）：
- 验证 headless 模式下登录 / 操作是否触发风控页 / 验证码（对比 headed）；
- 测量单实例与 4 并发实例的 CPU / 内存占用，给出资源基线；
- 输出「headless vs Xvfb+headed」结论，供 Phase 1 部署决策（websocket 镜像
  是否加 xvfb 依赖、容器内存限额、TIKTOK_MAX_BROWSER_INSTANCES）。

设计说明：
- 复用 s1 登录态（--user-data-dir）；用无头模式打开卖家中心首页并停留若干秒，
  检查是否跳转风控 / 验证码页（URL 特征）或出现验证码元素；
- 资源测量：浏览器进程内存经 ``psutil`` 读取（缺失时回退 Windows 的
  ``tasklist`` / Linux 的 ``/proc``，仅记录主进程 RSS 估算）；CPU 采样 3 次取均值；
- 4 并发实例用 4 个独立 user-data-dir 目录（复制登录态目录后各自启动）。

运行方式：``python s5_headless_detect.py [--user-data-dir ...]``
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from _common import TIKTOK_SELLER_URL, launch_context, setup_logging

logger = logging.getLogger("spike.tiktok.s5_headless_detect")

# 本文件所在目录（spike/tiktok），默认路径基于它定位。
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))

# 风控 / 验证码页 URL 特征（spike 实测后补充）。
_RISK_URL_MARKERS: tuple[str, ...] = ("captcha", "verify", "risk", "challenge", "security")

# 验证码元素选择器候选（出现任一即判定触发了验证码）。
_CAPTCHA_SELECTORS: tuple[str, ...] = (
    "iframe[src*='captcha']",
    "iframe[src*='verify']",
    "[data-e2e='captcha']",
    "[class*='captcha']",
    "[class*='verify']",
)

# 无头模式下页面停留时间（秒）。
_STAY_SECONDS: int = 20

# CPU 采样次数。
_CPU_SAMPLES: int = 3


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TikTok headless 可检测性探查 s5")
    parser.add_argument(
        "--user-data-dir",
        default=os.path.join(_SCRIPT_DIR, "_data", "user_test1"),
        help="已登录的持久化用户数据目录（s1 产物）",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=4,
        help="并发实例数（默认 4，需登录态目录可复制）",
    )
    parser.add_argument("--headless", action="store_true", default=True, help="使用无头模式")
    return parser.parse_args()


def _measure_process_memory_mb(pid: int) -> Optional[float]:
    """读取指定进程的内存占用（MB）。

    Windows 经 PowerShell Get-Process 读取 WorkingSet64；其他平台读
    ``/proc/<pid>/status``。失败返回 None（不阻塞测量）。

    Args:
        pid: 进程 ID。

    Returns:
        常驻内存 MB；不可用时返回 None。
    """
    try:
        if sys.platform == "win32":
            ps1 = (
                f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                "if ($p) { [Console]::WriteLine([math]::Round($p.WorkingSet64 / 1MB, 1)) }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps1],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            if out:
                return float(out)
            return None
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
        return None
    except Exception:  # noqa: BLE001 - 测量失败不阻塞
        return None


def _measure_process_cpu_percent(pid: int) -> Optional[float]:
    """粗略采样进程 CPU 占用（连续两次进程 CPU 时间差 / 墙钟时间差）。

    Args:
        pid: 进程 ID。

    Returns:
        CPU 百分比估算；不可用返回 None。
    """
    try:
        if sys.platform == "win32":
            return None  # Windows 无 /proc，CPU 采样跳过（内存为主指标）
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            parts = fh.read().split()
        # utime(14) + stime(15)（1-indexed 字段）。
        t0 = int(parts[13]) + int(parts[14])
        time.sleep(1)
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            parts = fh.read().split()
        t1 = int(parts[13]) + int(parts[14])
        delta = (t1 - t0) / os.sysconf("SC_CLK_TCK")
        return max(0.0, delta) * 100.0  # 单核百分比
    except Exception:  # noqa: BLE001
        return None


def _find_chromium_pids(user_data_dir: str) -> List[int]:
    """按 user-data-dir 定位 chromium 主进程 PID。

    persistent context 不暴露 browser.process，改用命令行特征匹配：
    chromium 进程命令行包含 ``user-data-dir=<dir>`` 的为浏览器主进程。
    Windows 经 PowerShell Get-CimInstance 查命令行，其他平台读 /proc。

    Args:
        user_data_dir: 用户数据目录（用于匹配命令行）。

    Returns:
        匹配到的 PID 列表。
    """
    marker = os.path.normpath(user_data_dir)
    pids: List[int] = []
    try:
        if sys.platform == "win32":
            # headless 模式下进程名为 chrome-headless-shell.exe；headed 为 chrome.exe。
            ps1 = (
                "Get-CimInstance Win32_Process "
                "-Filter \"Name like '%chrome%' or Name like '%chromium%'\" "
                "| ForEach-Object { if ($_.CommandLine) { "
                "[Console]::WriteLine($_.ProcessId.ToString() + '|' + $_.CommandLine) }}"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps1],
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout
            for line in out.splitlines():
                pid_s, _, cmd = line.partition("|")
                if marker.lower() in cmd.lower() and pid_s.strip().isdigit():
                    pids.append(int(pid_s.strip()))
        else:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as fh:
                        cmd = fh.read().decode("utf-8", "ignore").replace("\x00", " ")
                    if marker in cmd:
                        pids.append(int(pid))
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001 - 匹配失败返回空
        return []
    return pids


async def _probe_headless(user_data_dir: str, headless: bool) -> Dict[str, Any]:
    """以指定模式打开卖家中心，检测风控 / 验证码并测量资源。

    Args:
        user_data_dir: 用户数据目录。
        headless: 是否无头。

    Returns:
        探测结果字典。
    """
    playwright = None
    context = None
    try:
        playwright, context = await launch_context(user_data_dir, headless=headless)
        # 持久化上下文拿不到 browser.process，经命令行特征定位主进程。
        pid = None
        for _ in range(10):
            pids = _find_chromium_pids(user_data_dir)
            if pids:
                pid = pids[0]
                break
            await asyncio.sleep(0.5)
        memory_before = _measure_process_memory_mb(pid) if pid else None

        page = await context.new_page()
        await page.goto(TIKTOK_SELLER_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(_STAY_SECONDS * 1000)

        final_url = await page.evaluate("location.href")
        low_url = final_url.lower()
        risk_by_url = any(m in low_url for m in _RISK_URL_MARKERS)
        captcha_visible = False
        for sel in _CAPTCHA_SELECTORS:
            try:
                if await page.locator(sel).count() > 0:
                    captcha_visible = True
                    break
            except Exception:  # noqa: BLE001
                continue

        memory_after = _measure_process_memory_mb(pid) if pid else None
        cpus: List[Optional[float]] = []
        for _ in range(_CPU_SAMPLES):
            cpus.append(_measure_process_cpu_percent(pid) if pid else None)
            await page.wait_for_timeout(500)

        result = {
            "headless": headless,
            "pid": pid,
            "memory_before_mb": memory_before,
            "memory_after_mb": memory_after,
            "cpu_percent_samples": [c for c in cpus if c is not None],
            "final_url": final_url,
            "risk_by_url": risk_by_url,
            "captcha_visible": captcha_visible,
        }
        logger.info(
            "headless=%s 探测: URL=%s 风控URL=%s 验证码=%s 内存=%sMB",
            headless,
            final_url,
            risk_by_url,
            captcha_visible,
            memory_after,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("headless=%s 探测失败: %s", headless, exc)
        return {"headless": headless, "error": str(exc)}
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001
                pass


async def _prepare_concurrent_dirs(base_dir: str, count: int) -> List[str]:
    """复制登录态目录生成 count 个独立 user-data-dir（供并发测量）。

    Args:
        base_dir: 已登录的源目录。
        count: 需要的实例数。

    Returns:
        实例目录列表（含 base_dir 本身）。
    """
    dirs = [base_dir]
    for i in range(1, count):
        target = f"{base_dir}_clone{i}"
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        # 复制除锁文件外的全部内容。
        shutil.copytree(
            base_dir,
            target,
            ignore=shutil.ignore_patterns("SingletonLock", "SingletonCookie", "SingletonSocket"),
        )
        dirs.append(target)
    logger.info("已准备 %s 个并发实例目录", len(dirs))
    return dirs


async def main() -> int:
    args = _parse_args()
    setup_logging()
    logger.info("开始 headless 可检测性与资源测量（s5）")

    if not os.path.isdir(args.user_data_dir):
        logger.error("登录态目录不存在: %s（请先运行 s1_login.py 完成登录）", args.user_data_dir)
        return 1

    results: Dict[str, Any] = {"single": {}, "concurrent": []}

    # 单实例 headless 探测（含风控检测）。
    results["single"] = await _probe_headless(args.user_data_dir, headless=args.headless)

    # 并发实例资源测量。
    if args.concurrent > 1:
        dirs = await _prepare_concurrent_dirs(args.user_data_dir, args.concurrent)
        tasks = [_probe_headless(d, headless=args.headless) for d in dirs]
        results["concurrent"] = await asyncio.gather(*tasks)

    # 汇总结论。
    mems = [
        r.get("memory_after_mb")
        for r in [results["single"]] + results["concurrent"]
        if r.get("memory_after_mb") is not None
    ]
    risk_flags = [
        r.get("risk_by_url") or r.get("captcha_visible")
        for r in [results["single"]] + results["concurrent"]
        if isinstance(r, dict)
    ]

    report = {
        "结论": (
            "headless 可用（无风控/验证码触发）"
            if not any(risk_flags)
            else "headless 疑似触发风控/验证码，建议 Xvfb+headed"
        ),
        "实例数": len(mems),
        "平均常驻内存_MB": round(sum(mems) / len(mems), 1) if mems else None,
        "合计常驻内存_MB": round(sum(mems), 1) if mems else None,
        "detail": results,
    }
    out_path = os.path.join(_SCRIPT_DIR, "headless_report.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    logger.info("headless 报告已写出: %s", out_path)
    logger.info("汇总: %s", json.dumps(report, ensure_ascii=False)[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
