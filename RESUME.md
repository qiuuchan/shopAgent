# shopAgent · 简历向项目说明

> 面向招聘的项目卡。一句话定位 + 可贴简历的量化 bullet + 面试可深问的技术依据 + 二开边界诚信声明。
> 仓库：https://github.com/qiuuchan/shopAgent （fork 自 zhinianboke/pdd-auto-reply，AGPL-3.0）

---

## 一句话定位

面向拼多多 + TikTok Shop 多店铺商家的 7×24 客服消息自动化系统：双通道（拼多多长连接 + TikTok Playwright RPA）实时收发消息，经 9 级短路决策链与 LLM 工具调用 Agent 循环自动应答、转人工、风控与营业时间管控。

---

## 可直接贴简历的项目描述（量化版）

**shopAgent — 拼多多 + TikTok 多平台客服自动化系统（主导二次开发）**

- 基于 pdd-auto-reply 开源版二次开发，**主导 TikTok Shop 多平台通道扩展**：将原写死拼多多的架构重构为 `channel_base` 平台抽象层，实现双通道（拼多多长连接 + TikTok Playwright RPA）并存，新增 `channel_tiktok` 通道包（8 模块，单文件 ≤500 行规范）
- 设计 **9 级短路优先级自动回复决策链**（纯逻辑、无 I/O，输出 frozen dataclass 便于属性测试）：黑名单→过滤→营业时间→风控→关键词→商品专属→AI→默认回复→无匹配
- 实现 **LLM Agent 循环**：LLM↔知识库工具调用闭环，`max_loops` 上限 + 末轮 `tool_choice=none` 强制收尾 + 整体超时预算 + 四类失败（配置不可用/超时/异常/空内容）全回退默认回复，落地"不信任模型输出"工程哲学
- **工具调用经 `asyncio.to_thread` 线程池**执行同步 DB 检索避免阻塞事件循环；注入店铺隔离参数覆盖模型可能给错的 `shop_id` 保证数据隔离；jieba 中文分词检索商品/客服知识库
- **多模型供应商适配**（4 类协议：OpenAI 兼容 / Anthropic / Gemini / DashScope），仅依赖标准库 urllib，纯函数与网络分离便于单测，Gemini 经 header 传 key 防日志泄漏
- **TikTok RPA 通道工程化**：同步签名桥接主循环（`run_coroutine_threadsafe`）+ `asyncio.Lock` 串行化防同店并发 + 成功检测（`wait_for_selector` 己方气泡）+ 频率断路器（45–120s 随机节流降风控）+ human-like 输入 + 企微告警链路
- **600+ pytest 用例**（common/backend/websocket/scheduler 四服务）全绿，Hypothesis 属性测试（`max_examples=200`）+ 内存 SQLite 隔离 + mock 服务间调用；二开期间修复 Flaky 与发送器资源泄漏

---

## 量化指标

| 维度 | 数据 |
| --- | --- |
| 服务拆分 | 4 后端微服务（common 公共库 + backend API + websocket 长连接 + scheduler 定时）+ Vue3 前端 |
| 测试用例 | **624 个**（common 35 / backend 237 / websocket 319 / scheduler 33），重跑全绿 |
| 属性测试 | Hypothesis `max_examples=200`，内存 SQLite + `@compiles(BigInteger,"sqlite")` 适配，不依赖真实 MySQL/Redis |
| TikTok 通道 | 8 模块（channel/login/sender/session/selectors/guard/recovery/message），单文件 ≤500 行 |
| LLM 协议适配 | 4 类（OpenAI 兼容 / Anthropic / Gemini / DashScope） |
| 决策链 | 9 级短路优先级 |
| 二开 commit | 11 个（平台抽象 + TikTok 通道 + 企微告警链路 + 端到端实测调优等），全部为本人提交 |
| 基础设施 | MySQL 8.0 + Redis 7 + Docker Compose 编排 6 服务（健康检查 + 滚动更新） |

---

## 技术栈

**后端（Python ≥3.11）**：FastAPI + Uvicorn（ASGI）、SQLAlchemy 2.0（同步）+ PyMySQL（参数化查询）、websockets（拼多多长连接）、Playwright（TikTok RPA + 账号登录）、openai 兼容客户端 + jieba（AI 回复与知识库检索）、PyJWT + passlib[bcrypt]（鉴权与密码哈希）、cryptography（Fernet 对称加密）、APScheduler、pytest + Hypothesis。

**前端**：Vue 3 + Vite 6 + Vue Router + Pinia + Tailwind CSS + lucide-vue-next + axios。

**基础设施**：MySQL 8.0（utf8mb4、全链路北京时间 UTC+8）、Redis 7（缓存/分布式锁）、Docker Compose（多阶段构建 + envsubst 模板渲染 + 健康检查 + 命名卷持久化）。

---

## 技术亮点详解（面试可深问）

### 1. 多平台通道抽象（二开核心）

原系统 `MessageConsumer` 硬 import 拼多多 API（`SendMessage`/`PDDChatMessage`/`TransferService`）。二开抽出 `channel_base` 平台抽象层，新增 `channel_tiktok` 包，使两条通道并存且共享上层消费与决策链。

- **拼多多通道**：维护商家后台长连接（自动重连 + 心跳），`anti-content` 签名，消息入 FIFO 队列按序消费。
- **TikTok 通道**：TikTok 无公开长连接 API，采用 **Playwright RPA** 方案——CDP 被动监听收消息 / DOM 自动化发消息 / 选择器集中到 `selectors.py` 单一知识点 / headless 风控可检测性预研 / 每店独立 `user-data-dir` 持久化登录态。

### 2. Agent 决策链（reply_engine · 纯逻辑）

`decide_reply` 按 9 级固定优先级短路返回 `ReplyDecision`（frozen dataclass）。设计为**纯逻辑、无 I/O**：店铺配置与运行时计数由上层传入，本模块只做判定，不访问数据库/不调 LLM/不发消息。AI 分支仅给出 `action='ai'` 决策，实际 LLM 调用与失败降级由 `ai_reply_engine` 执行——**决策与执行分离**，便于属性测试。

### 3. LLM Agent 循环（ai_reply_engine · 不信任模型输出）

`_run_agent_loop` 实现 LLM↔工具调用循环：调 LLM（`tool_choice=auto`）→ 有工具调用则执行（`asyncio.to_thread` 线程池跑同步 DB 检索）→ 结果回传 messages → 再次调 LLM，直至无工具调用或达 `max_loops`。关键设计：

- **末轮强制收尾**：达上限前一轮注入 `[已达到最大工具调用次数，请基于已有信息给出最终中文回复]` 并 `tool_choice=none`，防止无限工具调用。
- **整体超时预算** = `timeout × (max_loops+1)`，避免工具往返被单次调用超时误判。
- **四类失败全回退默认回复**并记 `ai_reply_failed`：配置不可用 / 超时 / 异常 / 空内容——每条回复过校验层。
- **店铺隔离**：工具入参注入 `shop_id` 覆盖模型可能给错的值，保证多租户数据隔离。
- **错误日志附端点信息**（provider/base_url/model，绝不含密钥），便于定位 `Connection error` 类网络异常。

### 4. 多模型供应商适配（ai_provider_service · 仅标准库）

4 类协议（OpenAI 兼容 / Anthropic / Gemini / DashScope）统一适配，**仅依赖标准库 urllib**（common 运行期无 httpx）。纯函数（类型规范化/名称识别/配置校验）与网络（`test_ai_connection`/`fetch_model_list`）分离，便于单测。安全细节：Gemini 经 `x-goog-api-key` 请求头传密钥，避免密钥出现在 URL 查询串随日志泄漏。

### 5. TikTok Sender 工程化（同步/异步桥接 + 防风控）

`TikTokSender.send_text` 同步签名对齐拼多多 `SendMessage`，经 `asyncio.run_coroutine_threadsafe` 把 DOM 操作调度回主循环执行，对 `MessageConsumer` 完全无感。主循环侧 `asyncio.Lock` 串行化防同店并发 DOM 互扰；成功检测靠 `wait_for_selector` 等己方气泡出现（超时视为失败）；频率断路器在发送前 sleep `[45,120]s` 随机间隔模拟人工节奏降风控（手动发送 `enforce_interval=False` 跳过）。周末实测完成 TIK-018 端到端验收：真实买家消息→9 级决策→DOM 发送全链路打通，稳态首响 69–81s（阈值 300s），并修复 `:has(:text-is)` 不受支持、CS 子账号视角自发送回声、节流超时误判三处实测问题。

### 6. 测试体系

- 内存 SQLite + `@compiles(BigInteger,"sqlite")` 把 BIGINT 渲染为 INTEGER（否则 SQLite 自增主键失效），不依赖真实 MySQL/Redis。
- 服务间网络调用处均有 mock；纯逻辑组件（engine/agent/common 工具）无 I/O 便于单元与属性测试。
- Hypothesis `max_examples=200`；二开期间修复测试套件 Flaky 与一处发送器资源泄漏。

---

## 架构说明（配合架构图）

消息流主线：买家消息 → 双通道（拼多多长连接 / TikTok RPA）→ `channel_base` 平台抽象 → `MessageConsumer`（FIFO 队列，每店独立）→ `reply_engine` 9 级决策链 → AI 分支交 `ai_reply_engine` Agent 循环（LLM ↔ `kb_service` 知识检索 / `ai_provider` 多模型）→ Sender 发送回复。

三后端服务协作：`backend`（唯一对外 API + 鉴权 + 唯一执行建表/补字段启动迁移）、`websocket`（长连接 + 回复引擎 + AI 引擎 + Playwright 登录）、`scheduler`（APScheduler 定时任务：cookie 刷新 / 商品同步 / 日志清理）。公共逻辑收敛至 `common` 库经 `sys.path` 共享复用。

---

## 二开边界与诚信声明

- 本项目 **fork 自开源项目 [zhinianboke/pdd-auto-reply](https://github.com/zhinianboke/pdd-auto-reply)**（AGPL-3.0），非从零自研。简历/面试中应表述为"基于开源项目二次开发，主导 XXX 改造"，不宜表述为纯原创。
- 本人（qiuuchan/shopAgent）的主导改造范围（8 个 commit，全部为本人提交）：
  1. 店铺平台/代理字段、营业时间星期维度、`message_queue` 上移（common 层平台解耦基础）
  2. backend 店铺平台分派链 + 营业时间 weekdays 读写
  3. **TikTok 通道包 + engine 平台解耦 + 掉线告警链路补全**（websocket，核心二开）
  4. `tiktok_window` 时间窗任务 + 店铺平台/星期 UI（scheduler + frontend）
  5. TikTok 配置模板/编排/资源调整 + spike 报告 + TIK-018 验收工具 + 文档
  6. 修复测试套件警告与偶发 Flaky + 一处发送器资源泄漏
  7. TIK-018 前置修复：TikTok 真实登录、登录态目录复用与连接超时
  8. 企微告警渠道配通：群机器人协议适配与 errcode 校验
- 原架构（拼多多长连接通道、决策链骨架、AI 引擎骨架、backend/scheduler 主体、前端主体）为原作者工作，本人工作集中在**多平台扩展（TikTok 通道 + 平台抽象）+ 告警链路 + 工程质量收敛**。

---

## 已知技术债（nice-to-fix，体现工程严谨）

- ~~`common` 1 例 Hypothesis 属性测试（`test_repository_paginate_property`）在完整套件下偶发失败~~ **已修复**（2026-08-28：补 `deadline=None`，与同项目重操作属性测试惯例对齐；累计 9 次完整套件 + 7000+ examples 验证断言无反例，根因为主机负载下默认 200ms/example deadline 偶发超时，非实现缺陷）。
- ~~`backend` 存在 `datetime.utcfromtimestamp()` DeprecationWarning~~ **已修复**（2026-08-28 迁移至 `datetime.fromtimestamp(ts, timezone.utc)`，auth_service.py / profile.py）。
- `message_consumer.py` 1547 行，超单文件 ≤500 行规范（存量现状，新增文件已遵守）。
- 前端为纯 JS（无 TypeScript / ESLint / Prettier 配置）。

---

## 与 2026 年 Agent 应用开发岗位 JD 的对标

> 对标来源（2026-08 检索）：滴滴 Agent应用开发工程师、地平线 AI Agent应用工程师、中小企业服务网 AI Agent智能体开发工程师、厦门医疗 AI Agent开发工程师、国药 AI应用开发工程师（Agent方向）、盒子科技/喂车科技/知象光电（深圳应届岗），及海外 Agentic AI Engineer 岗位与 GenAI 岗位技能统计（RAG 提及率 60%+、LLM observability 提及量 2023-2025 涨 450%）。

**结论：可作为简历主力亮点项目，但需管理预期——命中 5/10，部分 3/10，待补强 2/10。**

### 命中（可作硬核卖点）
| 维度 | 项目依据 |
| --- | --- |
| Agent 编排与执行链路 | 自研 Agent 循环（LLM↔工具）+ 9 级短路决策链 + FIFO 消费 + 超时预算/兜底 |
| 工具调用 / Function Calling | OpenAI function schema + TOOL_REGISTRY + 店铺隔离参数注入 + 缺参中文提示 |
| 安全与护栏 | JWT 黑名单 + Fernet 加密 + 参数化 SQL + 密钥防泄漏 + RBAC 权限模块 |
| 工程化部署 | Docker Compose 6 服务 + 健康检查 + 滚动更新 + MySQL/Redis + CI/CD 脚本 |
| 测试与工程质量 | 624 用例 + Hypothesis 属性测试 + 内存 SQLite 隔离全绿（满足"可展示的完整 Agent 项目案例"直接要求） |

### 部分匹配（可讲、但别吹过头）
- **RAG**：jieba 关键词 + goods_id 精确匹配，缺向量检索/Embedding/混合检索/重排
- **记忆**：会话历史传入 LLM，缺长期记忆/向量记忆/上下文压缩
- **可观测性**：日志/风控日志/企微告警，缺 LLM trace（Langfuse 类）

### 待补强（面试会被追问的 2 个缺口）
1. **LangChain/LangGraph 框架**：JD 点名频率最高的框架，本项目为自研 ReAct 循环。面试须能讲清"为什么自研"（成本可控、无框架绑定、决策链/工具调用逻辑自己完全掌握）；简历表述为"自研 Agent 循环（ReAct 模式）"而非"熟悉 LangChain"。
2. **LLM 效果评测体系**：2026 年 JD 最强调的差异化技能（golden dataset / LLM-as-judge / RAGAS）。项目有 624 工程测试但无 LLM 效果评测。

### 补强路线（按回报排序）
- **P0（1-2 天，回报最高）**：给知识库检索加向量检索层——sqlite-vec 或 Qdrant 做 Embedding + BM25 关键词与向量混合检索（可复用本人 KB-AI 项目的 Qdrant + Cross-Encoder 重排经验），补齐 JD 第一高频技术（RAG 60%+ 提及率），面试可讲"混合检索 + 重排"生产级方案
- **P1（1 天）**：加最小 LLM 评测模块——golden QA 集 + 检索命中率/回复满意度指标（可选 LLM-as-judge），命中"评测"这一 2026 最被强调的技能
- **P1（半天）**：LangGraph spike——用 LangGraph 重写决策链做技术验证（不重构主项目），简历可写"接触过 LangGraph，并对比过自研 vs 框架的取舍"
- **P2（可选）**：MCP 工具接入、多 Agent 协作、长期记忆

---

## 运行与验证

```bash
python -m venv .venv
.venv/Scripts/pip install -e "common[test]" -e "backend[test]" -e "websocket[test]" -e "scheduler[test]"

cd common     && ../.venv/Scripts/python -m pytest     # 35 用例
cd backend    && ../.venv/Scripts/python -m pytest     # 234 用例
cd websocket  && ../.venv/Scripts/python -m pytest     # 299 用例
cd scheduler  && ../.venv/Scripts/python -m pytest     # 33 用例
```

测试用内存 SQLite，不依赖真实 MySQL/Redis；Windows 需 `pip install tzdata`，bcrypt 需 pin `<4.1`（passlib 1.7.4 兼容）。
