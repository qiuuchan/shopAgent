# TikTok 二开工单池（TICKETS_TIKTOK）

> 配套设计文档：[PLAN_TIKTOK.md](./PLAN_TIKTOK.md)（含全部行号/接缝核对，工单不重复展开，细节以该文为准）。
> 工单编号前缀 `TIK-`；批次 A–D 严格对齐 PLAN_TIKTOK.md 第 10 节「实施顺序」（Phase 1，已全部关单）；批次 E/F 对齐 PLAN §7/§8（Phase 2 收尾 + Phase 3，2026-08-29 立项）。
> 状态标记：`[ ]` 待办 / `[x]` 完成 / `[!]` 搁置或取消（原因见各单）。
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
| TIK-018 | 测试店端到端验收（周末窗口实测） | D1 | TIK-014 ~ TIK-017 全部 | 1 + 周末实测 | [x] 验收通过关单（2026-08-29，5/5 项全过） |
| TIK-019 | 多店并发实测（2~4 店资源与稳定性） | E1 | 外部：≥2 个 TikTok 测试店账号 | 1–2 + 观察窗口 | [!] 搁置（2026-08-29：口径调整，仅监督 1 店） |
| TIK-020 | 频率断路器配置化（RiskRule 限流规则） | E1 | — | 0.5 | [x] 已交付（2026-08-29 实测通过） |
| TIK-021 | 对账日常化（reconcile 固化为周期动作） | E1 | — | 0.5–1 | [ ] |
| TIK-022 | 部署清单落实（.env/内存/卷/灰度开关核对） | E1 | TIK-017 | 0.5–1 | [ ] |
| TIK-023 | 登录态过期周期观测 + cookie_refresh 周期配置 | E2 | — | 0.5 + 观察窗口 | [~] 工具侧交付（2026-08-29），观测窗口与人工演练待执行 |
| TIK-024 | 首家真实店铺灰度接入（含转人工关键词配置） | F1 | 批次 E 其余（TIK-019 搁置）；外部：真实店铺 + 生产企微群 | 0.5 + 1–2 周观察 | [ ] |
| TIK-025 | 回复率统计（首响分布/超时占比 + dashboard 平台维度） | F1 | —（可先行开发） | 1.5–2 | [x] 已交付（2026-08-29 真实库演练通过） |
| TIK-026 | 回复率跌破 85% 企微告警 | F2 | TIK-025 | 1 | [ ] |
| TIK-027 | 扩量至 4 家真实店铺（原 Phase 3 出口） | F3 | TIK-024 稳定、TIK-025/026 | 0.5 + 稳定期 | [!] 已取消（2026-08-29：口径调整，仅监督 1 店，不扩量） |

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
- **遗留（不阻塞关单）**：① 回声方向复核已修（297c746）并实测验证（发送后 3.5 分钟零新增决策）；② 「转人工」命中默认回复——测试店未配转人工关键词（`pdd_transfer_keyword` 空），属店铺配置项；③ ~~会话卡按 `:has-text` 用户名子串定位，同名前缀多买家精确路由留 Phase 2~~ **已处理（Phase 2，2026-08-29）**：新增 `conversation_nav.py` 会话卡精确导航——JS 收集全量卡（含 index/unread）+ Python 严格相等匹配 + `:nth-match` 精确点击，捕获（`_capture_conversations`）与发送（`TikTokSender`）两侧统一复用；同名前缀（如 ddy39s / ddy39s2）不再互相误配，未命中 / 同名歧义时发送侧不发送（宁失败不误发）、捕获侧跳过该卡；配套测试 18 个（含属性测试与假页面精确点击回归）。
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

## 批次 E（Phase 2 收尾——上线前加固，2026-08-29 立项）

> 对齐 PLAN §7。Phase 2 的前置项已随 Phase 1 顺手交付（店铺级代理字段、发送最小随机间隔、在线手动发送、登录保活 login_recovery、message_queue 上移 common、同名前缀精确路由 conversation_nav，见各工单 Phase 2 备注），本批次只剩**落地验证与配置类**工作，无新架构。
>
> **2026-08-29 口径调整：仅监督 1 家店铺后台**——TIK-019（多店并发实测）因此搁置；若后续业务恢复多店运营再启用。

### TIK-019 多店并发实测（2~4 店资源与稳定性）

- **状态**：`[!]` 搁置（2026-08-29 口径调整——仅监督 1 家店铺后台，不做多店并发；若后续恢复多店运营再启用）
- **批次**：E1 ｜ **依赖**：外部（≥2 个 TikTok 测试店账号） ｜ **预估**：1–2 人日 + 24h 观察窗口
- **说明**：单店链路已由 TIK-018 实测通过，多店并发从未验证。本单验证：① user-data-dir（`tiktok_{shop_pk}`）多店实际不串扰；② `TIKTOK_MAX_BROWSER_INSTANCES`（默认 4）超限拒绝连接并告警生效；③ 资源水位（s5 实测 headless 单实例 ≈75MB，据此核对容器 memory limit 预留）；④ 多店快照轮询并发下的 CPU/事件循环压力。测试店不足 4 个时先做 2 店，全量 4 店并发验证并入 TIK-027（已取消，随单搁置）。
- **涉及文件**：无新代码预期；`.env` / docker-compose 资源参数按实测微调。
- **验收**：① ≥2 店同时连接 24h 稳定（无串扰、无告警误报漏报）；② 实例上限生效（超限店被拒 + 告警实收）；③ 内存峰值记录在案并据此定容器限额。

### TIK-020 频率断路器配置化（RiskRule 限流规则）

- **状态**：`[x]` 已交付并关单（2026-08-29 真实库演练通过，验收 ①/② 全过）
- **批次**：E1 ｜ **依赖**：— ｜ **预估**：0.5 人日
- **说明**：PLAN §7。engine 风控组件（RiskRule 的 shop_reply_limit/window_seconds，会话/店铺维度限流）纯逻辑现成、零代码改造。本单给 TikTok 测试店配置「每小时回复上限」规则并实测命中后行为（暂停自动回复 + 风控日志可查），固化管理端/SQL 配置指引。
- **涉及文件**：无产品代码改动；新增配置指引 `RISK_RULE_GUIDE.md`、演练 CLI `tools/tiktok_acceptance/risk_rule_drill.py`、风控纯逻辑单测 `websocket/tests/test_risk_control.py`。
- **验收**：① 测试店配规则后，超限消息不再自动回复且 risk_log 可查；② 未配规则店铺行为不变。
- **交付与实测记录（2026-08-29）**：
  1. **配置固化**：TikTok 测试店（`shop_pk=1`，店铺名 `18023103936`）写入规则 `shop_reply_limit=20 / window_seconds=3600 / enabled=true`（单会话维度不限制）。表 `pdd_risk_rule` 此前为空——本项目从未配置过风控规则。
  2. **演练 CLI（新增）**：`tools/tiktok_acceptance/risk_rule_drill.py`。真实度与隔离边界：风控规则读写、店铺运行时加载（营业时间/默认回复/关键词）、决策链判定、**风控日志落库全部走真实库与真实代码**；发送器 / 消息日志 / 聊天记录 / 实时推送用内存桩替换，演练不向买家发消息、不污染 `pdd_message_log` / `pdd_chat_message` 对账数据。
  3. **验收 ① 实测**（演练上限临时取 3，窗口 3600s，每阶段 5 条）：前 3 条正常走默认回复，第 4、5 条被风控暂停（`action=risk_blocked`、未调用发送器），并真实落库 2 条 `pdd_risk_log`（`risk_type=frequency_limit`，原因「单店铺3600秒统计窗口内回复次数已达上限（3/3），暂停自动回复」）→ 符合预期。
  4. **验收 ② 实测**：规则不存在（`pdd_risk_rule` 无该店记录 → 运行时 `risk_enabled=False`）时 5 条消息全部照常回复、零风控日志；等价于 `enabled=false`（表内已有规则时按此验证）→ 行为不变。
  5. **单测补齐**：新增 `websocket/tests/test_risk_control.py`（17 例）——窗口边界（恰好落在窗口起点不计入、未来时刻不计入、窗口未配置不截断）、达上限语义（次数 ≥ 上限、上限 0 全禁）、先会话后店铺优先级、风控未启用/未配上限恒放行、窗口滑出后恢复、风控日志字段与北京时间 ISO 串。
  6. **回归**：websocket 352（新增 17 例，基线 335 → 352）/ backend 237 / common 35 / scheduler 37 / tools 34 全绿（scheduler、tools 两项计数含工作区其它在制品测试，非本单引入）。
- **遗留（不阻塞关单）**：① 回复计数为进程内存态，websocket 重启后清零（重启可绕过窗口上限，属已知取舍，已入指引注意事项）；② 真实买家消息端复核未做（本次按用户选择以「真实链路 + 真实库驱动」验证，TIK-024 灰度首店时可顺带复核一次）。

### TIK-021 对账日常化（reconcile 固化为周期动作）

- **批次**：E1 ｜ **依赖**：— ｜ **预估**：0.5–1 人日
- **说明**：PLAN §7「对账」。`tools/tiktok_acceptance/reconcile.py` 已备（TIK-018 验收工具）。本单把一次性验收工具固化为日常运维动作，两档方案：最小方案=文档化人工抽查流程（频率/步骤/留存约定，先行交付）；自动化方案=scheduler 每日任务调 CLI 出报表（企微日报或日志留存，随 Phase 3 数据量上来再上）。
- **涉及文件**：`tools/tiktok_acceptance/README.md`（运维流程章节）；自动化方案才涉及 scheduler 任务。
- **验收**：① 对账动作有固化入口（文档或任务）；② 一次真实对账演练产出留存。

### TIK-022 部署清单落实（.env/内存/卷/灰度开关核对）

- **批次**：E1 ｜ **依赖**：TIK-017 ｜ **预估**：0.5–1 人日
- **说明**：TIK-017 部署影响清单逐项核对落实（2026-08-29 口径调整为单店监督，资源按 1 店预留）：① `.env.example` TIKTOK_* 变量齐全（SHOP_ENABLED/POLL_INTERVAL/DEBOUNCE/SEND_TIMEOUT/LOGIN_WAIT_TIMEOUT/MAX_BROWSER_INSTANCES）；② websocket 容器 memory limit 预留 1×1GB（原 4×1GB 为 4 店口径，已随单店监督收窄）；③ `websocket_browser_data` 卷按单店 user-data-dir 预留；④ `TIKTOK_SHOP_ENABLED` 默认 false 语义复核（false 时 TikTok 店铺连接请求直接拒绝）。
- **涉及文件**：`.env.example`、docker-compose 配置。
- **验收**：① 部署配置逐项核对通过；② 空配置启动冒烟（默认值读取正确）；③ 灰度开关 false/true 行为各验证一次。

### TIK-023 登录态过期周期观测 + cookie_refresh 周期配置

- **批次**：E2 ｜ **依赖**：— ｜ **预估**：0.5 人日 + 数日~数周观察
- **说明**：TIK-018 遗留观测项。TikTok 登录态常驻 user-data-dir，实际过期周期未知。观测测试店 `login_expired` 出现频率得出周期结论，据此配置 scheduler cookie_refresh 任务周期（TikTok 侧该任务跳过 PDD 刷新、仅作巡检触发点，见 TIK-016 备注）；同步确认 login_recovery（默认 60s×10 次探测）在真实过期场景的接管表现。
- **涉及文件**：scheduler 任务周期配置；无代码改动预期。
- **验收**：① 观测记录（过期周期结论入档）；② cookie_refresh 周期按结论配置；③ 一次真实 login_expired → 人工重登 → 自动恢复演练。

> **2026-08-29 进展：工具侧交付（三项验收均待真实环境，故未关单）**
>
> 原方案「无代码改动」不成立：TikTok 侧 cookie_refresh 原本纯短路（`cookies.py` 直接 `skipped: True`），
> `pdd_task_run_log` 恒 success，**观测不到任何过期**；且登录态不入库、无「登录生效时刻」字段，
> 仅有告警记录时只有「已过期」时间点，推不出「登录 → 过期」时长。故补上主动巡检打点：
>
> 1. 新增 `websocket/channel_tiktok/login_probe.py`（纯逻辑可注入）+ `TikTokChannel.probe_login_state()`：
>    只读探测登录态（无页面 / 页面死亡 / 主站过期 / IM 过期 / 正常），**只判定不处置**
>    （不置状态、不发告警、不重开页面，避免巡检干扰主链路）；
> 2. `cookies.py` TikTok 分支改为巡检触发点，结果随 `data.login_probe` 回传，恒 success、
>    不改变任务成败语义；
> 3. `scheduler` 把巡检异常明细追加进 `task_run_log.message`，串起「何时仍正常 / 何时已失效」时间线；
> 4. 新增 `tools/tiktok_acceptance/login_expiry.py` 观测 CLI：统计 `login_expired` 事件间隔 →
>    推算建议巡检周期（`最短间隔 / 4`，夹 [600s, 6h]）+ 核对巡检覆盖率与巡检异常打点条数。
>
> **周期决策：维持 600 秒不动。** `cookie_refresh` 是 PDD / TikTok 共用任务，调大周期会同步降低
> PDD 侧 Cookie 保活频率，违反「对 PDD 零行为变更」；且 10 分钟巡检对「天」量级的过期周期本就
> 绰绰有余。调参决策规则（含 PDD 耦合约束与「什么情况下才该调」）见
> `tools/tiktok_acceptance/README.md` §6.4。
>
> **待执行**：① 观测期每 3~7 天跑一次观测 CLI 并 `--json` 归档（≥3 次过期事件才出周期结论，
> 且须复核巡检覆盖率 ≥80%）；③ 按 README §6.5 演练清单做一次真实 `login_expired` → 人工重登 →
> 自动恢复演练（验证 login_recovery 60s×10 次探测的接管表现）并回填归档模板。两项完成后本单关单。

---

## 批次 F（Phase 3——真实店铺灰度 + 回复率监控，2026-08-29 立项）

> 对齐 PLAN §8。**Phase 3 出口标准（2026-08-29 口径调整）：1 家真实店铺稳定运行，24h 回复率 ≥85%。**
> 原「扩量至 4 店」的出口（TIK-027）已随口径调整取消，TIK-019 多店并发实测同步搁置。

### TIK-024 首家真实店铺灰度接入（含转人工关键词配置）

- **批次**：F1 ｜ **依赖**：批次 E 其余（TIK-019 已搁置）；外部（真实店铺账号、生产企微群） ｜ **预估**：0.5 人日配置 + 1–2 周观察
- **说明**：PLAN §8。灰度先 1 家：`TIKTOK_SHOP_ENABLED=true` + 单店启用。同步补 TIK-018 遗留项——生产店必须配置转人工关键词（测试店 `pdd_transfer_keyword` 为空导致「转人工」命中默认回复）。观察期内人工对账（TIK-021 流程）+ 告警演练各一次。
- **涉及文件**：无代码改动预期；店铺配置（启用状态/营业时间/转人工关键词/风控规则）。
- **验收**：① 稳定运行 2 周（无漏回、告警及时、对账一致）；② 转人工关键词命中验证（不再落默认回复）；③ 观察期对账与告警演练记录留存。

### TIK-025 回复率统计（首响分布/超时占比 + dashboard 平台维度）

- **状态**：`[x]` 已交付并关单（2026-08-29 真实库演练通过，验收 ①②③ 全过）
- **批次**：F1 ｜ **依赖**：—（可先行开发，真实数据随 TIK-024 积累） ｜ **预估**：1.5–2 人日
- **说明**：PLAN §8。基于 chat_message 统计「首响时长分布、超 5 分钟占比、回复率」，支持平台 / 店铺 / 日期三个维度；backend 新接口 + 数据分析页新增首响统计区块。首响口径与 TIK-018 对账工具共用同一份纯函数（`tools/tiktok_acceptance/latency.py` 上移 `common/utils/latency.py`）。
- **涉及文件**：新增 `common/utils/latency.py`（原 tools 版上移 + 分布/回复率）、`backend/app/services/first_response_service.py`、`backend/app/api/routes/dashboard.py` 新增 `GET /dashboard/first-response`、`backend/tests/test_first_response_api.py`；前端 `frontend/src/pages/data_analysis.vue`、`frontend/src/api/dashboard_api.js`、新增 `frontend/src/config/platforms.js`；验收演练 `tools/tiktok_acceptance/first_response_drill.py` + `tools/tests/test_first_response_drill.py`。
- **验收**：① dashboard 可按平台/店铺查看首响分布与超 5 分钟占比；② 与 reconcile.py 抽查口径一致；③ backend/frontend 存量测试回归全绿。
- **交付与实测记录（2026-08-29）**：
  1. **口径上移 common**：`tools/tiktok_acceptance/latency.py` → `common/utils/latency.py`（backend 不应依赖 tools 包），新增 `latency_distribution()`（默认 5 桶：30 秒内 / 30–60 秒 / 1–3 分钟 / 3–5 分钟 / 超 5 分钟，左闭右开、末桶无上界）与 `reply_rate()`；对账工具 `reconcile.py` 改为从 common 导入，两份统计从此共用同一实现。测试文件随迁至 `common/tests/test_latency.py`。
  2. **回复率口径（已与用户确认）**：`回复率 =（已回复 − 超时）/（已回复 + 待回复）`——**窗口结束仍待回复的周期计未达标**（计入分母），对齐 Phase 3 出口「24h 回复率 ≥85%」，TIK-026 告警直接复用 `reply_rate()`，保证「看板看到的」与「告警判的」是同一个数。无任何周期时返回 `None`（无数据，不判 0 也不判 1）。
  3. **接口**：`GET /api/v1/dashboard/first-response`（`dashboard` 资源 view 权限 + 数据范围隔离），参数 `start_date / end_date / platform / shop_pk`；返回 `{summary, distribution, shops}`——summary 含均值/P50/P90/最长/超时占比/回复率，shops 为分店铺明细（带 shop_name 与 platform）。日期解析与可见店铺范围复用 `dashboard_service` 的既有函数（该三处私有函数改为公开命名供复用）；时间范围上限 92 天（消息按行读入内存聚合）。
  4. **前端（已与用户确认落在既有「数据分析」页）**：`data_analysis.vue` 在趋势图下方新增首响统计区块——平台 / 店铺下拉（与日期共用一套筛选，一次查询刷新两块数据）、6 张指标卡（回复率 / 已回复周期 / 待回复周期 / 超 5 分钟占比 / P50 / P90）、内联 SVG 分布柱状图（末桶高亮）、分店铺明细表。平台枚举抽到 `frontend/src/config/platforms.js`，店铺管理页改为复用同一份。
  5. **验收 ①② 实测（真实库驱动）**：新增 `tools/tiktok_acceptance/first_response_drill.py`——**只读**（仅查 `pdd_chat_message` / `pdd_shop` / `sys_user` / `sys_role`，不写任何表）、**无对外副作用**（不拉浏览器、不发消息、不发企微），统计走 backend 真实服务函数（与线上接口同一实现），再与 `reconcile.py` 同窗口同店铺算一遍做 8 项口径抽查。实测（TikTok 测试店 `shop_pk=1`／`18023103936`）：
     - 7 天窗口 2026-08-23~08-29：已回复 115 个周期、待回复 0、超时 1（0.9%）、**回复率 99.1%**、平均 8.52s、P50 0s、P90 4s、最长 760s；分布 `30 秒内 112 / 30–60 秒 0 / 1–3 分钟 2 / 3–5 分钟 0 / 超 5 分钟 1`；
     - 单日窗口 + `platform=tiktok`：已回复 80、超时 1（1.2%）、回复率 98.8%、平均 11.90s；
     - 平台错配（店铺为 tiktok、按 pdd 查）→ 拒绝并返回「店铺不存在或无访问权限」；
     - **8 项口径抽查（responded / pending / pending_conversations / over_threshold_count / over_threshold_ratio / mean / p50 / p90）backend 与 reconcile 全部一致，退出码 0**。
  6. **单测**：新增 `backend/tests/test_first_response_api.py` 14 例（平台/店铺筛选、分布桶左闭右开、末桶=超时计数、回复率含待回复、空窗口返回 None、数据范围隔离、非法平台/日期/超长范围/越权店铺拒绝、HTTP 层无权限拒绝）；`common/tests/test_latency.py` 补 9 例（分布与回复率）；`tools/tests/test_first_response_drill.py` 6 例（演练一致性、分布与店铺行、文本渲染、平台错配 / 无管理员 / 店铺不存在三个失败路径）。
  7. **回归**：backend 251（基线 237 → 251）、websocket 352、scheduler 37、common 55（基线 35 → 55，含随模块迁入的 20 例 latency 单测）、tools 29（迁出 11 例 + 新增 6 例）；前端 `npm run build` 通过（`data_analysis` chunk 10.82 kB）。
- **遗留（不阻塞关单）**：① 统计按行读入窗口内消息后内存聚合，单店 24h 无压力，若后期单窗口消息量到十万级需改为 SQL 侧预聚合；② dashboard 页面未在浏览器中人工目视验收（本次按用户偏好以「真实链路 + 真实库驱动」验证统计侧，HTTP 接口层由 TestClient 覆盖）。

### TIK-026 回复率跌破 85% 企微告警

- **批次**：F2 ｜ **依赖**：TIK-025 ｜ **预估**：1 人日
- **说明**：PLAN §8。新增事件类型（如 `reply_rate_below_threshold`）：scheduler 周期检查（每小时）各 TikTok 店铺 24h 回复率，跌破 85% 经现有 notify 链路（backend `POST /api/v1/internal/notify-events` + `alert_forwarder`）企微告警；AlertDedup 静默窗口去重、恢复后 resolve 可再告警（复用 TIK-005 组件语义）。
- **涉及文件**：scheduler 任务（新）；告警走现有链路，零新增服务。
- **验收**：① 模拟低回复率触发告警、企微实收；② 静默窗口去重 + 恢复后再告警；③ 正常水位无误报。

### TIK-027 扩量至 4 家真实店铺（原 Phase 3 出口）

- **状态**：`[!]` 已取消（2026-08-29 口径调整——仅监督 1 家店铺后台，不扩量；若后续业务恢复多店运营再恢复本单）
- **批次**：F3 ｜ **依赖**：TIK-024 稳定运行、TIK-025/026 ｜ **预估**：0.5 人日 + 稳定期
- **说明**：原 PLAN §8 Phase 3 出口为灰度 1→4 家，每店过配置清单（营业时间/转人工关键词/风控规则/代理）。取消后无验收项。

### 远期候选（不设工单号，方向储备）

- **选择器失配监控**：DOM 变化检测 →「页面结构变化」告警（风险清单 #1；selectors.py 已集中管理，缺监控触发点）。
- **ChannelAdapter 复用第三平台**：协议与工厂（TIK-010）已平台无关，新平台按 channel_* 包模式扩展。
- **发送可靠性增强**：DOM 发送失败重试与死信记录（当前宁失败不重试，失败留日志）。

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
