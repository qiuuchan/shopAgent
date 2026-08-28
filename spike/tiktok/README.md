# TikTok spike 探查报告（TIK-001）

> 状态：进行中（脚本已就绪，待浏览器环境就绪后实跑）。
> 配套：选择器清单见 `selectors.md`；脚本目录见本目录 s1~s5。

## 站点与关键事实（2026-08-27 实测修正）

- **生产目标 = 泰国站**（非 PLAN 原假设美区）。后台域名实测为
  `https://seller.tiktokshopglobalselling.com`（多语言国际站，可中文界面）。
- 客服聊天页路径（用户提供）：`/chat/inbox/current`。
- 原 PLAN 中 `seller-us.tiktok.com`（美区）假设作废，spike 全部按泰国站执行；
  TIK-011/013/017 等涉及基址的工单需同步修正。

## 运行方式

```bash
cd spike/tiktok
python s1_login.py                       # ① 登录（headed 人工登录一次，持久化登录态）
python s2_dom_map.py                     # ② 聊天页 DOM 测绘 → selectors.md / selectors.json
python s3_network_listen.py --listen-seconds 120   # ③ 收消息协议监听（只听不重放）
python s4_send_message.py --count 10     # ④ DOM 发送全链路（需 s2 选择器）
python s5_headless_detect.py             # ⑤ headless 可检测性 + 资源测量
```

## 决策结论（实跑后填写）

| 决策项 | 结论 | 依据 |
| --- | --- | --- |
| 收消息方案（CDP 监听 vs DOM 轮询 3-5s） | **待定**：静置无 WS，需真实消息补测（s3 重跑） | s3 |
| headless vs Xvfb+headed | **headless 可用**（3 实例无风控/验证码，单实例 ≈75MB） | s5 |
| 聊天页 URL（TIKTOK_CHAT_PATH） | `/chat/inbox/current` + **必带 `oec_seller_id`**（实测确认） | s1/s2 |
| DOM 发送可行性 / 单条耗时基线 | 待定（需真实会话，s4 未跑） | s4 |
| 登录失效检测标记（LOGIN_PAGE_MARKERS） | `/account/login`（title「TikTok Shop Seller Log In \| Cross Border」） | s1 |
| 后台语言 | 中文界面（lng=zh-CN） | s1 |

## 实测记录

### s1 登录探查（2026-08-27，泰国站 seller.tiktokshopglobalselling.com）✅

- 登录页：`/account/login`（title「TikTok Shop Seller Log In | Cross Border」）；
- 登录后落点：`/homepage?lng=zh-CN&region_check=1&shop_region=TH`（中文界面）；
- **登录态持久化有效**：同 user-data-dir 无头重开免登成功，Cookie 36 个
  （含 passport_csrf_token / msToken / ttwid，域覆盖 tiktokshopglobalselling.com）；
- 店铺：FunToy Lab（泰国站），首页显示 12 小时响应率 88.89%；
- 聊天入口：侧边栏「客户消息」（`div.ub-navItem-b6df03`），点击**新开标签页**
  进入 `/chat/inbox/current?oec_seller_id=...&from=seller_center_navigation_im`；
- **关键约束**：聊天页 URL 需带 `oec_seller_id`（缺省时访问 `/chat/inbox/current`
  可能跳回 homepage；实测部分场景不带也能进，行为不稳定，正式实现务必带）；
- 直接打开 `/chat/inbox/current` 无参数时若已登录可停留聊天页（title Shop Chat →
  客服会话管理），未登录会被踢到 `/account/login`。

### s2 DOM 测绘（2026-08-27）✅（部分）

- 稳定选择器：`unread_badge` = `.p-badge`（3/3 次刷新命中）；
- 聊天页结构：侧边栏分类（收件箱/未分配/已分配/我/全部/紧急/已过期/未回复/未读/
  已加星标/已关闭），当前无会话时显示「暂无会话中用户」；
- **待补测**：会话列表项/输入框/发送按钮/消息气泡——当前无活跃会话不渲染，
  需真实买家消息出现后重跑 s2 补测（见 selectors.md「待确认项」）。

### s3 收消息协议监听（2026-08-27，静置 90 秒）⚠️ 无定论

- **WS 连接 0 个**：静置期间无 WebSocket 建立；
- 聊天相关 HTTP 响应仅静态资源（前端组件 chunk，应用代号 `pigeon-pc`：
  `Chat-ContactList-ContactPanel` / `Chat-ChatRoom-ChatInput` /
  `Chat-ChatRoom-MessageList` 等）；
- **结论待定**：当前无会话，无数据流可观测。收消息协议（WS vs 轮询）须等
  真实买家消息出现后重跑 s3（监听期内有消息进出时观察 WS 是否建立 / 轮询
  接口 URL）。

### s4 DOM 发送链路 — ⏸ 未跑（依赖真实会话）

- 需 s2 补测出会话列表项/输入框/发送按钮选择器后执行；
- 空店无会话时输入框/发送按钮不渲染，无法演练。

### s5 headless 检测与资源（2026-08-27）✅

- **headless 可用**：3 实例全部落到 homepage?shop_region=TH，无风控 URL、
  无验证码——生产可直接 headless，**无需 Xvfb**（websocket 镜像不用加 xvfb）；
- 资源实测：单实例加载后 ≈75-78MB（Windows，headless shell），3 实例合计
  ≈230MB；远低于 PLAN 预估 0.5-1GB/店，**4 店并发约 300MB**，容器内存限额
  与 TIKTOK_MAX_BROWSER_INSTANCES 可按此量级设置（仍建议 Linux 容器复测）；
- 进程定位：headless 进程名为 `chrome-headless-shell.exe`（非 chrome.exe）；
  Windows 下经 PowerShell Get-CimInstance 按命令行 user-data-dir 匹配。

### 其他关键事实（spike 附带发现）

- 卖家后台主界面为**中文**（`lng=zh-CN`），泰国站店铺 `FunToy Lab`；
- 聊天页 URL 显式带 `lang=en`，渲染语言待有会话后确认；
- 登录失效判定标记：登录页 URL `/account/login`（title「TikTok Shop Seller
  Log In | Cross Border」）——可作为 LOGIN_PAGE_MARKERS 基线。


