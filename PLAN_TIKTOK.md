# TikTok Shop 多店客服自动应答系统 二开实施计划

> 基于对 pdd-auto-reply 仓库的只读探索（2026-08-26），所有文件/行号均经实际核对。
> 目标：周末/非工作时间无人值守，买家新消息 5 分钟内自动首响（仅合规引导话术），4 家泰国站店铺（seller.tiktokshopglobalselling.com，中文后台）24h 回复率 ≥85%。技术路线：Playwright RPA。
>
> 已确认的架构决策：① 平台抽象、双通道并存（channel_pdd 保留为参考实现）；② 掉线/登录失效告警走企业微信群机器人；③ 每店独立 user-data-dir，代理 IP 预留配置默认关闭；④ 泰国站单区域（2026-08-27 实测修正：生产目标为泰国站，原「美国站单区域」假设作废）。

---

## 0. 探索结论与关键事实修正（写代码前必读）

1. **MessageConsumer 实际位于 `websocket/engine/message_consumer.py`**（非 channel_pdd/ 下），共 1520 行（本身已超 500 行规范，属存量现状；新增文件仍须遵守 ≤500 行）。
2. **PDDChannel 并不使用 `ConnectionStateMachine`**。`channel_pdd/core/connection_state_machine.py` 是独立组件（仅测试在用）；PDDChannel 自带 `_connect_with_retry` 重连循环（pdd_channel.py:259-315），达上限后仅置 ERROR 状态、经状态机 `_to_error` 写一条 risk_log，**无通知渠道推送**。告警补全点应在 PDDChannel 的重连循环终局分支。
3. **`common/core/config.py` 已有 `proxy_enabled`/`proxy_api_url` 字段（:135-136）**，但语义是「AI 代理 API 地址」（需求 21.14/21.15），与 TikTok 店铺级浏览器代理无关。店铺代理须新增店铺级字段，勿复用这两个配置。
4. **`routes/messages.py`（在线聊天手动发消息）与 `GetChatHistory/GetConversations` 硬 import PDD API**。但 backend 的会话/历史查询走数据库（conversation/chat_message 表，平台无关），因此 TikTok 店铺在线聊天「可看不可发」，Phase 1 在 backend 侧拦截即可。
5. scheduler 无任务自动种子：`scheduled_task` 表由管理端页面手工配置；新增任务键需改 constants + task_runners 后经管理端建任务。
6. 前端营业时间面板：`frontend/src/components/shop_settings/BusinessHoursPanel.vue`。
7. **站点修正（2026-08-27 spike 实测，覆盖原美区假设）**：生产目标为**泰国站**，后台域名 `seller.tiktokshopglobalselling.com`（非 seller-us.tiktok.com），卖家后台为中文界面（lng=zh-CN）。聊天页路径 `/chat/inbox/current`，URL 必须带 `oec_seller_id` 参数（缺失时可能跳回 homepage，行为不稳定）。聊天入口为侧边栏「客户消息」导航（`div.ub-navItem-b6df03`），点击**新开标签页**进入聊天。登录失效标记：URL 命中 `/account/login`（title「TikTok Shop Seller Log In | Cross Border」）。headless 实测可用（无风控/验证码，单实例 ≈75MB），无需 Xvfb。收消息协议未定论（静置无 WS，待真实买家消息补测）。详细实测见 `spike/tiktok/README.md`。

其他关键接缝（均已核实）：
- 出站回复不走长连接：`MessageConsumer._send_reply`（message_consumer.py:800-840）经 `asyncio.to_thread` 调 `sender.send_text/send_image`（同步签名）。
- MessageConsumer 构造器（message_consumer.py:277-297）已支持注入 sender/transfer_service/runtime_loader/notifier 等；但模块头 :43-46 硬 import `SendMessage`/`PDDChatMessage`/`TransferService`。
- 入站窄接口：FIFO 队列（channel_pdd/message_queue.py，纯 asyncio 平台无关）+ `message_handler(raw, shop_id, user_id)` 回调。
- `consume_raw` 角色过滤按 `from_user`（'user'=买家进决策链 / 'mall_cs'=客服转发展示）；消息类型须属 `_ELIGIBLE_TYPES`。
- `build_notifier`（message_consumer.py:1481-1509）POST `/api/v1/notify/events` 携带 shop_pk；backend `push_system_event` 按店铺推已启用渠道（wecom 已实现，email 未接 SMTP 勿依赖）。
- **掉线告警缺口：`connection_disconnected`/`login_expired` 事件全仓库无触发点**——本工程上线前置条件，需补建。

---

## 1. 总体分阶段框架

| 阶段 | 内容 | 出口标准 |
| --- | --- | --- |
| Phase 0 | spike 探查（独立脚本目录，不改主代码） | spike 报告 + 选择器清单 + 收消息方案决策 |
| Phase 1 | 单测试店 MVP 闭环（登录→监听→规则回复→日志→告警→时间窗） | 测试店周末无人值守 5 分钟首响，消息日志可对账 |
| Phase 2 | 安全加固 + 多店（频率断路器、代理、4 店并发、对账） | 4 店稳定运行 2 周 |
| Phase 3 | 扩真实店铺 + 回复率监控 | 24h 回复率 ≥85% |

---

## 2. Phase 0：spike 探查（独立脚本，不改主代码）

### 2.1 脚本清单（全部放在新目录 `spike/tiktok/`，不进服务代码、不打包）

| 脚本 | 验证内容 | 通过标准 |
| --- | --- | --- |
| `s1_login.py` | Playwright 登录泰国站 seller.tiktokshopglobalselling.com（持久化 user-data-dir、验证码人工介入） | 登录成功、user-data-dir 持久化后二次免登、导出 Cookie JSON |
| `s2_dom_map.py` | 聊天页 DOM 测绘：会话列表、未读标记、消息流、输入框、发送按钮的稳定选择器 | 产出选择器清单（写入 `spike/tiktok/selectors.md`），每个选择器在 3 次刷新后仍命中 |
| `s3_network_listen.py` | CDP 被动监听（`page.on("websocket")` / `page.on("response")`）收消息协议 | 判定收消息走什么协议、报文能否解析出 (conversation_id, sender_role, content)；**只听不重放** |
| `s4_send_message.py` | DOM 发送全链路：定位会话→输入→发送→成功检测（消息流出现己方气泡） | 连发 10 条全部成功且可检测；测量单条端到端耗时 |
| `s5_headless_detect.py` | headless 可检测性（headless 登录/操作是否触发风控页/验证码）；4 并发实例资源测量 | 给出 headless vs Xvfb+headed 结论；记录单实例 CPU/内存 |

### 2.2 需要用户配合的点
- 提供 1 个 TikTok 泰国站测试店账号（账号/密码），并配合人工过验证码（滑块/邮箱验证码）。
- 在测试店后台确认消息收发、验证 spike 发出的消息确实到达买家侧。
- 提供企微群机器人 Webhook URL（用于后续告警联调）。

### 2.3 决策准则（spike 结论如何落到 Phase 1）
- **收消息方案**：若 s3 证明 WebSocket/XHR 报文稳定可解析 → CDP 被动监听（实时性好）；否则 DOM 轮询会话列表（3-5s 间隔，足够 5 分钟首响）。二者只影响 `tiktok_channel.py` 监控循环的实现，其余模块不变。
- **发送一律走 DOM 自动化**（不重放网络请求，降低风控特征）——已定为既定决策。
- **headless**：若 s5 显示 headless 被检测/无法登录 → 容器内 Xvfb + headed（websocket 镜像加 xvfb 依赖）；否则 headless。
- **选择器**：全部集中在 `channel_tiktok/selectors.py`，DOM 变化只改一处。

---

## 3. Phase 1：文件级改动总览

### 3.1 新增文件

```
websocket/
├── channel_tiktok/                    # TikTok 通道包（全部 ≤500 行、中文 docstring）
│   ├── __init__.py                    # 包声明 + __all__
│   ├── tiktok_channel.py              # TikTokChannel：生命周期 + 监控循环 + 登录失效检测
│   ├── browser_session.py             # BrowserSession：每店独立持久化浏览器 + 可选代理
│   ├── tiktok_message.py              # TikTokChatMessage：raw → Context（角色归一化）
│   ├── tiktok_login.py                # 登录编排：login_tiktok / refresh_tiktok_session
│   ├── tiktok_sender.py               # TikTokSender：DOM 发送 + 成功检测 + human-like 输入
│   ├── selectors.py                   # 后台选择器常量清单（唯一 DOM 知识点；泰国站实测为中文界面）
│   └── session_guard.py               # ReplyDebouncer：未回复识别 + 静默聚合（纯逻辑）
├── login/
│   └── browser_launcher.py            # 公共浏览器启动：锁清理 + 启动重试（从 playwright_login 提取）
├── channel_base.py                    # ChannelAdapter 协议 + PLATFORM_PDD/PLATFORM_TIKTOK 常量
└── engine/
    ├── message_parser.py              # MessageParser 协议（Callable + 角色归一化约定）+ PDD 默认实现
    └── alert_dedup.py                 # AlertDedup：告警去重/静默窗口（平台无关）
spike/tiktok/                          # Phase 0 脚本目录（见上节）
```

新增测试文件见第 6 节。

### 3.2 修改文件清单（精确到位置）

| 文件 | 改动 |
| --- | --- |
| `common/models/shop_models.py` | Shop 加 `platform` 列（`String(32), default='pdd', nullable=False, comment="所属平台：pdd/tiktok"`）。SchemaMigrator 启动幂等补列，存量店铺自动为 'pdd'，零阻力 |
| `common/services/dict_seed_data.py` | 加 `platform` 字典（pdd=拼多多 / tiktok=TikTok Shop），供前端下拉与列表徽标 |
| `common/models/config_models.py` | BusinessHours 加 `weekdays` 列（`String(32), nullable=True, comment="生效星期，逗号分隔 0-6（周一=0），空=每天"`） |
| `websocket/engine/business_hours.py` | `is_within_business_hours(...)` 加可选 kwarg `weekdays`（None/空=每天，兼容旧调用方零改动） |
| `websocket/engine/reply_engine.py` | ShopConfig 加 `business_weekdays` 字段（默认 None）；decide_reply 调 business_hours 时透传 |
| `websocket/engine/message_consumer.py` | ① 构造器加 `message_parser` 注入参数（默认 None→PDD 解析器）；② `_to_context`(:402-419) 委托 parser；③ 顶层 `from channel_pdd.api.send_message import SendMessage` / `from channel_pdd.transfer_service import TransferService` / `PDDChatMessage` 三处硬 import 移入惰性构造/默认实现函数内（类型注解改字符串）；④ `load_shop_runtime` 读 `weekdays` 填入 ShopConfig。行为对 PDD 完全不变 |
| `websocket/channel_pdd/connection_manager.py` | `create_channel`(:59) / `start_channel`(:103) 加 `platform` 参数分派（'pdd'→现有路径；'tiktok'→TikTokChannel）；`start_enabled_channels`(:144) 读出 `shop.platform` 传递；创建 notifier 时同时注入 consumer 与 channel（告警用） |
| `websocket/channel_pdd/pdd_channel.py` | 构造器加可选 `event_notifier`；`_connect_with_retry` 达上限置 ERROR 分支(:296-305)经 AlertDedup 包装触发 `connection_disconnected` 告警（经 `build_notifier` → POST /api/v1/notify/events）。PDD 同步受益 |
| `websocket/login/playwright_login.py` | `_clean_singleton_lock_files`(:118) / `_launch_persistent_context_with_retry`(:153) / `_CHROMIUM_ARGS`(:50) 提取到新 `login/browser_launcher.py`，本文件改为调用（行为不变） |
| `websocket/routes/login.py` | PasswordLoginRequest 加 `platform` 字段（默认 'pdd'），路由内分派 `pdd_login.login_pdd` / `channel_tiktok.tiktok_login.login_tiktok` |
| `websocket/routes/connections.py` | ConnectRequest 加 `platform` 字段，`connect_connection` 透传给 connection_manager |
| `websocket/routes/messages.py` | Phase 1 不支持 TikTok 手动发送：`send_message` 按 platform 校验（查 Shop.platform），TikTok 返回 `error_response(-1, "TikTok 店铺暂不支持在线手动发送")` |
| `websocket/routes/cookies.py` | RefreshCookieRequest 加 platform；TikTok 店铺返回成功并注明跳过（登录态常驻 user-data-dir，不依赖 cookies_enc 刷新，Phase 1 跳过） |
| `backend/app/services/account_service.py` | upsert_shop(:128)/login_shop_by_password(:266)/import_shop_by_cookie(:314) 加 platform 参数并入库/透传 |
| `backend/app/api/routes/shops.py` | UpsertShopRequest / PasswordLoginShopRequest 加 `platform` 字段（默认 'pdd'，校验合法枚举） |
| `backend/app/services/shop_login_client.py` | `login_by_password`/`import_by_cookie` 加 platform 参数，透传给 websocket /login/* |
| `backend/app/services/connection_notify.py` | notify_connect/notify_disconnect/query_connected* 加 platform 参数透传（保持默认 'pdd'） |
| `backend/app/services/business_hours_service.py` + `backend/app/api/routes/business_hours.py` | 营业时间读写支持 weekdays 字段（校验 0-6 逗号串或空） |
| `backend/app/services/chat_send_client.py` | 发送前查 Shop.platform，TikTok 直接返回中文失败（Phase 1 裁剪） |
| `scheduler/tasks/constants.py` | 加 `TASK_TIKTOK_WINDOW = "tiktok_window"`，并入 SUPPORTED_TASK_KEYS |
| `scheduler/tasks/task_runners.py` | 加 `run_tiktok_window()`（见 5.3）；TASK_RUNNERS 注册 |
| `scheduler/tasks/service_client.py` | 复用现有 connect/disconnect/status 触发函数（如缺则补 platform 透传） |
| `common/core/config.py` + `.env.example` | 加 TikTok 配置项（见 5.6） |
| `frontend/src/pages/shop_management.vue` | 添加店铺弹窗加「平台」radio（拼多多默认 / TikTok Shop）；选 TikTok 仅展示账密登录 tab；列表加平台徽标列 |
| `frontend/src/api/shop_api.js` | loginShopByPassword 等请求体带 platform |
| `frontend/src/components/shop_settings/BusinessHoursPanel.vue` | 加「生效星期」多选（周一~周日；默认全选=每天） |
| `websocket/Dockerfile` | （视 s5 结论）加 xvfb/中文字体依赖；确认 4 实例资源 |

---

## 4. ChannelAdapter 协议与 PDDChannel 适配

### 4.1 协议（`websocket/channel_base.py`，新文件）

对照两个真实接缝设计（connection_registry 鸭子类型只要求 `async stop()`；PDDChannel 构造需要 message_queue + message_handler）：

```python
PLATFORM_PDD: str = "pdd"
PLATFORM_TIKTOK: str = "tiktok"
ALLOWED_PLATFORMS: tuple[str, ...] = (PLATFORM_PDD, PLATFORM_TIKTOK)

class ChannelAdapter(Protocol):
    """平台通道协议：与 connection_registry（鸭子类型 stop()）和
    connection_manager 装配约定对齐的最小接口。"""
    shop_id: str
    user_id: int
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def get_connection_status(self) -> Optional[Dict[str, Any]]: ...
```

**不做大协议**的理由：注册表消费的只有 `stop()`；状态查询消费 `get_connection_status()`；消息入站统一走「FIFO 队列 + message_handler(raw, shop_id, user_id)」回调（pdd_channel.py:109-110 已定义签名，TikTok 复用同一约定）。入队队列复用 `common.utils.message_queue` 的 `FifoMessageQueue`/`message_queue_manager`（纯 asyncio、平台无关；**已按 Phase 2 前置上移 common，2026-08-28**，`channel_pdd.message_queue` 保留为兼容转发表）。

### 4.2 工厂分派（connection_manager.create_channel）

```python
def create_channel(shop_id, shop_pk, user_id, *, platform=PLATFORM_PDD, ...) -> ChannelAdapter:
    notifier = build_notifier(shop_pk) if enable_notify else None
    if platform == PLATFORM_TIKTOK:
        # TikTok：consumer 注入 tiktok_message_parser + TikTokSender（见 4.3）
        ...构造 TikTokChannel(message_queue=queue, message_handler=handler, event_notifier=notifier)
    else:
        ...现有 PDDChannel 路径（传 event_notifier=notifier，行为不变）
```

PDDChannel **不改行为地适配**：仅加可选构造参数 `event_notifier`，在重连达上限分支调用；其余零改动。

### 4.3 MessageConsumer 解耦（关键设计）

TikTok 店铺复用 MessageConsumer 全链路（决策链/落库/实时推送/通知），只注入三个平台件：

1. **message_parser**（新增协议 `engine/message_parser.py`）：
   ```python
   MessageParser = Callable[[Any], Optional[Context]]
   def pdd_parse_raw(raw_message: Any) -> Optional[Context]: ...  # 现 _to_context 的 PDD 逻辑搬入
   ```
   MessageConsumer 构造器加 `message_parser: Optional[MessageParser] = None`，缺省惰性取 `pdd_parse_raw`。**角色归一化约定**：TikTok 解析器输出的 `Context.kwargs["from_user"]` 映射为 `'user'`（买家）/`'mall_cs'`（本店客服），与 PDD 语义对齐——consume_raw(:380-397) 的角色过滤零改动。
2. **sender**：`TikTokSender.send_text(recipient_uid, content) -> Optional[dict]` 同 PDD SendMessage 签名（同步）。**线程桥接**：MessageConsumer `_send_reply` 经 `asyncio.to_thread` 在线程池调 sender；TikTokSender 内部用 `asyncio.run_coroutine_threadsafe(dom_send_coro, main_loop).result(timeout)` 把 DOM 操作调度回主循环执行并阻塞等待结果（构造时由 TikTokChannel 注入主循环引用）。对 MessageConsumer 完全无感。
3. **transfer_service**：不注入。TikTok 店铺 `transfer_keywords` 恒为空表 → `_should_transfer` 恒 False → 永不触碰 `_get_transfer_service` 的惰性 PDD 构造。商品卡片分支同理（TikTok 消息类型不产生 GOODS_SPECIFIC）。AI 分支零改造（不配 LlmConfig 则 ai_enabled=False，message_consumer.py:218-222 已核实）。

---

## 5. channel_tiktok 模块详细设计

### 5.1 tiktok_channel.py —— TikTokChannel

```python
class TikTokChannel:
    def __init__(self, shop_id: str, user_id: int, shop_pk: int, *,
                 message_queue: MessageQueueProtocol, message_handler: MessageHandler,
                 status_manager=None, browser_session=None, event_notifier=None,
                 poll_interval: float = 5.0, respect_business_hours: bool = True): ...
    async def start(self) -> None:
        # 1) respect_business_hours=True 时先查店铺营业时间（含 weekdays），
        #    窗口外直接置 DISCONNECTED 状态并 return（不启动浏览器、不登记注册表）
        #    —— 保证 websocket 服务重启时 start_enabled_channels 不破坏「非营业时间浏览器不在线」
        # 2) BrowserSession.start() → 打开聊天页 → 创建 _monitor_loop 任务
    async def stop(self) -> None:   # 停循环任务 → BrowserSession.close() → 置 DISCONNECTED
    async def _monitor_loop(self) -> None:
        # 周期（poll_interval）抓会话列表快照 → 会话守卫判定 → 新买家消息入队
        # 每轮检测登录失效（URL 跳转登录页 / 出现登录表单选择器）→ 置状态 + 事件告警 + 停循环
    def get_connection_status(self) -> Optional[Dict[str, Any]]: ...
```

- 监控循环实现方式（DOM 轮询 or CDP 监听）由 Phase 0 决策定，对外接口不变。
- 登录失效检测复用 `selectors.py` 中 `LOGIN_PAGE_MARKERS`。

### 5.2 其余模块

```python
# browser_session.py
class BrowserSession:
    def __init__(self, shop_id: str, *, headless: Optional[bool] = None,
                 proxy_server: Optional[str] = None, user_data_dir: Optional[str] = None): ...
    async def start(self) -> None    # browser_launcher.launch_persistent_context(
                                    #   dir=f"{PLAYWRIGHT_USER_DATA_DIR}/tiktok_{shop_pk}",
                                    #   headless, proxy_server)——每店独立 user-data-dir 防关联
    @property
    def page(self)                   # 活跃聊天页
    async def close(self) -> None

# tiktok_message.py
class TikTokChatMessage:
    def __init__(self, raw: Dict[str, Any]) -> None
        # raw 由监控循环产出：{conversation_id, sender_role(buyer/seller), content,
        #                    msg_type, timestamp, nickname}
    def to_context(self, shop_id=None, shop_name=None) -> Context
        # from channel_pdd.pdd_message import Context, ContextType（通用 DTO，位置历史原因）
        # 角色归一化：buyer→'user'，seller→'mall_cs'（见 4.3）
        # 消息类型映射：text→TEXT，image→IMAGE，其余→SYSTEM_STATUS（不进决策链）

# tiktok_login.py
TIKTOK_SELLER_URL = "https://seller.tiktokshopglobalselling.com"  # 泰国站固定基址（2026-08-27 实测确认，见 §0.1）
TIKTOK_CHAT_PATH = "/chat/inbox/current"       # 实测聊天页路径；URL 必须带 oec_seller_id（见 spike/README）
async def login_tiktok(name: str, password: str) -> Optional[Dict[str, Any]]
    # Playwright 账密登录（选择器见 selectors.py；验证码人工等待 120s，对齐
    # playwright_login._login_wait_timeout_ms 模式）；成功导出 Cookie JSON +
    # 从页面抓 shop_id/shop_name（以 spike 实测接口为准）
async def refresh_tiktok_session(name: str) -> Optional[str]
    # 无头打开卖家中心，检测登录态；Phase 1 cookies_refresh 对 TikTok 跳过（登录态常驻）

# tiktok_sender.py
class TikTokSender:
    def __init__(self, shop_id: str, user_id: int, *, browser_session=None, main_loop=None): ...
    def send_text(self, recipient_uid: str, content: str) -> Optional[dict]
        # run_coroutine_threadsafe 桥接主循环（见 4.3）：
        # 点击会话列表项(recipient_uid) → 输入框 fill → human-like 逐字 type
        # → 点击发送 → 消息流出现己方气泡视为成功（超时 15s 视为失败）
    # send_image Phase 1 不实现（显式不支持）

# session_guard.py（纯逻辑，可属性测试）
class ReplyDebouncer:
    def __init__(self, silence_seconds: float = 45.0): ...
    def feed(self, msg: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]
        # 买家消息：登记/重置该会话 pending 计时；卖家消息：取消该会话 pending
        # 返回「静默期满、应处理」的最后一条买家消息（仅当会话最后一条来自买家）
    def pending_conversations(self) -> List[str]: ...

# selectors.py —— 集中管理（示例结构，以 spike 实测填充）
CONVERSATION_ITEM = "..."; UNREAD_BADGE = "..."
MESSAGE_LIST = "..."; MESSAGE_TEXT = "..."
CHAT_INPUT = "..."; SEND_BUTTON = "..."
MY_MESSAGE_BUBBLE = "..."; LOGIN_PAGE_MARKERS = (...)
```

### 5.3 时间窗控制（scheduler）

`run_tiktok_window()`（interval 60s 任务，经管理端手工建 `scheduled_task` 记录启用）：
1. 读启用且 `platform='tiktok'` 的店铺及其 BusinessHours（含 weekdays）；
2. 对每店计算期望态 `should_connect = is_within_business_hours(start, end, weekdays=..., enabled=...)`（北京时间，engine 同款逻辑——星期判定纯函数放 `common/utils/weekdays.py`，engine 与 scheduler 共用，scheduler 不 import websocket 包，符合规范 52）；
3. 经 HTTP `status-batch` 查实际态 → 期望连接而未连接则调 `connect`（带 platform），期望断开而连接则调 `disconnect`；
4. 期望态收敛为幂等，网络失败仅记 task_run_log。PDD 店铺不受影响（过滤 platform）。

engine 内 business_hours 判定是第二道闸（TikTokChannel.start 的 respect_business_hours）。

### 5.4 掉线/登录失效告警链路补全（两平台共用）

**触发点**：
1. `pdd_channel.py` `_connect_with_retry` 达重连上限置 ERROR 分支 → `event_notifier("connection_disconnected", "店铺 xx 长连接重连失败已达上限")`；
2. `tiktok_channel.py` 监控循环检测登录失效/浏览器崩溃 → `event_notifier("login_expired", "店铺 xx TikTok 登录态失效，需人工重新登录")` 或 `connection_disconnected`；
3. `routes/cookies.py` / `pdd_login.refresh_pdd_cookies` 刷新失败标记 relogin 处（TikTok Phase 1 跳过，PDD 补上）。

**事件流到企微的路径**（全部复用现有组件）：
`channel 侧 event_notifier = AlertDedup 包装(build_notifier(shop_pk))` → POST `/api/v1/notify/events`（websocket 侧 build_notifier，message_consumer.py:1481-1509）→ backend `notify.py` → `push_system_event`（notify_service.py:498-571）→ 该店铺启用渠道（wecom）→ 企微群机器人（send_via_channel urllib POST，notify_service.py:155-203 已实现；email 未接 SMTP 勿依赖）。

**防告警风暴**（新模块 `engine/alert_dedup.py`）：
```python
class AlertDedup:
    def __init__(self, silence_seconds: float = 1800.0): ...
    def should_send(self, key: str) -> bool      # (shop_pk, event_type) 键，静默期内 False
    def mark_sent(self, key: str) -> None
    def resolve(self, key: str) -> None          # 恢复（重连成功/重新登录）时清除
```
- 去抖动语义：重连过程中（RECONNECTING）不告警，只在终局（ERROR/登录失效确认）告警；恢复时 resolve，下次故障可再告警。
- notifier 传递：connection_manager 创建时构造一份，同时给 consumer（业务事件）与 channel（连接事件），两处共用同一 AlertDedup 实例。

### 5.5 店铺 platform 字段与管理端分派

数据层：Shop.platform 列（SchemaMigrator 幂等补列）+ `platform` 字典种子。

backend 分派链（全部默认 'pdd' 向后兼容）：
- shops 路由请求体加 platform → account_service 入库；
- shop_login_client.login_by_password(platform=...) → websocket `/login/password` 带 platform → pdd_login / tiktok_login 分派；
- connection_notify.notify_connect(platform=...) → websocket `/connections/connect` 带 platform → connection_manager 工厂分派；
- chat_send_client 对 platform='tiktok' 直接返回中文失败（Phase 1 在线聊天可看不可发）。

前端：shop_management.vue 添加弹窗加平台 radio；列表平台徽标；BusinessHoursPanel.vue 加星期多选（空=每天，兼容旧数据）。

### 5.6 配置新增（common/core/config.py + .env.example）

```
TIKTOK_SHOP_ENABLED（bool，默认 false——灰度总开关，false 时 TikTok 店铺连接请求直接拒绝）
TIKTOK_POLL_INTERVAL_SECONDS（默认 5.0）
TIKTOK_DEBOUNCE_SECONDS（默认 45.0）
TIKTOK_SEND_TIMEOUT_SECONDS（默认 15.0）
TIKTOK_LOGIN_WAIT_TIMEOUT_MS（默认 120000）
TIKTOK_MAX_BROWSER_INSTANCES（默认 4，超限拒绝连接并告警）
```
遵循规范 21（不写死地址；seller.tiktokshopglobalselling.com 为泰国站固定基址，与 PDD_WEBSOCKET_BASE_URL 同等待遇允许写死并注释说明）。

---

## 6. 测试策略

### 6.1 新增测试文件

| 文件 | 覆盖 |
| --- | --- |
| `websocket/tests/test_tiktok_message.py` | raw→Context、角色归一化（buyer→user/seller→mall_cs）、类型映射、非法 raw 容错 |
| `websocket/tests/test_session_guard.py` | Hypothesis 属性测试：仅最后一条为买家消息才回、静默聚合、卖家回复取消 pending |
| `websocket/tests/test_tiktok_sender.py` | FakePage 注入：点击/输入/发送调用序、human-like 分段、成功检测（气泡出现/超时）、线程桥接（run_coroutine_threadsafe mock） |
| `websocket/tests/test_browser_session.py` | user-data-dir 按 shop_pk 隔离、代理参数传递、Singleton 锁清理（mock browser_launcher） |
| `websocket/tests/test_tiktok_channel.py` | 生命周期（start/stop 任务清理）、时间窗外 start 不启动浏览器、登录失效检测→告警、监控循环 diff 入队（mock browser_session） |
| `websocket/tests/test_channel_factory.py` | create_channel 按 platform 分派；PDD 默认路径行为不变（回归） |
| `websocket/tests/test_message_parser.py` | MessageConsumer 注入 TikTok parser + StubSender 全链路（对齐既有 StubSender 注入模式）；PDD 默认 parser 回归 |
| `websocket/tests/test_alert_dedup.py` | 静默窗口去重、resolve 后可再发 |
| `websocket/tests/test_business_hours_weekdays.py` | 星期维度 Hypothesis 属性测试（周中/周末/空值兼容/跨午夜+星期组合） |
| `backend/tests/test_shop_platform.py` | shops 路由 platform 字段、登录分派透传、chat_send_client TikTok 拦截 |
| `scheduler/tests/test_tiktok_window.py` | 期望态收敛逻辑（connect/disconnect 决策，mock service_client） |

### 6.2 Playwright stub 模式（双层 mock）

- **browser 层**：`FakePlaywright/FakeBrowserContext`（async `launch_persistent_context` 返回 FakeContext）；browser_session 构造器接受可注入 playwright 工厂（`browser_factory=None` 缺省走 async_playwright）。
- **page 层**：`FakePage`（async `click/fill/type/wait_for_selector/wait_for_function/query_selector_all`，locator 以预设 dict 应答），tiktok_sender/tiktok_channel 构造器接受 `page=None` 缺省从 session 取。
- 监控循环的「快照 diff」拆成纯函数（`diff_conversations(old, new)`）直接属性测试，不依赖 mock。
- 对齐 `test_message_consumer.py` StubSender 模式（:47-61）：TikTokSender 同签名，测试直接替换注入。

---

## 7. Phase 2 概要（安全加固 + 多店）

- **频率断路器**（**发送最小随机间隔已交付，2026-08-28**；**频率限制配置化已交付，2026-08-29**：TIK-020 为 TikTok 测试店配置「单店铺 20 条 / 3600 秒」规则并真实库演练通过——超限后暂停自动回复、`pdd_risk_log` 可查（`frequency_limit`）；补充 `websocket/tests/test_risk_control.py` 17 例；演练 CLI `tools/tiktok_acceptance/risk_rule_drill.py`；配置指引 [RISK_RULE_GUIDE.md](./RISK_RULE_GUIDE.md)；未配规则店铺行为不变已验证）：复用 engine 风控现成组件（RiskRule 的 shop_reply_limit/window_seconds 已实现会话/店铺维度限流，risk_control.py 纯逻辑零改造，只需给 TikTok 店铺配置规则=「每小时上限」）；发送最小随机间隔（45-120s）已内置 tiktok_sender（`_enforce_min_send_interval`：主循环侧、串行锁内，每次发送前 `asyncio.sleep(random.uniform(min,max))`，默认 45-120s，构造参数可覆盖、上限 ≤0 关闭，测试注入 mock 验证）。
- **多店并发**：4 实例资源评估（s5 实测数据决定容器 memory limit 与 TIKTOK_MAX_BROWSER_INSTANCES，TIK-017 已落地）；**店铺级代理字段（已按 Phase 2 前置交付，2026-08-28）**：`Shop.proxy_server` 列（SchemaMigrator 幂等补列）+ backend 路由/服务校验透传（`validate_proxy_server`，支持 http(s):// 与 socks5(5h)://）+ websocket 装配（`ConnectRequest.proxy_server` → `start_channel` → `create_channel` → `BrowserSession(proxy_server=...)`，`start_enabled_channels` 自动拉起时从库读出）+ scheduler `trigger_connect(proxy_server=...)` 透传 + 前端编辑表单输入（仅 TikTok 店铺展示）；默认关闭（空）。
- **对账**：每日任务核对 chat_message 表 vs 平台后台计数（人工抽查半自动）——**半自动工具已备**：`tools/tiktok_acceptance/reconcile.py`（TIK-018 验收工具，见 TICKETS_TIKTOK.md TIK-018）。
- **在线聊天手动发送 TikTok 支持**（**已交付，2026-08-28**：`routes/messages.py` TikTok 分支复用活跃通道的 `TikTokSender`（registry→`channel.sender`，与自动回复共享串行锁；手动发送 `enforce_interval=False` 跳过 45-120s 最小随机间隔、经 `asyncio.to_thread` 不阻塞事件循环；无活跃连接返回「未连接」）；backend `chat_send_client` 去掉 TikTok 拦截（平台分派收敛到 websocket 侧，TIK-003 的「可看不可发」裁剪撤销））；**登录态保活（已交付代码侧，2026-08-28**：`TikTokChannel` 登录失效后自动恢复探测——新增 `channel_tiktok/login_recovery.py`（`wait_login_recovery` 纯注入式探测：`login_recovery_interval` 默认 60s、`login_recovery_max_tries` 默认 10 次，人工重登后自动重开聊天页续接监控，超时未恢复才停循环）；scheduler `cookie_refresh` 修复 TikTok 误走 PDD 刷新路径（`_list_enabled_shops_with_platform` 读出 platform → `trigger_cookie_refresh(platform=...)` 透传，websocket 侧按平台跳过）；刷新任务周期待 TIK-018 实测登录态过期周期后配置）。
- ~~message_queue 上移 common~~（**已按 Phase 2 前置交付，2026-08-28**：实现迁至 `common/utils/message_queue.py`，`channel_pdd.message_queue` 保留为兼容转发表，存量调用方零改动；测试随迁 `common/tests/test_message_queue.py` + websocket 转发表身份测试）。

## 8. Phase 3 概要

- 4 家真实店铺接入（灰度：先 1 家再 4 家，TIKTOK_SHOP_ENABLED + 店铺启用状态控制）。
- 24h 回复率监控：基于 message_log/chat_message 统计「首响时长分布、超 5 分钟占比」，dashboard 加 TikTok 平台维度。
- 告警完善：回复率跌破 85% 阈值企微告警（新增事件类型，走同一 notify 链路）。

---

## 9. 风险清单与部署影响

**技术风险**：
1. TikTok DOM 变化致选择器失效 → selectors.py 集中管理 + 监控循环检测选择器失配时告警（「页面结构变化」事件）。
2. 反自动化检测（headless 特征/行为分析）→ 持久化 context、--disable-blink-features=AutomationControlled、human-like 输入、频率克制；s5 实测决定 headless vs Xvfb。
3. 消息漏检（轮询间隔内会话被顶出列表首屏）→ 轮询覆盖未读徽标 + 会话列表滚动；CDP 方案则依赖报文稳定性。
4. 4 浏览器实例资源（内存约 0.5-1GB/店）→ 容器限额与实例上限配置。
5. run_coroutine_threadsafe 桥接的并发安全 → TikTokSender 发送串行化（asyncio.Lock 于主循环侧），避免同店并发 DOM 操作。
6. Windows/Linux 差异（Singleton 锁文件形态）→ browser_launcher 已兼容（islink 判断）。

**平台风控**：账号限流/封禁（业务方已接受）；RPA 违反平台条款风险（已知悉）；仅发送合规引导话术（模板+关键词，无 LLM，内容可控）；买家数据存储合规沿用现状。

**部署影响**：websocket 容器内存上限（**2026-08-29 口径调整：单店监督，预留 1×1GB；现状 `mem_limit: 2g` 满足该预留并含 PDD 长连接/Python/系统缓冲余量**，原 4×1GB 为 4 店口径已随 TIK-027 取消不再适用，见 TIK-022）；视 s5 结论加 xvfb（实际 headless 可用、无需 Xvfb）；`websocket_browser_data` 卷挂载 `/app/websocket/browser_data`（单店 user-data-dir = 卷内 `tiktok_{shop_pk}` 子目录，TIK-022 核对）；`.env` 新增 TIKTOK_* 变量（6 项，见 §5.6）。

---

## 10. 实施顺序（依赖与并行）

```
并行批次 A（互不依赖，可同时开工）：
  A1. Phase 0 spike（需测试店账号；产出阻塞 B3/B4 的方案决策）
  A2. Shop.platform 字段 + 字典 + backend/websocket 路由分派（§5.5）
  A3. 掉线告警链路补全（AlertDedup + PDDChannel event_notifier，PDD 独立受益，立即可上）
  A4. business_hours 星期维度（common/utils/weekdays 纯函数 + 模型 + engine + 前后端）
批次 B（依赖 A2；B3/B4 依赖 A1 结论）：
  B1. MessageConsumer parser 解耦 + 硬 import 惰性化（含回归测试）
  B2. connection_manager 工厂化（依赖 A2 + B1）
  B3. channel_tiktok 六模块（selectors 按 spike 清单填充）
  B4. login/browser_launcher.py 提取（playwright_login 行为不变回归）
批次 C（依赖 B 全部）：
  C1. TikTokChannel 装配进 connection_manager + routes/connections 联调
  C2. scheduler tiktok_window 任务 + 管理端建任务
  C3. routes/login、messages、cookies 的平台分派
批次 D（收尾）：
  D1. 测试店端到端验收（周末窗口真实消息 → 5 分钟首响 → 企微告警演练 → 日志对账）
  D2. Dockerfile/资源/环境变量调整
```

关键验收（Phase 1 出口）：测试店周末无人值守期间，买家新消息 5 分钟内收到模板首响；消息日志与平台后台一致；人为杀掉浏览器/清除登录态后，企微立即收到对应告警，且告警静默窗口（默认 30 分钟）内同一事件不重复轰炸、恢复后可再次告警。
