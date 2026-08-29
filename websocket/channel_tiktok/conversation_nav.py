# -*- coding: utf-8 -*-
"""
channel_tiktok.conversation_nav —— 会话卡精确导航（Phase 2 同名前缀多买家路由）
================================================================================
本文件用途：解决 TIK-018 遗留项 ③「会话卡按 ``:has-text`` 用户名子串定位，同名前缀
多买家存在误配」——捕获（``_capture_conversations``）与发送（``TikTokSender``）
两侧共用本模块的「收集会话卡 + 精确匹配 + nth-match 点击」逻辑。

设计要点（2026-08-29 Phase 2）：
- **JS 收集 + Python 匹配**：一次 ``evaluate`` 返回全部会话卡 ``{index, name,
  unread}``（index 为 DOM 顺序，name 取用户名元素 ``innerText.trim()`` 精确文本，
  unread 是否含 ``.p-badge``）；匹配逻辑为纯函数 ``find_exact_card_index``，
  可属性测试。
- **严格相等匹配**：``name`` 与卡内用户名 ``===`` 比较，同名前缀（如 ddy39s /
  ddy39s2）不再互相命中；TikTok 平台用户名全局唯一，完全同名重复命中视为歧义
  返回 None（防误发，宁漏勿错）。
- **nth-match 点击**：命中后 ``page.click(":nth-match(card, idx+1)")`` 精确点击
  目标卡（``:nth-match`` 语法项目已用于己方气泡增量检测，见 tiktok_sender）。
- 收集与点击在同一协程帧内完成，DOM 顺序稳定；跨帧变化由监控轮询自愈。

实现约束（开发规范）：单文件 ≤500 行（35）、中文注释（37/50）、日志禁用 debug（38）。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from channel_tiktok.selectors import SELECTOR_CONVERSATION_ITEM

logger = logging.getLogger("channel_tiktok.conversation_nav")

# 会话卡收集 JS：遍历会话列表全部会话卡，返回 ``[{index, name, unread}]``。
# - index：卡在 DOM 中的顺序（0 起），供 nth-match 点击；
# - name：卡内用户名元素（conversation_card_username）的 innerText 精确文本；
# - unread：卡内是否含未读角标（.p-badge）。
# 选择器与 selectors.py 常量保持同步（JS 字符串内不便引用 Python 常量）。
COLLECT_CARDS_JS: str = """
() => {
  const cards = Array.from(
    document.querySelectorAll('[data-testid="chat.chatroom.conversation_card"]')
  );
  const out = [];
  for (let i = 0; i < cards.length; i++) {
    const c = cards[i];
    const nameEl = c.querySelector(
      '[data-testid="chat.chatroom.conversation_card_username"]'
    );
    const name = nameEl ? (nameEl.innerText || '').trim() : '';
    out.push({index: i, name: name, unread: !!c.querySelector('.p-badge')});
  }
  return out;
}
"""


def find_exact_card_index(cards: Any, name: str) -> Optional[int]:
    """在会话卡列表中精确匹配买家用户名，返回卡 DOM 索引（纯函数）。

    匹配规则：
    - 卡内用户名与目标 ``name`` **严格相等**（``===``），同名前缀不互相命中；
    - 无匹配 → 返回 None（发送侧视为失败，捕获侧跳过该卡）；
    - **完全同名重复命中** → 返回 None（平台用户名全局唯一，理论不出现；歧义
      时宁失败不误发，行为对调用方透明，日志由调用方记录）。

    Args:
        cards: ``evaluate(COLLECT_CARDS_JS)`` 产出列表（可含非字典脏数据）。
        name: 目标买家用户名（非空字符串）。

    Returns:
        命中卡在 DOM 中的索引（0 起）；无匹配 / 重复命中 / 参数非法返回 None。
    """
    if not isinstance(cards, list) or not name:
        return None
    hits: List[int] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        if card.get("name") == name:
            idx = card.get("index")
            if isinstance(idx, int):
                hits.append(idx)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        logger.warning("买家用户名重复命中 %s 个会话卡（按歧义跳过）: name=%s", len(hits), name)
    return None


async def click_conversation_exact(page: Any, name: str) -> bool:
    """按精确买家用户名点击目标会话卡（evaluate 收集 → 精确匹配 → nth-match）。

    Args:
        page: Playwright 页面（或注入假对象，需提供异步 ``evaluate`` / ``click``）。
        name: 目标买家用户名。

    Returns:
        命中并点击成功返回 True；无匹配 / 重复命中 / 页面异常返回 False。
    """
    try:
        cards = await page.evaluate(COLLECT_CARDS_JS)
        idx = find_exact_card_index(cards, name)
        if idx is None:
            return False
        await page.click(f":nth-match({SELECTOR_CONVERSATION_ITEM}, {idx + 1})")
        return True
    except Exception as exc:  # noqa: BLE001 - 单卡导航失败由调用方决定跳过/失败
        logger.warning("精确点击会话卡失败（按未命中处理）: name=%s, %s", name, exc)
        return False


__all__ = [
    "COLLECT_CARDS_JS",
    "find_exact_card_index",
    "click_conversation_exact",
]
