# -*- coding: utf-8 -*-
"""
channel_tiktok.selectors —— TikTok 卖家后台选择器 / 地址常量清单
==============================================================
本文件用途：集中声明 TikTok 泰国站卖家后台的「URL 基址 / 关键路径 / 登录失效标记 /
聊天页入口选择器」，作为 TIK-012/013 的 DOM 知识点唯一事实来源。所有取值均来自
TIK-001 spike 实测（``spike/tiktok/selectors.md`` 与 ``spike/tiktok/README.md``），
未实测部分以「待确认项」注释标明，避免臆测。

重要事实（以 spike 实测为准，2026-08-27）：
- **生产目标 = 泰国站**：后台域名为 ``seller.tiktokshopglobalselling.com``（多语言国际站，
  可中文界面），非早期假设的美区 ``seller-us.tiktok.com``。该基址为平台固定地址，允许
  写死（参照 demand 5.1 对 PDD 固定基址的口径，规范 21 例外：平台固定地址不属于
  「可漂移的部署地址」，故允许常量固化）。
- **聊天页 URL 必须带 ``oec_seller_id``**：实测确认，缺省访问 ``/chat/inbox/current``
  可能跳回 homepage（行为不稳定），正式实现务必带上该参数（见 TIKTOK_CHAT_PATH 注释）。
- **登录失效标记**：登录页路径为 ``/account/login``（title「TikTok Shop Seller Log In
  | Cross Border」），可作为登录态失效判定基线（LOGIN_PAGE_MARKERS）。

实现约束（开发规范）：全中文注释（37/50）、单文件 ≤500 行（35）、日志禁用 debug（38）。
"""
from __future__ import annotations

# ----------------------------------------------------------------------
# 站点基址与关键路径（泰国站实测，允许写死）
# ----------------------------------------------------------------------

# TikTok 卖家后台泰国站固定基址（spike/README 实测修正，生产目标）。
# 说明：该域名为 TikTok 卖家跨境平台固定地址，非部署可漂移地址，允许常量固化。
TIKTOK_SELLER_URL: str = "https://seller.tiktokshopglobalselling.com"

# 登录页路径（spike selectors.md：title「TikTok Shop Seller Log In | Cross Border」）。
TIKTOK_LOGIN_PATH: str = "/account/login"

# 登录后首页落点路径（spike s1：``/homepage?lng=zh-CN&region_check=1&shop_region=TH``）。
TIKTOK_HOME_PATH: str = "/homepage"

# 客服聊天页路径（spike 实测：用户提供的客服聊天入口为 ``/chat/inbox/current``）。
# 重要约束：正式打开聊天页 URL 必须带 ``oec_seller_id`` 参数，否则可能跳回 homepage
# （spike/README「关键约束」实测：缺省时行为不稳定，部分场景不带也能进，但正式实现
# 务必带）。完整模板见 ``TIKTOK_CHAT_URL_TEMPLATE``。
TIKTOK_CHAT_PATH: str = "/chat/inbox/current"

# 聊天页完整 URL 模板（spike selectors.md 实测聊天页 URL）：
#   {base}/chat/inbox/current?oec_seller_id={id}&shop_region=TH&lang=en
#   &cb_shop_region=TH&from=seller_center_navigation_im
# 使用方式：``TIKTOK_CHAT_URL_TEMPLATE.format(base=TIKTOK_SELLER_URL, id=<oec_seller_id>)``。
TIKTOK_CHAT_URL_TEMPLATE: str = (
    "{base}/chat/inbox/current"
    "?oec_seller_id={id}&shop_region=TH&lang=en&cb_shop_region=TH"
    "&from=seller_center_navigation_im"
)


# ----------------------------------------------------------------------
# 登录失效判定标记（LOGIN_PAGE_MARKERS）
# ----------------------------------------------------------------------
# 登录页路径集合：页面 URL 命中其一即视为「未登录 / 登录态失效」（供 TIK-013 监控循环
# 检测登录失效）。当前以 spike 实测的 ``/account/login`` 为基线（s2 待确认项亦指向此）。
LOGIN_PAGE_MARKERS: tuple[str, ...] = (
    TIKTOK_LOGIN_PATH,  # "/account/login"
)

# IM 会话过期弹窗标记（TIK-018 实测补充，2026-08-29）：
# 主站登录态有效时，IM 子系统会话过期在聊天页以 .p-modal 弹窗呈现（URL 不跳转），
# 文案「Your login has expired, please log in again」（URL 模板固定 lang=en）。
# 探测取 .p-modal 的 innerText 做包含匹配（大小写不敏感），故以短语而非整句为准。
# 同步发现：以过期/错误的 oec_seller_id 直连聊天页也会出现同款弹窗——出现该弹窗时
# 应同时核对店铺 oec_seller_id 是否与当前卖家后台一致。
IM_EXPIRED_MODAL_SELECTOR: str = ".p-modal"
IM_EXPIRED_MODAL_MARKERS: tuple[str, ...] = (
    "login has expired",  # "Your login has expired, please log in again"
)


# ----------------------------------------------------------------------
# 聊天页入口 / 会话结构选择器（以 spike selectors.md 为唯一事实来源）
# ----------------------------------------------------------------------
# 首页侧边栏「客户消息」导航（点击新开标签页进入聊天页）。
SELECTOR_CHAT_NAV: str = "div.ub-navItem-b6df03:has-text('客户消息')"

# 首页 IM 悬浮按钮（备用入口）。
SELECTOR_IM_BUTTON: str = "div.ub-IMButton-d003ce"

# 未读消息角标（spike：``.p-badge``，3/3 次刷新命中，稳定）。
SELECTOR_UNREAD_BADGE: str = ".p-badge"


# ----------------------------------------------------------------------
# 登录页表单选择器（s6_login_form_map 实测，2026-08-28）
# ----------------------------------------------------------------------
# 泰国站登录页（/account/login）实测结构（spike/tiktok/login_form_report.json）：
# - 默认激活「手机号」登录 tab（SPAN.panel-item.selected text=手机号），手机号与
#   密码输入框可见，邮箱输入框隐藏；无需切换 tab；
# - 区号选择为隐藏下拉（Area_Select 输入框默认不可见），未测到默认区号，登录时
#   直接填手机号，若平台报「号码/区号」错误由上层提示人工处理；
# - 登录后可能弹出验证码输入框（短信/图形，见 SELECTOR_LOGIN_CODE_INPUT），
#   此时需人工输入（登录流程预留等待时间）。
# 手机号登录输入框（type=tel，name=mobile）。
SELECTOR_LOGIN_MOBILE_INPUT: str = "#TikTok_Ads_SSO_Login_Mobile_Input"
# 密码输入框（type=password，name=password）。
SELECTOR_LOGIN_PASSWORD_INPUT: str = "#TikTok_Ads_SSO_Login_Pwd_Input"
# 登录提交按钮（name=loginBtn，text=登录）。
SELECTOR_LOGIN_SUBMIT_BUTTON: str = "#TikTok_Ads_SSO_Login_Btn"
# 验证码输入框（登录后若触发短信/图形验证码则可见，需人工输入）。
SELECTOR_LOGIN_CODE_INPUT: str = "#TikTok_Ads_SSO_Login_Code_Input"
# 登录表单错误提示的宽松选择器（页面出现可见错误信息时提前终止等待）。
SELECTOR_LOGIN_ERROR_HINT: str = ".error-msg, .error_message, .arco-message-error"



# 以下选择器尚未测绘（空店无活跃会话时不渲染），仅声明占位语义，供 TIK-012 引用时
# 明确「待补测」边界，避免提前写成臆测选择器：
#   SELECTOR_CONVERSATION_ITEM：会话列表条目
#   SELECTOR_MESSAGE_INPUT：消息输入框
#   SELECTOR_SEND_BUTTON：发送按钮
#   SELECTOR_MESSAGE_LIST：消息流容器
#   SELECTOR_MY_MESSAGE_BUBBLE：己方消息气泡
# 正式取值待 TIK-012 阶段以真实会话重跑 spike s2 后回填本文件。


__all__ = [
    "TIKTOK_SELLER_URL",
    "TIKTOK_LOGIN_PATH",
    "TIKTOK_HOME_PATH",
    "TIKTOK_CHAT_PATH",
    "TIKTOK_CHAT_URL_TEMPLATE",
    "LOGIN_PAGE_MARKERS",
    "SELECTOR_CHAT_NAV",
    "SELECTOR_IM_BUTTON",
    "SELECTOR_UNREAD_BADGE",
    "SELECTOR_LOGIN_MOBILE_INPUT",
    "SELECTOR_LOGIN_PASSWORD_INPUT",
    "SELECTOR_LOGIN_SUBMIT_BUTTON",
    "SELECTOR_LOGIN_CODE_INPUT",
    "SELECTOR_LOGIN_ERROR_HINT",
]
