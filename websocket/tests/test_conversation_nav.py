# -*- coding: utf-8 -*-
"""
websocket.tests.test_conversation_nav —— 会话卡精确导航测试（Phase 2）
=======================================================================
本文件用途：验证 ``channel_tiktok.conversation_nav`` 的同名前缀精确路由能力：

- ``find_exact_card_index`` 纯函数：严格相等匹配、同名前缀不互相命中、无匹配 /
  完全同名重复命中返回 None（防误发）；Hypothesis 属性测试覆盖组合场景；
- ``click_conversation_exact``：假页面注入验证 evaluate 收集 → 精确匹配 →
  nth-match 点击的调用链，以及未命中 / 页面异常时的 False 容错。

不依赖真实浏览器（延续项目「mock 完成全部测试」硬约束）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

from hypothesis import given, settings, strategies as st

from channel_tiktok.conversation_nav import (
    COLLECT_CARDS_JS,
    click_conversation_exact,
    find_exact_card_index,
)


# ----------------------------------------------------------------------
# find_exact_card_index 纯函数测试
# ----------------------------------------------------------------------
def test_exact_match_single_card():
    """单卡精确命中 → 返回该卡 DOM 索引。"""
    cards = [{"index": 3, "name": "ddy39s", "unread": True}]
    assert find_exact_card_index(cards, "ddy39s") == 3


def test_same_prefix_not_confused():
    """同名前缀（ddy39s / ddy39s2）严格相等匹配，互不误配。"""
    cards = [
        {"index": 0, "name": "ddy39s", "unread": True},
        {"index": 1, "name": "ddy39s2", "unread": True},
    ]
    assert find_exact_card_index(cards, "ddy39s") == 0
    assert find_exact_card_index(cards, "ddy39s2") == 1


def test_no_match_returns_none():
    """无精确匹配 → None。"""
    cards = [{"index": 0, "name": "alice", "unread": True}]
    assert find_exact_card_index(cards, "bob") is None


def test_duplicate_exact_name_returns_none():
    """完全同名重复命中（歧义）→ None（宁失败不误发）。"""
    cards = [
        {"index": 0, "name": "ddy39s", "unread": True},
        {"index": 1, "name": "ddy39s", "unread": True},
    ]
    assert find_exact_card_index(cards, "ddy39s") is None


def test_empty_name_returns_none():
    """空目标名 / 空卡列表 / 非列表输入 → None（不抛异常）。"""
    assert find_exact_card_index([{"index": 0, "name": "a"}], "") is None
    assert find_exact_card_index([], "a") is None
    assert find_exact_card_index(None, "a") is None
    assert find_exact_card_index("not-a-list", "a") is None


def test_dirty_card_entries_tolerated():
    """卡列表含非字典脏数据时不影响其余卡的匹配。"""
    cards = ["dirty", None, {"index": 5, "name": "alice"}, 42]
    assert find_exact_card_index(cards, "alice") == 5


@given(
    st.lists(
        st.fixed_dictionaries(
            {"index": st.integers(min_value=0, max_value=50),
             "name": st.text(max_size=10)},
        ),
        max_size=10,
    ),
    # 空目标名的防御语义（恒返回 None）由 test_empty_name_returns_none 单测覆盖，
    # 属性测试聚焦非空目标的精确匹配充要条件。
    st.text(max_size=10).filter(lambda s: s != ""),
)
@settings(max_examples=200)
def test_property_exact_match_semantics(cards, target):
    """属性测试：精确命中的充要条件是「恰一张卡用户名 == target」。

    验证结果与语义一致：命中 → 恰 1 张卡用户名严格相等且返回其索引；
    未命中（None）→ 非恰 1 张相等（0 张无匹配 / ≥2 张同名歧义）。
    """
    exact = [c["index"] for c in cards if c["name"] == target]
    idx = find_exact_card_index(cards, target)
    if len(exact) == 1:
        assert idx == exact[0]
    else:
        assert idx is None


# ----------------------------------------------------------------------
# click_conversation_exact 假页面测试
# ----------------------------------------------------------------------
class FakeNavPage:
    """记录调用的假页面：evaluate 返回可配置卡片，click 记录选择器。"""

    def __init__(self, cards: Optional[List[Dict[str, Any]]] = None,
                 click_raise: bool = False) -> None:
        self.cards = cards if cards is not None else []
        self.click_raise = click_raise
        self.clicked: List[str] = []
        self.eval_count = 0

    async def evaluate(self, js: str, *args: Any) -> Any:
        assert js == COLLECT_CARDS_JS  # 点击路径必须使用收集 JS
        self.eval_count += 1
        return self.cards

    async def click(self, selector: str) -> None:
        if self.click_raise:
            raise RuntimeError("click failed")
        self.clicked.append(selector)


async def _run_click(page: FakeNavPage, name: str) -> bool:
    return await click_conversation_exact(page, name)


def test_click_exact_hit_uses_nth_match():
    """命中 → 按精确匹配索引构造 nth-match 选择器点击。"""
    page = FakeNavPage([
        {"index": 0, "name": "ddy39s", "unread": True},
        {"index": 1, "name": "ddy39s2", "unread": True},
    ])
    assert asyncio.run(_run_click(page, "ddy39s2")) is True
    assert page.eval_count == 1
    assert page.clicked == [":nth-match([data-testid=\"chat.chatroom.conversation_card\"], 2)"]


def test_click_exact_miss_returns_false():
    """未命中 → False，且不产生点击。"""
    page = FakeNavPage([{"index": 0, "name": "alice", "unread": True}])
    assert asyncio.run(_run_click(page, "bob")) is False
    assert page.clicked == []


def test_click_exact_duplicate_returns_false():
    """完全同名重复 → False（防误发），不产生点击。"""
    page = FakeNavPage([
        {"index": 0, "name": "ddy39s", "unread": True},
        {"index": 1, "name": "ddy39s", "unread": True},
    ])
    assert asyncio.run(_run_click(page, "ddy39s")) is False
    assert page.clicked == []


def test_click_exact_evaluate_error_returns_false():
    """evaluate 抛异常 → False（容错，不影响调用方主链路）。"""
    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=RuntimeError("page closed"))
    page.click = AsyncMock()
    assert asyncio.run(_run_click(page, "any")) is False
    page.click.assert_not_awaited()


def test_click_exact_click_error_returns_false():
    """点击本身抛异常 → False（容错）。"""
    page = FakeNavPage(
        [{"index": 0, "name": "ddy39s", "unread": True}], click_raise=True
    )
    assert asyncio.run(_run_click(page, "ddy39s")) is False


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-q"])
