# TikTok 卖家中心聊天页选择器清单（spike 实测）

> 本文件由 ``s2_dom_map.py`` 生成：候选选择器经 3 次刷新验证稳定后收录。
> 站点：泰国站 ``seller.tiktokshopglobalselling.com``（生产目标，中文界面）。

## 已确认的入口与页面事实（s1/s2 实测）

| 语义 | 选择器 / URL | 说明 |
| --- | --- | --- |
| 聊天入口（侧边栏） | ``div.ub-navItem-b6df03:has-text('客户消息')`` | 首页导航，点击新开标签页 |
| 聊天入口（IM 按钮） | ``div.ub-IMButton-d003ce`` | 首页 IM 悬浮按钮（备用） |
| 聊天页 URL 模板 | ``{base}/chat/inbox/current?oec_seller_id={id}&shop_region=TH&lang=en&cb_shop_region=TH&from=seller_center_navigation_im`` | 必须带 oec_seller_id |
| 实测聊天页 URL | ``https://seller.tiktokshopglobalselling.com/chat/inbox/current?oec_seller_id=7494494994748966018&shop_region=TH&lang=en&cb_shop_region=TH&from=seller_center_navigation_im`` | s2 使用的完整 URL |
| 登录页 URL | ``/account/login`` | title「TikTok Shop Seller Log In | Cross Border」 |

## 当前有效选择器

| 语义分组 | 选择器 | 验证 |
| --- | --- | --- |
| unread_badge | ``.p-badge`` | 3/3 次刷新命中 |

## 待确认项（需真实买家会话出现后补测）

- ``conversation_item``：会话列表条目（当前无会话，未测绘到）；
- ``message_input`` / ``send_button``：消息输入框与发送按钮（无活跃会话时不渲染）；
- ``message_list`` / ``my_message_bubble``：消息流与己方气泡；
- ``LOGIN_PAGE_MARKERS``：登录失效判定标记（登录页 URL 为 ``/account/login``）。