# AGENTS.md —— 拼多多自动回复系统（pdd-auto-reply）

本文档供 AI 编码代理（及新加入的开发者）快速理解本项目。所有信息均基于对仓库实际代码、配置与实测结果的核对，非泛化假设。

## 项目概览

面向拼多多多店铺商家的客服消息自动化系统：基于拼多多商家后台实时收发客服消息（长连接），结合关键词规则、AI 智能回复（LLM + 知识库工具调用），实现 7×24 小时自动应答、转人工、风控与营业时间管控。管理端为「左侧导航 + 右侧内容」的 Vue 3 单页应用，蓝白主色调、支持暗黑模式。

系统按服务拆分为 4 个后端组件 + 1 个前端 + 基础设施：

| 组件 | 目录 | 说明 | 默认端口 |
| --- | --- | --- | --- |
| common 公共库 | `common/` | 无独立入口，被各服务经 `sys.path` 共享复用 | — |
| backend | `backend/` | HTTP API 服务（FastAPI），承载全部 REST 接口 | 8089 |
| websocket | `websocket/` | 长连接服务：拼多多通道、回复引擎、AI 引擎、Playwright 登录 | 8090 |
| scheduler | `scheduler/` | 定时任务服务（APScheduler） | 8091 |
| frontend | `frontend/` | Vue 3 前端，nginx 提供静态资源并反向代理 | 80（宿主可改） |
| MySQL 8.0 / Redis 7 | — | docker-compose 内置基础设施 | 3306 / 6379 |

各 Python 服务以各自目录下 `main.py` 为统一入口（最小入口桩），真正的应用装配在 `_bootstrap.py`；公共逻辑收敛至 `common` 库。

## 技术栈

- **后端（Python ≥3.11）**：FastAPI + Uvicorn（ASGI）、SQLAlchemy 2.0（同步）+ PyMySQL（MySQL，参数化查询）、websockets（拼多多长连接）、Playwright（账号密码登录）、openai 兼容客户端 + jieba（AI 回复与知识库检索）、**向量 embedding（OpenAI 兼容 `/embeddings`，仅标准库 urllib；知识向量落 MySQL TEXT 列）**、PyJWT + passlib[bcrypt]（鉴权与密码哈希）、cryptography（Fernet 可逆加密）、pydantic / pydantic-settings（校验与配置）、APScheduler（仅 scheduler）、aiohttp（仅 scheduler，服务间调用）。
- **前端**：Vue 3 + Vite 6、Vue Router 4 + Pinia、Tailwind CSS 3（class 暗黑模式）+ lucide-vue-next、axios。无 TypeScript、无 ESLint/Prettier 配置（前端为纯 JS）。
- **基础设施**：MySQL 8.0（utf8mb4、全链路北京时间 UTC+8）、Redis 7（缓存 / 分布式锁）。
- **许可证**：AGPL-3.0。

## 目录结构

```
pdd-auto-reply/
├── common/                  # 公共库（sys.path 共享，无 main.py）
│   ├── core/config.py       #   统一配置加载（pydantic-settings，环境变量优先）
│   ├── db/                  #   连接池(session.py)、通用仓储(repository.py)、
│   │                        #   失败重试(retry.py)、启动迁移(init_database.py)
│   ├── models/              #   数据模型，按业务域拆分（user/shop/reply/knowledge/
│   │                        #   config/log/setting/task + base.py 基类混入）
│   ├── schemas/             #   统一响应体(common.py)、输入清洗(sanitize.py)
│   ├── services/            #   字典服务、AI 供应商、知识库、管理员种子、服务间客户端、
│   │                        #   向量 embedding(embedding_service，P0L)、向量索引
│   │                        #   (kb_indexing，P0L)、混合排序(kb_hybrid，P0L)
│   └── utils/               #   加解密(crypto.py)、安全(security.py)、分页、北京时间、
│                            #   星期/营业时间判定(weekdays.py/business_hours.py)、
│                            #   首响时长统计纯函数(latency.py，TIK-025 由 tools 上移)
├── tools/                    # 验收/统计/评测独立工具（不进服务代码，经 sys.path 引用 common）
│   ├── agent_eval/          #   LLM 评测模块（golden 集 / 确定性指标 / LLM-as-judge /
│   │                        #   baseline vs candidate 对比）——TIK/P0L
│   └── kb_embedding/        #   知识库向量回填 CLI（--full/--delta/--check）——P0L
├── backend/                 # HTTP API 服务（8089）
│   ├── app/api/routes/      #   REST 路由（auth/users/roles/shops/keywords/replies/
│   │                        #   ai_config/knowledge/risk_control/chat/... 共 28 个域）
│   ├── app/api/deps.py      #   鉴权依赖（Bearer JWT → 当前用户）
│   ├── app/api/router.py    #   全部路由聚合，前缀 /api/v1
│   ├── app/core/            #   业务码、异常处理器、权限、令牌黑名单
│   ├── app/services/        #   业务服务（薄路由 + 厚服务）
│   └── main.py / _bootstrap.py
├── websocket/               # 长连接服务（8090）
│   ├── channel_pdd/         #   拼多多通道：连接管理、消息队列、登录、转人工、
│   │                        #   核心（连接状态机/凭据存储/anti-content 签名）
│   ├── engine/              #   回复引擎：关键词匹配、消息过滤、营业时间、风控、
│   │                        #   决策链(reply_engine.py)、消息消费(message_consumer.py)
│   ├── agent/               #   AI 回复引擎、LLM 客户端、工具调用
│   ├── login/               #   Playwright 登录、Cookie 导入
│   ├── routes/              #   供 backend/scheduler 经 HTTP 调用的接口
│   └── main.py / _bootstrap.py
├── scheduler/               # 定时任务服务（8091）
│   ├── tasks/               #   SchedulerService、任务执行体、执行日志、常量
│   └── main.py / _bootstrap.py
├── frontend/                # Vue 3 前端
│   ├── src/pages/           #   页面（看板/店铺/关键词/知识库/在线聊天/日志/管理端等）
│   ├── src/api/             #   接口封装（axios 实例 + 各业务域 API）
│   ├── src/router/          #   路由（由 config/navigation.js 菜单配置生成）
│   ├── src/store/           #   Pinia（用户/布局/标签页/UI）
│   ├── src/components/      #   布局、通用组件、店铺设置面板
│   ├── src/utils/           #   request.js(axios 封装)/chat_ws/theme/format 等
│   └── vite.config.js       #   端口 9100，/api 与 /static 代理到后端
├── docker-compose.yml       # 全量编排（mysql/redis/backend/websocket/scheduler/frontend）
├── docker-compose.db.yml    # 仅本地二开：MySQL + Redis（映射 127.0.0.1）
├── build.sh / deploy.sh / update.sh   # 一键构建 / 部署 / 滚动更新
└── .env.example             # 环境变量模板（部署脚本自动复制为 .env）
```

## 常用命令（构建 / 测试 / 运行）

### 测试（已实测，见文末「实测记录」）

```bash
# 建议：创建虚拟环境后，按服务安装（common 必须先装，其余服务经 sys.path 引用它）
python -m venv .venv
.venv/Scripts/pip install -e "common[test]" -e "backend[test]" -e "websocket[test]" -e "scheduler[test]"
# （Linux/macOS 用 .venv/bin/pip）

cd common     && ../.venv/Scripts/python -m pytest     # 公共库测试
cd backend    && ../.venv/Scripts/python -m pytest     # API 测试
cd websocket  && ../.venv/Scripts/python -m pytest     # 长连接/引擎测试
cd scheduler  && ../.venv/Scripts/python -m pytest     # 调度测试
cd tools      && ../.venv/Scripts/python -m pytest tests  # 验收/评测工具测试
```

测试全部使用内存 SQLite（conftest 将 `get_db` / `get_session_factory` / `get_engine` 重定向到测试引擎），不依赖真实 MySQL/Redis；服务间网络调用处均有 mock。属性测试统一使用 Hypothesis（默认 `max_examples` 不低于 100，实测常见 200）。

### 本地运行

```bash
cp .env.example .env        # 按需修改（数据库/Redis/密钥/端口）

cd backend && ../.venv/Scripts/pip install -e . && python main.py      # 8089
cd websocket && ../.venv/Scripts/pip install -e . && playwright install chromium && python main.py   # 8090
cd scheduler && ../.venv/Scripts/pip install -e . && python main.py    # 8091

cd frontend && npm install && npm run dev    # Vite dev，端口 9100，/api 代理到 8089
cd frontend && npm run build                # 生产构建产物到 dist/
```

仅本地二开数据库：`docker compose -f docker-compose.db.yml up -d`（MySQL 3306、Redis 6379 映射到 127.0.0.1）。

### 容器化部署

```bash
bash build.sh [service]   # 仅构建镜像
bash deploy.sh [-y]       # 清理本项目容器/镜像（保留数据卷）→ 构建 → 启动
bash update.sh [-y] [--no-git]   # 可选 git pull → 重建镜像 → 滚动更新
```

## 架构与运行时要点

### 统一响应体与业务码（规范 1-3）

- 所有 HTTP 接口**恒返回状态码 200**，业务成败由统一响应体表达：`{code, success, message, data}`；`success == (code == 0)`（见 `common.schemas.common`）。
- 前端 `request.js` 拦截器：`success=true` 直接返回 `data`；失败弹出 `message` toast 并以「已处理」拒绝结束（前端不再二次包装）；`code=40100`（未登录/过期）清除令牌并跳转登录页。
- 业务码集中在 `backend/app/core/business_codes.py`：`0` 成功、`40000` 参数错误、`40001` 登录失败、`40100` 未登录/过期、`40300` 无权限、`40400` 不存在、`42601` 签名缺失、`42602` 外部依赖错误、`42610` 备份无效、`50000` 服务器错误（兜底，不向前端暴露堆栈）。
- 后端业务异常统一经 `app/core/errors.py` 注册的处理器转为 HTTP 200 + 失败响应体（`BusinessError` / `AuthError` / 参数校验 / 兜底 `Exception`）。

### 三个后端服务如何协作

- **backend**：唯一对外 API。负责鉴权、CRUD、在线聊天（含 `/api/v1/chat/ws` WebSocket 实时推送）、仪表盘等。**唯一执行建表/补字段/补字典启动迁移**（`common.db.init_database.SchemaMigrator`，只增不改不删、幂等）。
- **websocket**：按店铺维护拼多多长连接（`channel_pdd`），消息链路为 `PDDChannel 收包 → FIFO 队列（每店铺独立）→ MessageConsumer → decide_reply 决策链 → 发送回复/转人工/记日志/通知`。决策优先级（`engine/reply_engine.py`）：黑名单 → 过滤 → 非营业时间 → 风控 → 关键词 → 商品专属 → AI → 默认回复 → 无匹配。服务启动时自动拉起全部「启用」店铺连接；backend 经 HTTP（`INTERNAL_SERVICE_TOKEN` 校验）触发连接启停/发消息/登录等。
- **scheduler**：APScheduler 后台调度器，从 `scheduled_task` 表加载启用任务（`cron` / `interval` 两种方式），任务键：`cookie_refresh` / `product_sync` / `log_file_cleanup` / `tiktok_window`（TikTok 营业时间窗收敛，60s）/ `reply_rate_check`（TikTok 24h 回复率巡检，3600s，跌破 85% 企微告警，TIK-026）；内置任务由 backend `scheduled_task_service.DEFAULT_TASKS` 在访问任务列表时幂等补齐，执行体写 `task_run_log`。job 默认 `misfire_grace_time=3600`、`coalesce=True`、`max_instances=1`，时区 Asia/Shanghai。

### 数据访问与模型约定

- 全部 SQL 经 `common.db.repository.Repository`（`create/get/get_by/list/count/update/paginate/soft_delete/upsert`）参数化执行，业务层不写原生 SQL（规范 12/16）。
- 事务由 `common.db.session.get_db`（FastAPI 依赖）统一 commit/rollback；仓储层只 flush 不 commit（规范 36）。
- 模型统一继承 `common.models.base.Base` + `AuditMixin`（自增 BIGINT 主键 `id`、`created_at`/`updated_at` 北京时间、`created_by`）；**不使用外键约束**（关系由代码维护）；**禁止物理删除业务数据**（软删除经 `deleted_flag/status/enabled/is_active` 探测）；敏感字段（`password_hash/cookies_enc/...`）对外响应脱敏。

#### 向量混合检索（POL 引入，默认关）

- **`common.services.embedding_service`**（新建）：OpenAI 兼容 `/embeddings` 向量化，仅 stdlib urllib；纯函数（配置校验/文本规整/向量 JSON 编解码/`cosine_similarity` 零向量安全/`resolve_embedding_config` 回退 chat 配置）与网络 `embed_texts` 分离；异常文本不含密钥。
- **`common.services.kb_hybrid`**（新建）：`hybrid_rank` 纯函数——token 命中数与余弦各自 min-max 归一化后加权合并（默认权重 0.5），无 I/O。
- **`common.services.kb_indexing`**（新建）：`index_knowledge_content` best-effort 生成向量并 upsert（`content_hash` 失效检测），失败仅记 warning 绝不阻断知识库写操作；`is_embedding_enabled` / `build_content_hash` 纯函数。
- **`common.models.knowledge_models.KbEmbedding`**：表 `pdd_kb_embedding`（shop_pk / source_type / source_id / content_hash / model_name / vector_json Text / enabled），`UniqueConstraint(shop_pk, source_type, source_id)`，无外键——对齐 `pdd_product` 表 uix 先例。
- **`LlmConfig` 增列**：`embedding_model`(String 128 可空) / `embedding_enabled`(Boolean default False)；SchemaMigrator 自动补列，**无需注册、不改 init_database.py**（backend 唯一执行迁移）。
- **触发条件**（`kb_service.search`）：传入 query + 店铺 `embedding_enabled=True` + 本店启用记录有可用向量，三者同时满足才走向量路径；任一不满足逐字节回退原 jieba 路径（零行为变更）。`websocket/agent/tools.py` 零改动（升级对 Agent 循环透明）。
- **索引构建**：backend 知识库写路径 best-effort 挂钩（`cs_knowledge_service` / `product_knowledge_service`）+ `tools/kb_embedding/backfill.py` CLI（`--mode full/delta/check`）。

### 前端约定

- 路由由 `src/config/navigation.js` 菜单配置生成（菜单即路由单一数据源）；已实现页面在 `src/router/index.js` 的 `PAGE_COMPONENTS` 按菜单键映射，未实现项回退占位组件。
- axios 基址 `VITE_API_BASE_URL` 未配置时回退相对路径 `/api`（dev 由 Vite 代理、生产由 nginx 代理）；请求超时统一 90 秒；令牌存 localStorage 的 `auth_token`。
- 主题：暗黑模式 class 策略（`<html>.dark`），主题色经 CSS 变量 `--theme-primary-*` 注入 Tailwind `primary` 色板（`utils/theme.js` 切换）。

## 代码风格与开发规范（代码注释中引用「规范 N」，本清单为可核实的要点）

- **入口模式（规范 32/33）**：每个 Python 服务 `main.py` 仅做 sys.path 注入（服务目录 + 项目根目录）与拉起 `_bootstrap.py`；`pyproject.toml` 显式声明 `[tool.setuptools].packages`（避免「多个顶级目录」打包错误）；`common` 依赖由各服务经 sys.path 引用，不重复打包。
- **配置（规范 21）**：禁止在代码/编排中写死 `localhost`；所有地址与端口经环境变量管理，默认值采用 Docker 服务名（`mysql`/`redis`/`backend`/`websocket`/`scheduler`）。统一经 `common.core.config.get_settings()` 读取（pydantic-settings，支持根目录 `.env`，字段名与环境变量名大小写不敏感）。
- **时间（规范 17）**：全链路北京时间 `Asia/Shanghai`；时间工具统一用 `common.utils.time_utils`（`now_beijing` / `now_beijing_naive` / `format_beijing` 等）。
- **日志（规范 38/4）**：统一使用 info/warning/error，**禁用 debug 级别**（websocket 装配时即使误配 DEBUG 也提升为 INFO）；日志时间显示为北京时间。
- **结构与命名**：单文件 ≤500 行（规范 35）；Python 文件名用下划线（规范 40）；注释与提示文案全中文（规范 37/50）；导入置顶（规范 51）；复用既有组件，不做重复实现（规范 52）。
- **分页（规范 28）**：默认按时间字段倒序，`Repository.paginate` 返回 `{list, total, page, page_size}`。
- **幂等与重试（规范 13/14）**：数据库连接失败自动重试（`common.db.retry.with_db_retry`）；启动迁移幂等。
- **错误处理**：业务失败不要抛 HTTP 4xx/5xx，抛 `BusinessError`（携带业务码 + 中文 message）由统一处理器兜底为 HTTP 200 响应体。
- **代码注释约定**：每个模块文件头部有大段中文 docstring 说明「文件用途 / 关键约束 / 设计说明」，并常引用「规范 N / 需求 N.N / Property N」编号；新增文件请保持同一风格。

## 测试约定

- pytest + Hypothesis（属性测试）；测试文件命名 `test_*.py`；每个服务目录下 `tests/` 自带 `conftest.py`（把服务目录与仓库根目录加入 sys.path，保证 `import app.*` / `import common.*` 可用）。
- backend 的 conftest 用内存 SQLite + `@compiles(BigInteger, "sqlite")` 把 BIGINT 渲染为 INTEGER（否则 SQLite 自增主键失效）；不触发应用 lifespan 迁移。
- 纯逻辑组件（engine / agent / common 工具）设计为无 I/O，便于单元与属性测试。
- 运行入口：各服务目录下 `pytest`（见上文命令）。

## 部署与发布（Docker）

- `docker-compose.yml` 编排 6 个服务，依赖顺序经 `depends_on + condition: service_healthy` 保证（backend/websocket/scheduler 依赖 mysql、redis 健康；websocket/scheduler/frontend 还依赖 backend 健康，确保迁移先完成）。
- 各 Python 服务镜像统一 `python:3.11-slim`，构建期从各自 pyproject 提取依赖安装；`PYTHONPATH=/app` 使 `common` 可导入；健康检查探测 `/health`。
- 数据持久化于命名卷（`mysql_data` / `redis_data` / `backend_static` / `websocket_browser_data`），**禁止删除数据卷**（deploy.sh 用 `down` 不带 `-v`）。
- 前端为多阶段构建（node:18-alpine 构建 → nginx:alpine 运行）；`nginx.conf` 是模板，容器启动时 envsubst 将 `${BACKEND_API_URL}` 渲染为实际后端地址；`/api/v1/chat/ws` 单独配置长读超时（24h）以支撑在线聊天 WebSocket。
- 环境变量集中在根目录 `.env`（由 `.env.example` 复制，已 gitignore）；一键脚本（build/deploy/update）自动复制并统一携带 `--env-file`。

## 安全注意事项

- `.env` 含密钥与密码，已 gitignore，禁止提交；生产务必修改：`JWT_SECRET_KEY`（JWT 签名）、`DATA_ENCRYPT_KEY`（Fernet 派生密钥，加密 Cookie/账号密码）、`INTERNAL_SERVICE_TOKEN`（服务间内部接口鉴权）、`DEFAULT_ADMIN_PASSWORD`（初始超管，仅库中无用户时创建）。
- 密码仅存不可逆哈希（bcrypt_sha256，先 SHA-256 预哈希规避 bcrypt 72 字节截断）；Cookie 凭据用 Fernet 对称加密（`common.utils.crypto`），列表响应脱敏。
- JWT 载荷含 `jti`（令牌唯一 ID）与 `ver`（版本号）；登出令牌记入黑名单（`backend/app/core/token_blacklist.py`）实现主动失效。
- 所有 SQL 参数化（防注入）；上传/静态资源经 nginx 代理；内部接口须携带 `INTERNAL_SERVICE_TOKEN`。

## 环境变量速查（详见 `.env.example`）

- `MYSQL_*` / `REDIS_*`：数据库与缓存（host 为 Docker 服务名）
- `BACKEND_WEB_PORT` / `WEBSOCKET_PORT` / `SCHEDULER_PORT` / `FRONTEND_HOST_PORT`：端口
- `BACKEND_WEB_SERVICE_URL` / `WEBSOCKET_SERVICE_URL` / `SCHEDULER_SERVICE_URL` / `BACKEND_API_URL`：服务间地址
- `JWT_SECRET_KEY` / `DATA_ENCRYPT_KEY` / `INTERNAL_SERVICE_TOKEN`：安全密钥
- `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD`：初始超管
- `BROWSER_HEADLESS`（默认 false，账号密码登录需人工验证码）/ `MAX_CAPTCHA_CONCURRENT`（默认 1）/ `PLAYWRIGHT_USER_DATA_DIR`：Playwright 登录
- `LOG_LEVEL`：日志级别（默认 INFO）
- 前端：`VITE_DEV_PROXY_TARGET`（dev 代理目标，默认 http://127.0.0.1:8089）、`VITE_API_BASE_URL`（未配置时用相对路径 `/api`）

## 实测记录（2026-08-26，Windows 本地）

- Python 3.12.10 + 新建 `.venv`（仓库根目录，已 gitignore）安装四个服务 `[test]` 依赖后：
  - `common`：22 通过 / 1 失败（见下）
  - `backend`：207 通过（172s）
  - `websocket`：176 通过（49s）
  - `scheduler`：17 通过
- **已知环境坑 1（Windows）**：`zoneinfo` 在 Windows 无系统时区库，直接运行测试会报 `ZoneInfoNotFoundError`；需 `pip install tzdata`（Linux/macOS/Docker 无此问题，pyproject 未声明该依赖）。
- **已知环境坑 2（依赖版本）**：`passlib[bcrypt]>=1.7.4` 未约束 bcrypt 上界，新装会拉到 bcrypt≥4.1（实测 5.0.0），导致 passlib 1.7.4 的 `bcrypt_sha256` 失效（`module 'bcrypt' has no attribute '__about__'`，随后报「password cannot be longer than 72 bytes」）。实测 `pip install bcrypt==4.0.1` 后 common 测试全部通过。建议在 `common/pyproject.toml` 中为 bcrypt 加上界（如 `bcrypt<4.1`），或升级 passlib。
- 前端：Node 24 + `npm ci --no-audit --no-fund` + `npm run build` 实测成功（`✓ built in 36.04s`，产物在 `frontend/dist/`）。

### 2026-09-01 基线（POL 打磨工单推进前核实，回归基线）

四服务 + tools 全量测试，内存 SQLite + mock 网络，共 **749** 例全绿：

| 组件 | 通过 |
| --- | --- |
| common | 63 |
| backend | 253 |
| websocket | 352 |
| scheduler | 52 |
| tools | 29 |
| **合计** | **749** |

POL 批次（RAG 补强 / 评测模块 / 文档收口）落地后全量回归：**829 例全绿**
（common 98 / backend 257 / websocket 352 / scheduler 52 / tools 70），
较 POL 前基线 749 新增 80 例（common +35 / backend +4 / tools +41）。
