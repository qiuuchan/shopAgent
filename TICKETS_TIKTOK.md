# TikTok 二开工单池（TICKETS_TIKTOK）

> 配套设计文档：[PLAN_TIKTOK.md](./PLAN_TIKTOK.md)（含全部行号/接缝核对，工单不重复展开，细节以该文为准）。
> 工单编号前缀 `TIK-`；批次/依赖严格对齐 PLAN_TIKTOK.md 第 10 节「实施顺序」。
> 状态标记：`[ ]` 待办 / `[x]` 完成。
>
> **每张工单的通用出口准则**（除各自验收标准外必须全部满足）：
> ① 该工单对应的新增测试（PLAN §6.1 映射，见各工单「验收」）全绿；② 不破坏存量回归基线（backend 207 / websocket 176 / scheduler 17 / common 22）；③ 新增 Python 文件 ≤500 行、模块头中文 docstring、全中文注释与文案；④ 对 PDD 现有路径零行为变更。

## 0. 总览

| 工单 | 标题 | 批次 | 依赖 | 预估(人日) | 状态 |
| --- | --- | --- | --- | --- | --- |
| TIK-001 | Phase 0 spike 探查（s1–s5） | A1 | 外部：测试店账号 + 企微 Webhook | 2–3 | [x] 主体完成（s3/s4 待真实消息补测） |
| TIK-002 | Shop.platform 数据层（列 + 迁移 + 字典） | A2 | — | 0.5 | [x] 已交付（2026-08-27 验证） |
| TIK-003 | backend 平台分派链（店铺/登录/连接/发送） | A2 | TIK-002 | 1 | [x] 已交付（2026-08-27 验证） |
| TIK-004 | 前端店铺平台 UI（radio + 徽标 + api） | A2 | TIK-002 | 0.5–1 | [x] 已交付（2026-08-28 验证） |
| TIK-005 | 掉线/登录失效告警链路补全（AlertDedup + 触发点） | A3 | — | 1 | [x] 已交付（2026-08-27 验证） |
| TIK-006 | 营业时间星期维度（weekdays 全链路） | A4 | — | 1–1.5 | [x] 已交付（2026-08-27 验证） |
| TIK-007 | 前端营业时间星期多选（BusinessHoursPanel） | A4 | TIK-006 | 0.5 | [x] 已交付（2026-08-28 验证） |
| TIK-008 | MessageConsumer parser 解耦 + 硬 import 惰性化 | B1 | TIK-002* | 1–1.5 | [x] 已交付（2026-08-27 验证） |
| TIK-009 | login/browser_launcher.py 提取 | B4 | TIK-001 | 0.5 | [x] 已交付（2026-08-27 验证） |
| TIK-010 | channel_base 协议 + connection_manager 工厂化 | B2 | TIK-002、TIK-008 | 1 | [x] 已交付（2026-08-27 验证，TikTok 分支为桩待 TIK-013） |
| TIK-011 | channel_tiktok 基础件（browser_session / login / selectors） | B3 | TIK-001、TIK-009 | 1.5–2 | [x] 已交付（2026-08-27 验证，selectors 待 TIK-018 实测回填） |
| TIK-012 | TikTok 消息解析/发送/会话守卫（message / sender / guard） | B3 | TIK-001、TIK-011 | 1.5–2 | [x] 已交付（2026-08-27 验证） |
| TIK-013 | TikTokChannel 主循环（监控 + 登录失效检测） | B3 | TIK-005、TIK-011、TIK-012 | 1.5–2 | [x] 已交付（2026-08-27 验证） |
| TIK-014 | TikTokChannel 装配 + routes/connections 联调 | C1 | TIK-010、TIK-013 | 1 | [x] 已交付（2026-08-28 验证） |
| TIK-015 | scheduler tiktok_window 任务 | C2 | TIK-002、TIK-006、TIK-014 | 1–1.5 | [x] 已交付（2026-08-28 验证） |
| TIK-016 | websocket routes login/messages/cookies 平台分派 | C3 | TIK-002、TIK-011、TIK-013 | 1 | [x] |
| TIK-017 | TIKTOK_* 配置 + Dockerfile/资源调整 | D2 | TIK-001 | 0.5–1 | [x] 已交付（2026-08-28 验证） |
| TIK-018 | 测试店端到端验收（周末窗口实测） | D1 | TIK-014 ~ TIK-017 全部 | 1 + 周末实测 | [ ] 工具已备（2026-08-28），待周末窗口实测 |

> \* TIK-008 逻辑上独立，按 PLAN 批次约束挂在 A2 之后；若 A2 阻塞可先行开工（注意与 TIK-005 无冲突）。

**关键路径**：`TIK-001 → TIK-011/012 → TIK-013 → TIK-014 → TIK-018`（TIK-001 需测试店账号，建议最先启动并尽早向用户要账号/Webhook）。

**外部依赖（阻塞项）**：
- 1 个 TikTok 泰国站测试店账号（账密），需人工配合过验证码 → 阻塞 TIK-001，进而阻塞 TIK-009/011/012/013/017。
- 企微群机器人 Webhook URL → TIK-005/018 联调用。
- 测试店后台配合确认消息收发、验证 spike 发出的消息确实到达买家侧 → TIK-001/018。

**批次并行建议**：
- 批次 A（TIK-001~007）互不依赖，可同时开工；其中 TIK-002 是 B 批次前置，优先排。
- 批次 B：TIK-008 与 TIK-009 可并行；TIK-011/012 可并行（同依赖 TIK-001）；TIK-013 等 011/012。
- 批次 C（TIK-014/015/016）依赖 B 全部，可并行；批次 D 收尾。

## 0.1 派发与调度方案（最快完成）

**结论：总工期由依赖链决定，不是人手。** 中值估算最快约 **11.5 个工作日**（上限约 13 天），另加 TIK-018 周末窗口实测（日历依赖）。**最少 2 人即可达到该工期**；3–4 人可压缩关键路径；5 人以上边际收益趋零，多余人手建议投入 Phase 2 前置（message_queue 上移 common、店铺级代理字段设计）或验收用例预演。

**关键路径（决定工期，须 1 人专责、中间不停）**：

```
TIK-001(2.5d) → TIK-009(0.5d) → TIK-011(1.75d) → TIK-012(1.75d) → TIK-013(1.75d) → TIK-014(1d) → TIK-015(1.25d) → TIK-018(1d)
```

**2 人派发（最少最优）**：

| 人 | 任务序列（按依赖序） |
| --- | --- |
| A（关键路径专责） | 001 → 009 → 011 → 012 → 013 → 014 → 015 → 018 |
| B（清空全部分支） | 002 → 003 → 004 → 005 → 006 → 007 → 008 → 010 → 017 → 016（等 013 完成）→ 协助 018 |

**4 人推荐派发（最快、零文件冲突）**：

| 人 | 领单（按顺序） | 说明 |
| --- | --- | --- |
| A（关键路径专责） | 001 → 011 → 012 → 013 → 014 → 015 → 018 | 全程一条线不换手；009 由 D 提前完成，A 不占用 |
| B（分支主力） | 002 → 005 → 008 → 010 → 017 → 016 → 协助 018 | 002 第一天做完解锁全队；改 connection_manager（005/010）与 message_consumer（008） |
| C（分支） | 006 → 007 → 003 → 004 → 018 验收脚本准备 → 协助 | 006 先于 B 的 008 做（同改 message_consumer，时间错开不冲突） |
| D（提前量专责） | 009 → 012 纯逻辑（session_guard / tiktok_message 骨架 + 测试）→ 011 骨架（browser_session / selectors 框架 + 测试）→ 协助组装 | 001 期间并行开发不依赖 spike 的部分；001 完成后 A 只填 selectors + 组装，关键路径缩短 1–2 天 |

**2 人 / 3 人简化版**：2 人 = 上表 A + B（B 顺延吃掉 C/D 全部单）；3 人 = A + B + C（C 兼做 D 的提前量）。

**里程碑（防空转）**：① Day ~2.5 TIK-001 完成 → 评审 spike 报告 + selectors.md → 才开 011/012；② Day ~7.75 TIK-013 完成 → 才开 014/015/016；③ C 批全部完成 → 018 排周末窗口实测。

**加速原则（人手充足时的正确用法）**：

1. **TIK-001 最先启动**：它是外部阻塞（测试店账号）。脚本框架可先写好，账号到位即运行；开工第一天就向用户要账号 + 企微 Webhook。
2. **提前做不依赖 spike 的部分**：session_guard 纯逻辑、tiktok_message 骨架、browser_launcher 主体（headless 参数留配置）均不依赖 spike 结论，可在 001 期间并行开发；001 完成后关键路径上只剩「填 selectors + 联调」。
3. **同文件冲突归同一人**：connection_manager（TIK-005/010/014）、message_consumer（TIK-006/008）由同一人连续做，避免合并冲突。
4. **验收用例提前备**：TIK-018 的对账脚本、告警演练清单在 016 完成后即可编写（不依赖 018 开工），把 018 压缩为「周末窗口实测执行」。
5. **日历对齐周末**：TIK-018 需要真实买家消息 + 无人值守窗口，排期尽量让关键路径在周五前后完成，否则要多等一个周末。

---

## 批次 A（互不依赖，可同时开工）

### TIK-001 Phase 0 spike 探查（s1–s5）

- **批次**：A1 ｜ **依赖**：外部（测试店账号、企微 Webhook） ｜ **预估**：2–3 人日
- **状态**：✅ **主体完成（2026-08-27）**——s1/s5 全绿、s2 部分、s4 未跑、s3 待定；**剩余项：需真实买家消息出现后补跑 s2/s3/s4**（会话选择器 + 收消息协议 + 发送链路），企微 Webhook 到位后联调告警。
- **说明**：新建独立目录 `spike/tiktok/`（不进服务代码、不打包），用 5 个脚本验证 TikTok 通道关键技术前提；只读探查，不改主代码。
- **涉及文件**：`spike/tiktok/s1_login.py`、`s2_dom_map.py`、`s3_network_listen.py`、`s4_send_message.py`、`s5_headless_detect.py`、`spike/tiktok/selectors.md`、`spike/tiktok/README.md`（产出报告）
- **实测关键结论**（详见 `spike/tiktok/README.md`）：
  - **站点修正**：生产目标为**泰国站** `seller.tiktokshopglobalselling.com`（非美区）；后台中文界面；
  - s1 ✅：登录态持久化 + 二次免登 + Cookie 导出（36 个）；聊天页 URL 必须带 `oec_seller_id`；
  - s2 ✅部分：`unread_badge=.p-badge` 稳定；会话/输入框待真实消息补测；
  - s3 ⚠️：静置 90s 无 WS，收消息协议未定论，待真实消息重跑；
  - s4 ⏸：未跑（依赖会话选择器）；
  - s5 ✅：headless 可用（无风控/验证码、无需 Xvfb），单实例 ≈75MB、4 店 ≈300MB。
- **需要用户配合**：测试店账号/密码 + 人工过验证码；企微 Webhook URL；**真实买家消息**（补测 s2/s3/s4）；后台确认消息收发。
- **验收/产出**：① spike 报告（`spike/tiktok/README.md`）；② `selectors.md` 选择器清单；③ 三项方案决策落地结论：收消息方案（**待定**，需真实消息）、headless 方案（**headless 可用**，`TIKTOK_CHAT_PATH` 实测为 `/chat/inbox/current` + `oec_seller_id`）、发送耗时基线（**待定**）。
- **阻塞下游**：TIK-009/011/012/013/017。

### TIK-002 Shop.platform 数据层（列 + 迁移 + 字典）

- **批次**：A2 ｜ **依赖**：— ｜ **预估**：0.5 人日
- **说明**：数据层引入店铺平台字段，是全部平台分派的前置。存量店铺自动归 'pdd'，零阻力。
- **涉及文件**：
  - `common/models/shop_models.py`：Shop 加 `platform` 列（`String(32), default='pdd', nullable=False, comment="所属平台：pdd/tiktok"`），SchemaMigrator 启动幂等补列。
  - `common/services/dict_seed_data.py`：加 `platform` 字典（pdd=拼多多 / tiktok=TikTok Shop），供前端下拉与列表徽标。
- **验收**：① 启动迁移后存量店铺 `platform='pdd'`、可写入 'tiktok'；② 字典种子生成；③ backend 店铺列表响应含 platform（脱敏口径不变）；④ `common` 测试全绿。

### TIK-003 backend 平台分派链（店铺/登录/连接/发送）

- **批次**：A2 ｜ **依赖**：TIK-002 ｜ **预估**：1 人日
- **说明**：backend 侧 platform 全链路透传（默认 'pdd' 向后兼容）；websocket 侧路由接收 platform 的实际分派在 TIK-016/014 落地，本工单只做 backend 侧。
- **涉及文件**：
  - `backend/app/api/routes/shops.py`：UpsertShopRequest / PasswordLoginShopRequest 加 `platform`（默认 'pdd'，校验合法枚举）。
  - `backend/app/services/account_service.py`：`upsert_shop`(:128) / `login_shop_by_password`(:266) / `import_shop_by_cookie`(:314) 加 platform 参数并入库/透传。
  - `backend/app/services/shop_login_client.py`：`login_by_password` / `import_by_cookie` 加 platform 透传给 websocket `/login/*`。
  - `backend/app/services/connection_notify.py`：`notify_connect` / `notify_disconnect` / `query_connected*` 加 platform 透传（默认 'pdd'）。
  - `backend/app/services/chat_send_client.py`：~~发送前查 Shop.platform，TikTok 直接返回中文失败（Phase 1 在线聊天「可看不可发」）~~（**Phase 2 已撤销拦截，2026-08-28**：平台分派收敛到 websocket 侧按 Shop.platform 完成，TikTok 在线手动发送已支持）。
- **验收**：① 新增 `backend/tests/test_shop_platform.py`（shops 路由 platform 字段校验、登录分派透传、chat_send_client TikTok 拦截）；② backend 存量 207 测试回归全绿；③ PDD 路径行为不变。

### TIK-004 前端店铺平台 UI（radio + 徽标 + api）

- **批次**：A2 ｜ **依赖**：TIK-002 ｜ **预估**：0.5–1 人日
- **涉及文件**：
  - `frontend/src/pages/shop_management.vue`：添加店铺弹窗加「平台」radio（拼多多默认 / TikTok Shop）；选 TikTok 仅展示账密登录 tab；列表加平台徽标列。
  - `frontend/src/api/shop_api.js`：loginShopByPassword 等请求体带 platform。
- **验收**：① `npm run build` 通过；② platform 随请求体提交且默认 'pdd'；③ 列表平台徽标正确渲染。

### TIK-005 掉线/登录失效告警链路补全（AlertDedup + 触发点）

- **批次**：A3 ｜ **依赖**：—（PDD 独立受益，立即可上） ｜ **预估**：1 人日
- **说明**：PLAN §5.4。`connection_disconnected` / `login_expired` 事件全仓库目前无触发点，是本工程上线前置条件；本次补全 PDD 侧与通用去重组件，TikTok 侧触发点在 TIK-013。
- **涉及文件**：
  - `websocket/engine/alert_dedup.py`（新）：`AlertDedup(silence_seconds=1800.0)`，`should_send(key)` / `mark_sent(key)` / `resolve(key)`，键 `(shop_pk, event_type)`；重连过程（RECONNECTING）不告警，只终局告警，恢复时 resolve。
  - `websocket/channel_pdd/pdd_channel.py`：构造器加可选 `event_notifier`；`_connect_with_retry` 达上限置 ERROR 分支(:296-305) 经 AlertDedup 触发 `connection_disconnected` 告警（经 `build_notifier` → POST `/api/v1/notify/events`）。
  - `websocket/routes/cookies.py` / `pdd_login.refresh_pdd_cookies`：刷新失败标记 relogin 处补触发（PDD）。
  - `websocket/channel_pdd/connection_manager.py`：创建时构造一份 AlertDedup，同时注入 consumer（业务事件）与 channel（连接事件），共用同一实例。
- **验收**：① 新增 `websocket/tests/test_alert_dedup.py`（静默窗口去重、resolve 后可再发）；② mock build_notifier 验证 PDD 重连失败→企微告警事件体（shop_pk 正确）；③ websocket 存量 176 测试回归全绿。
- **注意**：与 TIK-010 同文件 connection_manager，若并行注意合并冲突。

### TIK-006 营业时间星期维度（weekdays 全链路）

- **批次**：A4 ｜ **依赖**：— ｜ **预估**：1–1.5 人日
- **说明**：营业时间支持按星期生效（周末场景核心），engine 与 scheduler 共用纯函数，旧调用方零改动兼容。
- **涉及文件**：
  - `common/utils/weekdays.py`（新）：星期判定纯函数（engine 与 scheduler 共用，scheduler 不 import websocket 包，符合规范 52）。
  - `common/models/config_models.py`：BusinessHours 加 `weekdays` 列（`String(32), nullable=True, comment="生效星期，逗号分隔 0-6（周一=0），空=每天"`）。
  - `websocket/engine/business_hours.py`：`is_within_business_hours(...)` 加可选 kwarg `weekdays`（None/空=每天）。
  - `websocket/engine/reply_engine.py`：ShopConfig 加 `business_weekdays`（默认 None）；decide_reply 透传。
  - `websocket/engine/message_consumer.py`：`load_shop_runtime` 读 weekdays 填入 ShopConfig。
  - `backend/app/services/business_hours_service.py` + `backend/app/api/routes/business_hours.py`：读写 weekdays（校验 0-6 逗号串或空）。
- **验收**：① 新增 `websocket/tests/test_business_hours_weekdays.py`（Hypothesis 属性测试：周中/周末/空值兼容/跨午夜+星期组合）；② 既有调用方零改动、兼容旧数据；③ backend 接口读写校验。
- **注意**：与 TIK-008 同文件 message_consumer.py，若并行注意合并。

### TIK-007 前端营业时间星期多选（BusinessHoursPanel）

- **批次**：A4 ｜ **依赖**：TIK-006 ｜ **预估**：0.5 人日
- **涉及文件**：`frontend/src/components/shop_settings/BusinessHoursPanel.vue`
- **改动要点**：加「生效星期」多选（周一~周日；默认全选=每天；空值/旧数据回显为全选）。
- **验收**：① 保存后 weekdays 字段正确提交（逗号串或空）；② 旧数据（null）回显兼容；③ `npm run build` 通过。

---

## 批次 B（依赖 A2；B3/B4 额外依赖 TIK-001）

### TIK-008 MessageConsumer parser 解耦 + 硬 import 惰性化

- **批次**：B1 ｜ **依赖**：TIK-002（按 PLAN 批次约束，逻辑上可提前） ｜ **预估**：1–1.5 人日
- **说明**：PLAN §4.3 关键设计。TikTok 店铺复用 MessageConsumer 全链路，只注入平台件；对 PDD 行为完全不变。
- **涉及文件**：
  - `websocket/engine/message_parser.py`（新）：`MessageParser = Callable[[Any], Optional[Context]]`；`pdd_parse_raw(raw_message)` 搬现 `_to_context` 的 PDD 逻辑。
  - `websocket/engine/message_consumer.py`：① 构造器加 `message_parser` 注入参数（默认 None→惰性取 pdd_parse_raw）；② `_to_context`(:402-419) 委托 parser；③ 顶层三处硬 import（`SendMessage` / `TransferService` / `PDDChatMessage`，见模块头 :43-46）移入惰性构造/默认实现函数内，类型注解改字符串。
- **角色归一化约定**：解析器输出 `Context.kwargs["from_user"]` 映射 `'user'`（买家）/`'mall_cs'`（本店客服），`consume_raw`(:380-397) 角色过滤零改动。
- **验收**：① 新增 `websocket/tests/test_message_parser.py`（注入 TikTok parser + StubSender 全链路，对齐既有 StubSender 注入模式；PDD 默认 parser 回归）；② websocket 存量 176 测试回归全绿。

### TIK-009 login/browser_launcher.py 提取

- **批次**：B4 ｜ **依赖**：TIK-001（s5 headless 结论影响启动参数） ｜ **预估**：0.5 人日
- **说明**：从 `playwright_login.py` 提取公共浏览器启动逻辑，行为不变。
- **涉及文件**：
  - `websocket/login/browser_launcher.py`（新）：`_clean_singleton_lock_files`(:118) / `_launch_persistent_context_with_retry`(:153) / `_CHROMIUM_ARGS`(:50) 迁入。
  - `websocket/login/playwright_login.py`：改为调用（行为不变）。
- **验收**：① playwright_login 相关既有测试回归；② browser_launcher 锁清理/启动重试单测（mock）。

### TIK-010 channel_base 协议 + connection_manager 工厂化

- **批次**：B2 ｜ **依赖**：TIK-002、TIK-008 ｜ **预估**：1 人日
- **说明**：PLAN §4.1/4.2。定义平台通道协议与工厂分派；TikTok 分支本工单先桩（真实现待 TIK-013），PDD 路径零改动。
- **涉及文件**：
  - `websocket/channel_base.py`（新）：`PLATFORM_PDD` / `PLATFORM_TIKTOK` / `ALLOWED_PLATFORMS` + `ChannelAdapter` Protocol（shop_id / user_id / `async start()` / `async stop()` / `get_connection_status()`）。不做大协议：注册表只消费 `stop()`，消息入站统一复用 `channel_pdd/message_queue.py` 的 FIFO 队列 + `message_handler(raw, shop_id, user_id)` 回调。
  - `websocket/channel_pdd/connection_manager.py`：`create_channel`(:59) / `start_channel`(:103) 加 `platform` 参数分派（'pdd'→现有路径；'tiktok'→TikTokChannel 桩）；`start_enabled_channels`(:144) 读出 `shop.platform` 传递。
- **验收**：① 新增 `websocket/tests/test_channel_factory.py`（create_channel 按 platform 分派；PDD 默认路径行为不变回归）；② websocket 存量测试回归全绿。

### TIK-011 channel_tiktok 基础件（browser_session / login / selectors）

- **批次**：B3 ｜ **依赖**：TIK-001、TIK-009 ｜ **预估**：1.5–2 人日
- **说明**：`channel_tiktok` 包基础设施；选择器以 TIK-001 的 `selectors.md` 实测清单填充。
- **涉及文件**（`websocket/channel_tiktok/`，全部 ≤500 行 + 中文 docstring）：
  - `__init__.py`：包声明 + `__all__`。
  - `selectors.py`：后台选择器常量清单（唯一 DOM 知识点；泰国站实测为中文界面），含 `LOGIN_PAGE_MARKERS`（登录页 URL `/account/login`）、`TIKTOK_SELLER_URL = "https://seller.tiktokshopglobalselling.com"`（泰国站固定基址，允许写死并注释）、`TIKTOK_CHAT_PATH = "/chat/inbox/current"`（URL 须带 `oec_seller_id`，详见 spike/README）。
  - `browser_session.py`：`BrowserSession`（每店独立 user-data-dir `tiktok_{shop_pk}`、headless / proxy_server 可选、`page` 属性、`close()`；构造器可注入 playwright 工厂，缺省走 async_playwright）。
  - `tiktok_login.py`：`login_tiktok(name, password)`（验证码人工等待 120s，对齐 `playwright_login._login_wait_timeout_ms` 模式；成功导出 Cookie JSON + 抓 shop_id/shop_name）、`refresh_tiktok_session(name)`。
- **验收**：① 新增 `websocket/tests/test_browser_session.py`（user-data-dir 按 shop_pk 隔离、代理参数传递、Singleton 锁清理，mock browser_launcher）；② 登录流程冒烟（需测试店；若账号未就绪标注「待 TIK-018 实测」不阻塞后续纯逻辑开发）。

### TIK-012 TikTok 消息解析/发送/会话守卫（message / sender / guard）

- **批次**：B3 ｜ **依赖**：TIK-001、TIK-011 ｜ **预估**：1.5–2 人日
- **涉及文件**（`websocket/channel_tiktok/`）：
  - `tiktok_message.py`：`TikTokChatMessage`（raw → `Context`，复用 `channel_pdd.pdd_message.Context/ContextType`）；角色归一化 buyer→'user' / seller→'mall_cs'；类型映射 text→TEXT / image→IMAGE / 其余→SYSTEM_STATUS（不进决策链）；非法 raw 容错。
  - `tiktok_sender.py`：`TikTokSender`（`send_text(recipient_uid, content) -> Optional[dict]` 同步签名，对齐 PDD SendMessage）；`run_coroutine_threadsafe(dom_send_coro, main_loop).result(timeout)` 桥接主循环（构造时注入主循环引用）；DOM 发送 + 成功检测（消息流出现己方气泡，超时 15s 视为失败）；发送串行化（主循环侧 asyncio.Lock，防同店并发 DOM 操作）；`send_image` Phase 1 显式不支持。
  - `session_guard.py`（纯逻辑，可属性测试）：`ReplyDebouncer(silence_seconds=45.0)`，`feed(msg, now)` / `pending_conversations()`——买家消息登记/重置 pending，卖家消息取消 pending，仅当会话最后一条来自买家且静默期满才返回应处理消息。
- **验收**：① `websocket/tests/test_tiktok_message.py`（raw→Context、角色归一化、类型映射、非法 raw 容错）；② `test_session_guard.py`（Hypothesis：仅最后一条为买家才回、静默聚合、卖家回复取消 pending）；③ `test_tiktok_sender.py`（FakePage 注入：点击/输入/发送调用序、human-like 分段、成功检测、线程桥接 mock）。

### TIK-013 TikTokChannel 主循环（监控 + 登录失效检测）

- **批次**：B3 ｜ **依赖**：TIK-005、TIK-011、TIK-012 ｜ **预估**：1.5–2 人日
- **涉及文件**：`websocket/channel_tiktok/tiktok_channel.py`
- **改动要点**（对齐 PLAN §5.1）：
  - 构造参数：`shop_id / user_id / shop_pk / message_queue / message_handler / status_manager=None / browser_session=None / event_notifier=None / poll_interval=5.0 / respect_business_hours=True`。
  - `start()`：respect_business_hours=True 时先查店铺营业时间（含 weekdays），窗口外直接置 DISCONNECTED 并 return（不启动浏览器、不登记注册表）；否则 BrowserSession.start() → 打开聊天页 → 创建 `_monitor_loop` 任务。
  - `stop()`：停循环任务 → BrowserSession.close() → 置 DISCONNECTED。
  - `_monitor_loop()`：周期抓会话列表快照 → 快照 diff（拆纯函数 `diff_conversations(old, new)`）→ 会话守卫判定 → 新买家消息入队；每轮检测登录失效（URL 跳登录页 / LOGIN_PAGE_MARKERS）→ 置状态 + 经 AlertDedup 触发 `login_expired` / `connection_disconnected` 告警 + 停循环。
- **验收**：① `websocket/tests/test_tiktok_channel.py`（生命周期 start/stop 任务清理；时间窗外 start 不启动浏览器；登录失效检测→告警；监控循环 diff 入队，mock browser_session）；② diff 纯函数属性测试；③ 告警事件走 AlertDedup 去重（复用 TIK-005 组件）。

---

## 批次 C（依赖 B 全部，可并行）

### TIK-014 TikTokChannel 装配 + routes/connections 联调

- **批次**：C1 ｜ **依赖**：TIK-010、TIK-013 ｜ **预估**：1 人日
- **说明**：把 TikTokChannel 真正接进 connection_manager 工厂（TIK-010 的桩替换为真实现）；consumer 注入 tiktok_message_parser + TikTokSender。
- **涉及文件**：
  - `websocket/channel_pdd/connection_manager.py`：tiktok 分支装配 TikTokChannel（message_queue、message_handler、event_notifier 注入）。
  - `websocket/routes/connections.py`：ConnectRequest 加 `platform` 字段，`connect_connection` 透传。
- **验收**：① 联调冒烟（经 backend connect 一个 TikTok 店铺 → 通道 start/stop/状态查询正确，连接事件不崩）；② PDD 路径回归全绿。

### TIK-015 scheduler tiktok_window 任务

- **批次**：C2 ｜ **依赖**：TIK-002、TIK-006、TIK-014 ｜ **预估**：1–1.5 人日
- **说明**：PLAN §5.3 时间窗控制。scheduler 经 HTTP 维持 TikTok 店铺连接与营业时间窗口一致。
- **涉及文件**：
  - `scheduler/tasks/constants.py`：加 `TASK_TIKTOK_WINDOW = "tiktok_window"`，并入 SUPPORTED_TASK_KEYS。
  - `scheduler/tasks/task_runners.py`：加 `run_tiktok_window()`（interval 60s）：读启用且 `platform='tiktok'` 的店铺及其 BusinessHours（含 weekdays）→ 每店计算期望态 `should_connect = is_within_business_hours(..., weekdays=...)`（北京时间，复用 `common/utils/weekdays.py`）→ 经 HTTP `status-batch` 查实际态 → 期望连接未连接调 `connect`（带 platform）、期望断开已连接调 `disconnect` → 幂等收敛，失败仅记 task_run_log；PDD 店铺不受影响（过滤 platform）。TASK_RUNNERS 注册。
  - `scheduler/tasks/service_client.py`：connect/disconnect/status 触发函数带 platform 透传（如缺补）。
- **验收**：① `scheduler/tests/test_tiktok_window.py`（期望态收敛逻辑 connect/disconnect 决策，mock service_client）；② scheduler 存量 17 测试回归全绿；③ 管理端建任务指引（scheduled_task 表手工建记录，无自动种子）。

### TIK-016 websocket routes login/messages/cookies 平台分派

- **批次**：C3 ｜ **依赖**：TIK-002、TIK-011、TIK-013 ｜ **预估**：1 人日
- **涉及文件**：
  - `websocket/routes/login.py`：PasswordLoginRequest 加 `platform`（默认 'pdd'），路由内分派 `pdd_login.login_pdd` / `channel_tiktok.tiktok_login.login_tiktok`。
  - `websocket/routes/messages.py`：~~`send_message` 按 platform 校验（查 Shop.platform），TikTok 返回 `error_response(-1, "TikTok 店铺暂不支持在线手动发送")`（Phase 1 可看不可发）~~（**Phase 2 已交付，2026-08-28**：TikTok 分支复用活跃通道 `TikTokSender` 手动发送——`get_connection(shop_id, owner_user_id)` 取通道 `sender`，`asyncio.to_thread` 执行 + `enforce_interval=False` 跳过最小随机间隔；无活跃连接返回「TikTok 店铺未连接，请先建立连接」）。
  - `websocket/routes/cookies.py`：RefreshCookieRequest 加 platform；TikTok 店铺返回成功并注明跳过（登录态常驻 user-data-dir，不依赖 cookies_enc 刷新）。
- **验收**：① 与 backend（TIK-003 透传链）联调：测试店 TikTok 登录可触发、~~手动发送被拦~~（Phase 2 已支持，2026-08-28）、刷新跳过；② PDD 路径回归全绿。

---

## 批次 D（收尾）

### TIK-017 TIKTOK_* 配置 + Dockerfile/资源调整

- **批次**：D2 ｜ **依赖**：TIK-001（s5 结论） ｜ **预估**：0.5–1 人日
- **涉及文件**：
  - `common/core/config.py` + `.env.example`：新增 `TIKTOK_SHOP_ENABLED`（bool 默认 false——灰度总开关，false 时 TikTok 店铺连接请求直接拒绝）、`TIKTOK_POLL_INTERVAL_SECONDS`（5.0）、`TIKTOK_DEBOUNCE_SECONDS`（45.0）、`TIKTOK_SEND_TIMEOUT_SECONDS`（15.0）、`TIKTOK_LOGIN_WAIT_TIMEOUT_MS`（120000）、`TIKTOK_MAX_BROWSER_INSTANCES`（4，超限拒绝连接并告警）。遵循规范 21。
  - `websocket/Dockerfile`：视 s5 结论加 xvfb/中文字体依赖；确认 4 实例资源（容器 memory limit 预留 4×1GB、`websocket_browser_data` 卷扩容）。
- **验收**：① 配置读取冒烟（默认值正确）；② 部署影响清单逐项落实（.env 模板、Dockerfile、卷、内存）。

### TIK-018 测试店端到端验收（周末窗口实测）

- **状态**：`[x]` 验收通过（2026-08-29 周末窗口实测完成，5/5 项全过，见下方实测记录）｜**验收工具已备（2026-08-28）**：`tools/tiktok_acceptance/`（对账 CLI `reconcile.py` + 首响统计纯函数 `latency.py` + 验收演练手册 `README.md`，含 5 项验收逐条步骤与告警演练清单；配套测试 `tools/tests/` 16 个全绿）。验收 5 项步骤详见该 README。
- **实测发现与修复（2026-08-29）**：
  1. **店铺 oec_seller_id 失效**：库中 `shop_id=18023103936` 已不是当前卖家后台真实 ID（实测当前为 `7494494994748966018`，经主页「客户消息」导航进入聊天页从 URL 取得），以旧 ID 直连聊天页会弹「Your login has expired」模态（与登录态无关）。已修正库值。
  2. **IM 会话过期弹窗检测盲区**：主站登录态有效时 IM 会话过期以 `.p-modal` 弹窗呈现，URL 不跳转，原 `page.url` 检测覆盖不到。已补 `_is_im_login_expired()`（`selectors.py` 新增 `IM_EXPIRED_MODAL_MARKERS`）+ 测试。
  3. **浏览器死亡无感知**：快照抓取为纯逻辑桩不触碰页面时，杀浏览器无任何告警。已补监控循环每轮 `_check_page_alive()` 最小存活探测（evaluate 失败 → `connection_disconnected`）+ 测试。实测杀浏览器后 **4 秒内**告警。
  4. **告警服务间链路缺失**：`build_alert_notifier` 的 `send_cb` 恒为 `None`（仅日志占位），告警到不了企微。已补：backend 新增内部接口 `POST /api/v1/internal/notify-events`（X-Internal-Token 鉴权，转交 `push_system_event`，`operator_id=None` 系统内部调用）；websocket 新增 `engine/alert_forwarder.py`（`backend_alert_send_cb`，fire-and-forget 线程池转发）并注入 PDD/TikTok/cookie 刷新三处调用点。实测杀浏览器后 `pdd_notify_record` 落库 success（企微 errcode 校验真实送达）。
  5. websocket 301 / backend 237 用例全绿（各含新增用例）。
- **验收结论（2026-08-29，窗口 20:00-21:15 实测）**：
  - 验收 1 ✅：真实买家消息（「售前」「转人工」）稳态首响 **69s / 81s**（< 300s）；
  - 验收 2 ✅：对账 CLI 正常出数，收发计数与会话明细齐全（`--json` 可留存附件）；
  - 验收 3 ✅：杀浏览器 4s 内 `connection_disconnected` 告警，企微实收（用户确认）；
  - 验收 4 ✅：静默窗口 5 分钟无重复，恢复重连后再次故障 6s 内可再告警；
  - 验收 5 ✅：窗外 scheduler 收敛断开 + 重启跳过建连（双道闸），窗内 1 周期内自动重连。
- **遗留（不阻塞关单）**：① 回声方向复核已修（297c746）并实测验证（发送后 3.5 分钟零新增决策）；② 「转人工」命中默认回复——测试店未配转人工关键词（`pdd_transfer_keyword` 空），属店铺配置项；③ 会话卡按 `:has-text` 用户名子串定位，同名前缀多买家精确路由留 Phase 2。
- **选择器回填与收发链路打通（2026-08-29 晚）**：
  1. 真实买家会话（ddy39s 发「测试」）实测回填 5 个选择器至 selectors.py：会话卡/用户名用平台官方 `data-testid`（`chat.chatroom.conversation_card[_username]`）、消息流 `.chatd-scrollView-content`、己方气泡 `.chatd-bubble--self`、输入框 `textarea[placeholder]`（主输入唯一带 placeholder 属性）、发送按钮 `.p-btn-primary:has-text("发送")`。
  2. `_capture_conversations` 真实实现：**未读角标驱动**（仅 `.p-badge` 会话入快照，msg_id=买家名+预览文本）——本店发送不产生未读，diff 天然规避自激循环。
  3. 发送链路实测修正 3 处：`:has(:text-is(...))` 嵌套文本伪类 Playwright 不支持（匹配 0）→ 改 `:has-text`；气泡计数须在**打开会话后**统计（消息流仅打开时渲染）且成功检测改 `:nth-match` 数量增量；`page.count` 不存在改 `locator().count()`。
  4. 频率断路器修正：45-120s 节流从桥接协程内移到 `send_text` 桥接前（同步 sleep）——原实现桥接等待超时仅 15s，节流必然超时误判失败。
  5. **端到端实测通过**：买家「测试」→ 快照 diff → 45s 去抖 → 默认回复决策 → DOM 发送成功（20:50:23 success=True），页面确认己方气泡出现且经平台自动翻译送达买家；决策→发出全程 128s（< 300s 验收阈值）。对账 CLI 正常出数（ddy39s 收 3/发 1，多收 2 条为调试期重启重复消费）。
- **批次**：D1 ｜ **依赖**：TIK-014 ~ TIK-017 全部 ｜ **预估**：1 人日 + 周末窗口实测
- **说明**：Phase 1 出口验收，对齐 PLAN §10「关键验收」与 §1 出口标准。
- **需要用户配合**：测试店账号、企微群（收告警）。
- **验收标准（全部满足才可关闭）**：
  1. 测试店周末/非工作时间无人值守期间，买家新消息 5 分钟内收到模板首响（仅合规引导话术）；
  2. 消息日志与平台后台一致（可对账）；
  3. 人为杀掉浏览器/清除登录态 → 企微立即收到对应告警（connection_disconnected / login_expired）；
  4. 告警静默窗口（默认 30 分钟）内同一事件不重复轰炸；恢复（重连/重登）后可再次告警；
  5. 营业时间窗口外连接不建立（scheduler 期望态 + TikTokChannel 第二道闸双验证）。

---

## 附：测试文件与工单映射（PLAN §6.1）

| 测试文件 | 归属工单 |
| --- | --- |
| `websocket/tests/test_tiktok_message.py` | TIK-012 |
| `websocket/tests/test_session_guard.py` | TIK-012 |
| `websocket/tests/test_tiktok_sender.py` | TIK-012 |
| `websocket/tests/test_browser_session.py` | TIK-011 |
| `websocket/tests/test_tiktok_channel.py` | TIK-013 |
| `websocket/tests/test_tiktok_connect.py` | TIK-014 |
| `websocket/tests/test_channel_factory.py` | TIK-010 |
| `websocket/tests/test_message_parser.py` | TIK-008 |
| `websocket/tests/test_alert_dedup.py` | TIK-005 |
| `websocket/tests/test_business_hours_weekdays.py` | TIK-006 |
| `backend/tests/test_shop_platform.py` | TIK-003 |
| `scheduler/tests/test_tiktok_window.py` | TIK-015 |

## 附：开工前核对清单（PLAN §0 关键事实，改动涉及处必读）

- MessageConsumer 位于 `websocket/engine/message_consumer.py`（1520 行，存量超 500 行规范；新增文件仍须 ≤500 行）。
- PDDChannel 不用 `ConnectionStateMachine`；重连达上限分支在 `pdd_channel.py:296-305`，现仅写 risk_log、无通知。
- `common/core/config.py:135-136` 的 `proxy_enabled/proxy_api_url` 是 AI 代理 API 地址（需求 21.14/21.15），**勿复用**作店铺浏览器代理。
- `routes/messages.py` 与 `GetChatHistory/GetConversations` 硬 import PDD API；backend 会话/历史查询走数据库（平台无关），TikTok 在线聊天 Phase 1 拦截即可。
- scheduler 无任务自动种子，`scheduled_task` 表由管理端手工配置。
- 营业时间前端面板：`frontend/src/components/shop_settings/BusinessHoursPanel.vue`。
