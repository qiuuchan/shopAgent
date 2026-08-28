# -*- coding: utf-8 -*-
"""
test_tiktok_channel —— TikTokChannel 主循环单元测试（TIK-013）
================================================================
本文件用途：在**不真实启动浏览器**的前提下验证 ``TikTokChannel`` 的关键行为（关联
工单 TIK-013）：

- 生命周期 ``start`` / ``stop`` 的任务清理（mock browser_session）；
- 营业时间窗口外 ``start`` 不启动浏览器（mock 营业时间查询）；
- 登录失效检测 → 经 ``event_notifier``（AlertDedup 链路）触发 ``login_expired`` 告警；
  登录失效后恢复探测（Phase 2 保活：人工重登后续接监控，见 test_login_recovery）；
- 监控循环快照 diff → 去抖 → 买家消息入队（mock 快照与队列）；
- ``diff_conversations`` 纯函数属性测试（Hypothesis）。

不依赖真实浏览器 / 真实数据库：浏览器会话与营业时间查询经 monkeypatch 注入桩，
告警通知器以 Mock 断言调用，符合「mock 完成全部测试，不真实联调」硬约束。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings, strategies as st

import channel_tiktok.tiktok_channel as tc_mod
from channel_tiktok.session_guard import ReplyDebouncer
from channel_tiktok.tiktok_channel import TikTokChannel, diff_conversations
from channel_pdd.message_queue import FifoMessageQueue


@pytest.fixture
def fake_browser():
    """最小假浏览器会话：page.url 可注入，goto/start/close 为协程桩。"""
    browser = AsyncMock()
    page = MagicMock()
    page.url = "https://seller.tiktokshopglobalselling.com/chat/inbox/current"
    page.goto = AsyncMock()
    browser.page = page
    return browser


@pytest.fixture
def in_hours(monkeypatch):
    """mock 营业时间查询：无配置（视为全天营业，窗口内）。"""
    monkeypatch.setattr(tc_mod, "_query_business_hours", lambda pk: None)


# ----------------------------------------------------------------------
# 1. 生命周期 start / stop 任务清理
# ----------------------------------------------------------------------
def test_start_then_stop_cleans_tasks(monkeypatch, fake_browser, in_hours):
    """start 创建监控/消费任务，stop 取消任务并释放浏览器、置 DISCONNECTED。"""
    queue: FifoMessageQueue = FifoMessageQueue(name="test:1")
    ch = TikTokChannel(
        shop_id="t1",
        user_id=1,
        shop_pk=1,
        message_queue=queue,
        message_handler=AsyncMock(),
        browser_session=fake_browser,
        poll_interval=0.01,
    )

    async def _run():
        await ch.start()
        await asyncio.sleep(0.05)
        assert ch._monitor_task is not None
        assert ch._consume_task is not None
        await ch.stop()

    asyncio.run(_run())

    # 浏览器确实被拉起并释放。
    assert fake_browser.start.called
    assert fake_browser.close.called
    # 任务已清理。
    assert ch._monitor_task is None
    assert ch._consume_task is None
    # 终态 DISCONNECTED。
    assert ch.get_connection_status()["state"] == "disconnected"


def test_stop_without_start_is_safe(monkeypatch, fake_browser, in_hours):
    """未 start 直接 stop 不应抛异常（资源均为 None 时安全跳过）。"""
    queue: FifoMessageQueue = FifoMessageQueue(name="test:2")
    ch = TikTokChannel(
        shop_id="t2",
        user_id=2,
        shop_pk=2,
        message_queue=queue,
        message_handler=AsyncMock(),
        browser_session=fake_browser,
    )
    asyncio.run(ch.stop())
    assert ch.get_connection_status()["state"] == "disconnected"


# ----------------------------------------------------------------------
# 2. 时间窗外 start 不启动浏览器
# ----------------------------------------------------------------------
def test_start_outside_business_hours_skips_browser(monkeypatch, fake_browser):
    """窗口外：营业时间查询返回配置且 is_within_business_hours=False → 不启动浏览器。"""
    monkeypatch.setattr(
        tc_mod,
        "_query_business_hours",
        lambda pk: {
            "start_time": "08:00",
            "end_time": "22:00",
            "enabled": True,
            "weekdays": "",
        },
    )
    monkeypatch.setattr(tc_mod, "is_within_business_hours", lambda *a, **k: False)

    queue: FifoMessageQueue = FifoMessageQueue(name="test:3")
    ch = TikTokChannel(
        shop_id="t3",
        user_id=3,
        shop_pk=3,
        message_queue=queue,
        message_handler=AsyncMock(),
        browser_session=fake_browser,
    )

    asyncio.run(ch.start())

    # 浏览器绝未被拉起，且不登记注册表（此处仅验证未启动）。
    assert not fake_browser.start.called
    assert ch._monitor_task is None
    # 状态置为断开（窗口外）。
    assert ch.get_connection_status()["state"] == "disconnected"


def test_start_within_business_hours_starts_browser(monkeypatch, fake_browser):
    """窗口内（is_within_business_hours=True）应正常启动浏览器。"""
    monkeypatch.setattr(
        tc_mod,
        "_query_business_hours",
        lambda pk: {
            "start_time": "08:00",
            "end_time": "22:00",
            "enabled": True,
            "weekdays": "",
        },
    )
    monkeypatch.setattr(tc_mod, "is_within_business_hours", lambda *a, **k: True)

    queue: FifoMessageQueue = FifoMessageQueue(name="test:4")
    ch = TikTokChannel(
        shop_id="t4",
        user_id=4,
        shop_pk=4,
        message_queue=queue,
        message_handler=AsyncMock(),
        browser_session=fake_browser,
        poll_interval=0.01,
    )

    async def _run():
        await ch.start()
        await asyncio.sleep(0.03)
        await ch.stop()

    asyncio.run(_run())
    assert fake_browser.start.called
    assert ch.get_connection_status()["state"] == "disconnected"


# ----------------------------------------------------------------------
# 3. 登录失效检测 → 告警触发
# ----------------------------------------------------------------------
def test_login_expired_triggers_alert(monkeypatch, fake_browser, in_hours):
    """监控循环中 page.url 命中登录页 → 置状态并经 event_notifier 触发 login_expired。"""
    # 注入登录页 URL（命中 LOGIN_PAGE_MARKERS）。
    fake_browser.page.url = (
        "https://seller.tiktokshopglobalselling.com/account/login"
    )
    notifier = MagicMock()
    queue: FifoMessageQueue = FifoMessageQueue(name="test:5")
    ch = TikTokChannel(
        shop_id="t5",
        user_id=5,
        shop_pk=5,
        message_queue=queue,
        message_handler=AsyncMock(),
        browser_session=fake_browser,
        event_notifier=notifier,
        poll_interval=0.01,
        # 登录失效后恢复探测：关闭（max_tries=0 → 立即返回 False → 退出监控），
        # 保持与 Phase 1 行为等价（失效即停止循环）。
        login_recovery_max_tries=0,
    )

    async def _run():
        await ch.start()
        await asyncio.sleep(0.05)
        await ch.stop()

    asyncio.run(_run())

    # 告警被触发且事件类型为 login_expired。
    assert notifier.called
    event_types = [c.args[0] for c in notifier.call_args_list]
    assert "login_expired" in event_types
    # 状态置为断开（登录失效）。
    assert ch.get_connection_status()["state"] == "disconnected"


def test_login_expiry_recovery_resumes_monitor(monkeypatch, fake_browser, in_hours):
    """登录失效后登录态恢复（人工重登）→ 自动续接监控，不再退出（Phase 2 保活）。"""
    # 登录页 URL 初始命中；首轮探测后恢复为聊天页 URL（模拟人工重登）。
    urls = [
        "https://seller.tiktokshopglobalselling.com/account/login",
        "https://seller.tiktokshopglobalselling.com/account/login",
        "https://seller.tiktokshopglobalselling.com/chat/inbox/current",
    ]
    fake_browser.page.url = urls[0]
    # 每轮探测前刷新 page.url（用生成器模拟登录态从失效 → 恢复）。
    orig = fake_browser.page.url

    def _url():
        if len(urls) > 1:
            urls.pop(0)
        return urls[0]

    type(fake_browser.page).url = property(lambda self: _url())

    notifier = MagicMock()
    queue: FifoMessageQueue = FifoMessageQueue(name="test:5b")
    ch = TikTokChannel(
        shop_id="t5b",
        user_id=5,
        shop_pk=5,
        message_queue=queue,
        message_handler=AsyncMock(),
        browser_session=fake_browser,
        event_notifier=notifier,
        poll_interval=0.01,
        login_recovery_interval=0.01,
        login_recovery_max_tries=10,
    )
    # 关闭去抖，使恢复后快照直接入队。
    ch._debouncer = ReplyDebouncer(silence_seconds=0.0)

    async def _run():
        await ch.start()
        await asyncio.sleep(0.15)
        await ch.stop()

    asyncio.run(_run())

    # 登录失效告警已触发，但监控循环未被终止（恢复后继续运行）。
    assert notifier.called
    event_types = [c.args[0] for c in notifier.call_args_list]
    assert "login_expired" in event_types
    # 恢复后重开聊天页被调用（_open_chat_page → page.goto）。
    assert fake_browser.page.goto.called


def test_connection_disconnected_on_snapshot_error(monkeypatch, fake_browser, in_hours):
    """快照抓取异常视为连接断开 → 触发 connection_disconnected 告警并停循环。"""
    notifier = MagicMock()
    queue: FifoMessageQueue = FifoMessageQueue(name="test:6")
    ch = TikTokChannel(
        shop_id="t6",
        user_id=6,
        shop_pk=6,
        message_queue=queue,
        message_handler=AsyncMock(),
        browser_session=fake_browser,
        event_notifier=notifier,
        poll_interval=0.01,
    )
    # 让快照抓取抛异常。
    ch._capture_conversations = AsyncMock(side_effect=RuntimeError("DOM 失配"))

    async def _run():
        await ch.start()
        await asyncio.sleep(0.05)
        await ch.stop()

    asyncio.run(_run())
    event_types = [c.args[0] for c in notifier.call_args_list]
    assert "connection_disconnected" in event_types


# ----------------------------------------------------------------------
# 4. 监控循环 diff → 去抖 → 入队
# ----------------------------------------------------------------------
def test_monitor_loop_enqueues_new_buyer(monkeypatch, fake_browser, in_hours):
    """监控循环经 diff 发现新买家消息 → 去抖期满 → 入队（message_queue.put）。"""
    queue: FifoMessageQueue = FifoMessageQueue(name="test:7")
    ch = TikTokChannel(
        shop_id="t7",
        user_id=7,
        shop_pk=7,
        message_queue=queue,
        message_handler=AsyncMock(),
        browser_session=fake_browser,
        poll_interval=0.01,
    )
    # 缩短去抖静默期，使 pop_due 立即返回（测试加速）。
    ch._debouncer = ReplyDebouncer(silence_seconds=0.0)

    buyer_snap = {
        "c1": {
            "conversation_id": "c1",
            "from_user": "user",
            "sender_role": "buyer",
            "msg_id": "m1",
            "content": "在吗",
        }
    }

    calls = {"n": 0}

    def _cap():
        calls["n"] += 1
        # 首轮空快照，其后保持同一买家消息（DIFF 不会重复入队）。
        return {} if calls["n"] == 1 else buyer_snap

    ch._capture_conversations = AsyncMock(side_effect=_cap)

    async def _run():
        await ch.start()
        await asyncio.sleep(0.08)
        await ch.stop()

    asyncio.run(_run())

    # 买家原始报文已入队并交由 message_handler 消费（FIFO 消费循环驱动）。
    assert ch._message_handler.called
    handler_payloads = [
        c.args[0]
        for c in ch._message_handler.call_args_list
        if isinstance(c.args[0], dict)
    ]
    assert any(p.get("msg_id") == "m1" for p in handler_payloads)


# ----------------------------------------------------------------------
# 5. diff_conversations 纯函数：单测 + 属性测试（Hypothesis）
# ----------------------------------------------------------------------
def test_diff_returns_new_buyer():
    old = {"c1": {"conversation_id": "c1", "from_user": "user", "msg_id": "a"}}
    new = {"c1": {"conversation_id": "c1", "from_user": "user", "msg_id": "b"}}
    res = diff_conversations(old, new)
    assert len(res) == 1 and res[0]["msg_id"] == "b"


def test_diff_ignores_unchanged_buyer():
    old = {"c1": {"conversation_id": "c1", "from_user": "user", "msg_id": "a"}}
    new = dict(old)
    assert diff_conversations(old, new) == []


def test_diff_ignores_seller_and_none():
    old = {"c1": {"conversation_id": "c1", "from_user": "user", "msg_id": "a"}}
    new = {
        "c1": {"conversation_id": "c1", "from_user": "mall_cs", "msg_id": "x"},
        "c2": {"conversation_id": "c2", "from_user": "user", "msg_id": "y"},
    }
    res = diff_conversations(old, new)
    # c1 卖家消息不算；c2 新买家算。
    assert [m["conversation_id"] for m in res] == ["c2"]


def test_diff_handles_none_inputs():
    assert diff_conversations(None, None) == []
    assert diff_conversations(None, {"c1": {"from_user": "user", "msg_id": "a"}}) == [
        {"from_user": "user", "msg_id": "a"}
    ]


@st.composite
def _old_new_pair(draw):
    """生成 (old, new) 会话快照对：new 由 old 经「same/bump/role_swap/drop」变换并追加新会话得到。"""
    n = draw(st.integers(0, 6))
    old: Dict[str, Any] = {}
    new: Dict[str, Any] = {}
    for i in range(n):
        cid = f"c{i}"
        role = draw(st.sampled_from(["user", "mall_cs", "system"]))
        mid = draw(st.integers(0, 1_000_000))
        old[cid] = {"conversation_id": cid, "from_user": role, "msg_id": mid}
        choice = draw(st.sampled_from(["same", "bump", "role_swap", "drop"]))
        if choice == "same":
            new[cid] = {"conversation_id": cid, "from_user": role, "msg_id": mid}
        elif choice == "bump":
            new[cid] = {"conversation_id": cid, "from_user": role, "msg_id": mid + 1}
        elif choice == "role_swap":
            new_role = "mall_cs" if role == "user" else "user"
            new[cid] = {
                "conversation_id": cid,
                "from_user": new_role,
                "msg_id": mid + 7,
            }
        # drop：不写入 new。
    extra = draw(st.integers(0, 3))
    for j in range(extra):
        cid = f"x{j}"
        role = draw(st.sampled_from(["user", "mall_cs", "system"]))
        mid = draw(st.integers(0, 1_000_000))
        new[cid] = {"conversation_id": cid, "from_user": role, "msg_id": mid}
    return old, new


@given(_old_new_pair())
@settings(max_examples=100)
def test_diff_properties(pair):
    """diff_conversations 契约的属性测试。

    不变式：
    1) 返回项均为买家消息（``from_user == 'user'``）；
    2) 返回项均来自 ``new``；
    3) 返回集合 == 期望集合（new 中买家消息且 old 无该会话或 msg_id 不同）。
    """
    old, new = pair
    result: List[Dict[str, Any]] = diff_conversations(old, new)

    # 1) 全部为买家。
    assert all(m.get("from_user") == "user" for m in result)

    # 2) 全部来自 new。
    new_keys = {
        (m.get("conversation_id"), m.get("msg_id")) for m in new.values()
    }
    for m in result:
        assert (m.get("conversation_id"), m.get("msg_id")) in new_keys

    # 3) 与期望集合一致。
    expected: List[Dict[str, Any]] = []
    for cid, m in new.items():
        if m.get("from_user") != "user":
            continue
        prev = old.get(cid)
        prev_id = prev.get("msg_id") if isinstance(prev, dict) else None
        if prev is None or prev_id != m.get("msg_id"):
            expected.append(m)
    assert {
        (m.get("conversation_id"), m.get("msg_id")) for m in result
    } == {(m.get("conversation_id"), m.get("msg_id")) for m in expected}


@given(_old_new_pair())
@settings(max_examples=100)
def test_diff_empty_when_identical(pair):
    """当 old == new（同对象内容）时，diff 必为空（无新买家消息）。"""
    old, _ = pair
    # 构造与 old 完全等同的 new。
    new = {cid: dict(m) for cid, m in old.items()}
    assert diff_conversations(old, new) == []


__all__ = [
    "test_start_then_stop_cleans_tasks",
    "test_stop_without_start_is_safe",
    "test_start_outside_business_hours_skips_browser",
    "test_start_within_business_hours_starts_browser",
    "test_login_expired_triggers_alert",
    "test_login_expiry_recovery_resumes_monitor",
    "test_connection_disconnected_on_snapshot_error",
    "test_monitor_loop_enqueues_new_buyer",
    "test_diff_returns_new_buyer",
    "test_diff_ignores_unchanged_buyer",
    "test_diff_ignores_seller_and_none",
    "test_diff_handles_none_inputs",
    "test_diff_properties",
    "test_diff_empty_when_identical",
]
