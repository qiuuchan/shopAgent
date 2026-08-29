# TIK-018 端到端验收工具与演练清单

> 配套计划：[PLAN_TIKTOK.md](../../PLAN_TIKTOK.md) §10「关键验收」｜工单：[TICKETS_TIKTOK.md](../../TICKETS_TIKTOK.md) TIK-018
> 本目录为 TIK-018 的验收工具（代码侧已就绪，不依赖测试店账号即可先行验证逻辑）。

## 1. 工具组成

| 文件 | 用途 |
| --- | --- |
| `reconcile.py` | 对账 CLI：按店铺 + 时间窗口统计收发消息与首响时长，供与 seller center 人工对账 |
| `first_response_drill.py` | 首响统计验收演练 CLI（TIK-025）：真实库 + 真实 backend 统计服务跑一遍，并与 `reconcile.py` 逐项抽查口径一致性（详见 §7） |
| `risk_rule_drill.py` | 风控频率断路器配置演练 CLI（TIK-020）：真实库配规则 + 真实决策链驱动，验证超限暂停与风控日志落库，配置指引见 [RISK_RULE_GUIDE.md](../../RISK_RULE_GUIDE.md) |
| `login_expiry.py` | 登录态过期观测 CLI（TIK-023）：统计 `login_expired` 事件间隔、推算建议巡检周期、核对巡检覆盖率 |
| `tests/`（`../tests/`） | 纯函数单测 + SQLite 内存库集成测试（16 个 + TIK-020/TIK-023 新增） |

## 2. 运行方式

使用仓库根目录 `.venv`（common 已 editable 安装），在**仓库根目录**执行：

```bash
# 默认统计最近 7 天（北京时间）
./.venv/Scripts/python tools/tiktok_acceptance/reconcile.py --shop 3

# 指定窗口 + JSON 全量输出
./.venv/Scripts/python tools/tiktok_acceptance/reconcile.py --shop 3 \
    --since 2026-08-30T00:00:00 --until 2026-08-31T23:59:59 --json

# 只看单个会话
./.venv/Scripts/python tools/tiktok_acceptance/reconcile.py --shop 3 --customer 买家A
```

- 数据源默认走系统配置（`common.db.session`，MySQL，读取 `.env` 的 `DATABASE_URL`）；
- 本地无 MySQL 时可用 `--db sqlite:///xxx.db` 验证（需先用测试 conftest 的方言适配建表，见 `tools/tests/conftest.py`）；
- 时间口径：`msg_time` 统一北京时间（规范 17），`--since/--until` 按北京时间解析，不做时区换算。

自测：`cd tools && ../.venv/Scripts/python -m pytest tests/ -q`

风控演练（TIK-020，需真实 MySQL 且店铺处于营业时间内）：

```bash
./.venv/Scripts/python tools/tiktok_acceptance/risk_rule_drill.py --shop-pk 1
```

登录态过期周期观测（TIK-023，默认拉最近 30 天）：

```bash
./.venv/Scripts/python tools/tiktok_acceptance/login_expiry.py --shop 3
./.venv/Scripts/python tools/tiktok_acceptance/login_expiry.py --shop 3 --days 14 --json
./.venv/Scripts/python tools/tiktok_acceptance/login_expiry.py --shop 3 \
    --since 2026-08-29T00:00:00 --until 2026-09-12T23:59:59
```

## 3. 验收前置条件（周末窗口实测前确认）

1. 测试店账号已人工登录一次（user-data-dir 登录态有效，二次免登通过）；
2. 生产 `.env`：`TIKTOK_SHOP_ENABLED=true`，其余 `TIKTOK_*` 按需调整；
3. 管理端 `/admin/scheduled-tasks` 存在启用中的 `tiktok_window` 任务（backend 启动后按内置种子幂等补齐，页面仅支持编辑/启停；确认 schedule_config=60、enabled=true）；
4. 店铺已启用企微通知渠道（`notify` 配置），企微群机器人可收到消息（**2026-08-28 已配通**：notify_channel id=1 wecom/店铺1，实测测试发送与 `connection_disconnected` 事件均 success 且企微实收；企微新版群机器人入口名为「消息推送」，需管理员后台「应用管理 → 消息推送」开通创建权限）；
5. 营业时间为周末实际值守窗口（BusinessHoursPanel 已配 weekdays，TIK-006/007 交付）。

## 4. 验收标准逐条演练

### 验收 1：非工作时间买家新消息 5 分钟内收到模板首响

**前置**：周末无人值守窗口开始前，确认店铺状态为已连接（websocket 状态接口 / 管理端显示在线）。

**步骤**：
1. 周末用买家账号向测试店发一条新消息（或等真实买家消息）；
2. 记录发出时刻；在平台后台「客户消息」确认消息到达；
3. 等待系统自动回复（预期模板首响，仅合规引导话术）；
4. 实测后运行对账 CLI，量化首响：
   ```bash
   ./.venv/Scripts/python tools/tiktok_acceptance/reconcile.py --shop <pk> --since <周末起> --until <周末止>
   ```
   查看【会话明细】该会话的首响秒数与【首响时长汇总】`超 5 分钟` 计数。

**通过判定**：该会话首响 ≤ 300 秒；`over_threshold_count=0`（或逐条人工确认超时原因）。

### 验收 2：消息日志与平台后台一致（可对账）

**步骤**：
1. 运行对账 CLI 取【每日收发统计】；
2. 登录 seller center「客户消息」页，人工比对同一天收发计数与逐会话条数（半自动抽查，口径：本系统只统计消息，平台侧含系统消息时以「客户消息」为准）；
3. 不一致时用 `--customer` 定位到会话，检查 `pdd_message_log` 处理结果。

**通过判定**：抽样会话计数一致；不一致项均有明确原因（如平台侧系统提示不计入）。

### 验收 3：人为杀掉浏览器/清除登录态 → 企微立即收到对应告警（告警演练清单）

**演练 A —— 连接断开（connection_disconnected）**：
1. 确认店铺在线，记下当前时刻；
2. 杀掉浏览器进程（容器内：`pkill -f chromium`；Windows 本机：任务管理器结束 chrome 子进程，注意只杀测试店 user-data-dir 对应实例）；
3. 预期：数秒内企微收到 `connection_disconnected` 告警（内容含店铺名）；
4. 复核落库：查 `pdd_notify_record`（event_type=`connection_disconnected`，send_result=成功）。

**演练 B —— 登录态失效（login_expired）**：
1. 删除/改名测试店 user-data-dir（`websocket_browser_data/tiktok_<shop_pk>`，先停 websocket 容器再操作，避免写冲突）；
2. 重启 websocket 服务（容器重建或 `start_enabled_channels` 重跑）；
3. 预期：通道检测到跳登录页，企微收到 `login_expired` 告警；
4. 复核落库：`pdd_notify_record`（event_type=`login_expired`）。

**通过判定**：两类事件均在 1 分钟内到达企微且 `notify_record` 有对应成功记录。

### 验收 4：告警静默窗口（默认 30 分钟）内不重复轰炸，恢复后可再告警

**步骤**：
1. 完成验收 3 演练 A 后，保持故障状态，再等 5 分钟确认**没有**第二条同事件告警；
2. 恢复：重启 websocket / 重新连接使通道回到连接态（触发 `AlertDedup.resolve`）；
3. 再次杀掉浏览器 → 预期能再次收到告警（静默窗口已重置）；
4. 复核落库：`pdd_notify_record` 中同事件类型在首次告警后 30 分钟内仅 1 条。

**通过判定**：静默窗口内同事件不重复推送；恢复后再次故障可再告警。

### 验收 5：营业时间窗口外连接不建立（scheduler 期望态 + TikTokChannel 第二道闸双验证）

**步骤**：
1. 把测试店营业时间临时改为「已过窗口」（或等自然进入窗口外），等待一个 scheduler 周期（interval 60s）；
2. 验证第一道闸：scheduler `tiktok_window` 任务的 `task_run_log` 显示已调 `disconnect`（期望断开）；
3. 验证第二道闸：websocket 状态接口显示 `DISCONNECTED`；窗口外重启 websocket 服务后 `start_enabled_channels` 不启动浏览器（TikTokChannel.start 的 respect_business_hours 拦截）；
4. 复核：窗口外无新浏览器进程（`ps`/容器内查 chromium 实例数）；
5. 改回营业时间，确认一个周期内自动 `connect` 恢复在线。

**通过判定**：窗口外连接不建立；窗口内自动恢复，与营业时间一致（含 weekdays 维度）。

## 5. 常见问题

- **对账数字对不上**：先确认窗口口径（北京时间）与店铺 pk；再确认 `TIKTOK_SHOP_ENABLED` 与店铺启用状态（未启用不连不落库）；
- **告警没收到**：查 `pdd_notify_record` 是否落库（落库 success 未达企微 = 渠道配置/网络问题，复查 webhook key 是否失效——企微对失效 key 返回 errcode≠0，系统按业务错误记 failed；未落库 = 事件未触发，检查通道状态与 AlertDedup 静默窗口）；
- **`--db` sqlite 验证**：sqlite 下 BigInteger 不自增，需先用 `tools/tests/conftest.py` 的方言适配建表再插数；
- **周末窗口对账**：验收 1/2 建议用 `--json` 输出留存，作为验收记录附件。
- **巡检打点全是 `channel_absent`**：该取值表示「无活跃页面」（店铺未连接或在营业时间窗外），属正常期望态而非故障；若确认店铺应在线却长期 `channel_absent`，查通道是否真的启动（websocket 状态接口）以及营业时间 weekdays 配置，详见 §6.1。

## 6. TIK-023 登录态过期周期观测

> 工单：[TICKETS_TIKTOK.md](../../TICKETS_TIKTOK.md) TIK-023（批次 E2）｜验收：① 观测记录入档；② cookie_refresh 周期按结论配置；③ 一次真实 `login_expired` → 人工重登 → 自动恢复演练。

### 6.1 观测原理：数据从哪来

TikTok 登录态常驻浏览器 user-data-dir（`websocket_browser_data/tiktok_<shop_pk>`），**不入库、也没有任何「登录生效时刻」字段**，因此光看告警记录只有「已过期」的时间点，推不出「登录 → 过期」的时长。TIK-023 补上两个数据源：

| 数据源 | 落库位置 | 提供什么 |
| --- | --- | --- |
| 登录态巡检打点 | `pdd_task_run_log`（`task_key='cookie_refresh'`） | 每周期对 TikTok 店铺做一次**只读**登录态探测，异常时把明细追加进 message（`店铺[xx] 巡检异常：中文说明`） |
| 过期告警事件 | `pdd_notify_record`（`event_type='login_expired'`） | 过期发生的时间点，相邻间隔即周期样本 |

巡检链路：`scheduler.run_cookie_refresh` → HTTP → `websocket POST /api/v1/cookies/refresh` → TikTok 分支跳过 PDD 刷新、改为调 `TikTokChannel.probe_login_state()`（与监控循环同口径，但**只判定不处置**：不置状态、不发告警、不重开页面）→ 结果随 `data.login_probe` 回传。

`login_probe` 取值（服务间契约，改动须同步 websocket 与 scheduler 两侧）：

| 取值 | 含义 | 是否记入执行日志 |
| --- | --- | --- |
| `ok` | 登录态有效 | 否（常态不打点，避免刷屏） |
| `login_expired` | 主站跳登录页 | 是 |
| `im_expired` | IM 会话过期弹窗（主站未跳转） | 是 |
| `page_dead` | 页面存活探测失败（浏览器已退出） | 是 |
| `channel_absent` | 无活跃页面（未连接 / 营业时间窗外），**非故障** | 是 |
| `unknown` | 探测异常，不误报 | 是 |

### 6.2 观测跑法与频率

- **观测期**：数日 ~ 数周（TikTok 登录态过期周期未知，短窗口取不到样本）；
- **跑法**：观测期内每 3~7 天跑一次本 CLI 跟进，收尾时用 `--since/--until` 一次性拉全窗口；
- **留存**：每次用 `--json` 输出归档，作为验收①的观测记录附件；
- **当前巡检周期**：`cookie_refresh` = **600 秒**（10 分钟），观测期内维持不动。

### 6.3 结论判读与口径限制

读报告时务必带上这三条限制，否则结论会偏乐观：

1. **告警次数可能少于真实发生次数**：`AlertDedup` 静默窗口（默认 30 分钟）会压制重复告警，据此算出的过期间隔是**上界**而非精确值；
2. **样本 <3 次过期事件不出周期结论**（即 <2 个间隔），CLI 只给保守建议值 1800 秒；
3. **巡检覆盖率 <80% 时结论不可信**：说明窗口不完整（服务重启 / 调度未启用 / misfire 丢弃），需补齐观测再下结论。

交叉校验：报告里的「巡检异常打点条数」× 巡检周期 ≈ 累计失效时长；该数通常**多于**告警事件数（告警受静默去重压制），若反而少于，需核查告警链路。

### 6.4 cookie_refresh 周期调参规则

**建议周期算法**（CLI 已实现，见 `login_expiry.suggest_probe_interval`）：

```
建议周期 = clamp(ceil_to_minute(最短观测间隔 / 4), 下界 600s, 上界 21600s)
```

除以 4 是为最短间隔留出采样冗余，避免巡检与过期整周期错配而漏采。

**⚠ 调参前必读：cookie_refresh 是 PDD / TikTok 共用任务**

`run_cookie_refresh` 遍历全部启用店铺，周期对两个平台同时生效。调大周期会**同步降低 PDD 侧 Cookie 保活频率**，违反「对 PDD 现有路径零行为变更」的出口准则。因此决策规则如下：

| 观测结论 | 处置 |
| --- | --- |
| 无过期事件 / 样本不足（<3 次） | **不动**，维持 600 秒 |
| 最短间隔 ≤ 40 分钟（建议值算出 ≤600s） | **不动**：已触及下界，再密无收益 |
| 建议值 > 600 秒 | **需权衡**：先确认 PDD 侧可接受该保活频率；若不可接受则**维持 600 秒不动**（10 分钟巡检对「天」量级的过期周期本就绰绰有余） |

**改法**（改完需重启 scheduler 或等下次调度重载）：

```bash
# 方式一：管理端 /admin/scheduled-tasks 改 cookie_refresh 的调度配置（秒）
# 方式二：直改库（注意：backend 内置任务种子仅在 task_key 不存在时补齐，不会覆盖已有配置）
UPDATE pdd_scheduled_task SET schedule_config = '1800' WHERE task_key = 'cookie_refresh';
```

### 6.5 恢复演练清单（验收③）

演练 `login_expired` → 人工重登 → `login_recovery` 自动接管（默认 60s × 10 次探测，共 10 分钟窗口）。**需人工配合，择机执行**。

**步骤**：

1. 确认测试店在线、企微可收告警，记下当前时刻 T0；
2. 停 websocket 服务，把 user-data-dir 改名（`websocket_browser_data/tiktok_<shop_pk>` → `.bak`），重启服务；
3. 预期 T0 后 1 个监控周期内：企微收到 `login_expired` 告警，通道置 `DISCONNECTED`；
4. **不要立刻重登**：等待 2~3 分钟，确认监控循环进入恢复探测（`login_recovery` 每 60s 探一次），且**无重复告警轰炸**（静默窗口 30 分钟）；
5. 人工在浏览器窗口重登（过验证码），完成后**不做任何系统操作**；
6. 预期：下一个 60s 探测点检测到登录态恢复 → 自动重开聊天页 → 续接监控，通道回到连接态；
7. 用买家账号发一条消息，确认自动回复恢复（首响 ≤300s）；
8. 复核落库与日志：
   ```bash
   ./.venv/Scripts/python tools/tiktok_acceptance/login_expiry.py --shop <pk> \
       --since <T0> --until <T0+1h> --json
   ./.venv/Scripts/python tools/tiktok_acceptance/reconcile.py --shop <pk> --since <T0>
   ```

**通过判定**：① `login_expired` 告警 1 分钟内到达企微且 `notify_record` 落库成功；② 静默窗口内无重复告警；③ 人工重登后 60~120 秒内通道自动恢复连接、无需重启服务；④ 恢复后消息自动回复正常。

**归档模板**（演练后回填，作为验收③记录）：

```
演练日期/时刻：
店铺 shop_pk / shop_id：
T0（置故障）／T1（告警到达企微）／T2（人工重登完成）／T3（通道自动恢复）：
告警延迟 T1-T0：      静默窗口内重复告警条数：
自动恢复耗时 T3-T2：   恢复后首响：
login_recovery 是否接管（是/否）：  恢复后是否需人工重启服务（是/否）：
结论（通过/不通过）：  偏差说明：
```

---

## 7. TIK-025 首响统计验收演练（first_response_drill.py）

> 工单：[TICKETS_TIKTOK.md](../../TICKETS_TIKTOK.md) TIK-025 ｜ 统计口径：`common/utils/latency.py`
> （原 `latency.py` 已于 TIK-025 上移 common，对账工具、统计接口、跌破阈值告警三处共用同一实现）

### 7.1 演练做什么

用**真实库 + 真实 backend 统计服务**（`app.services.first_response_service`，与
`GET /api/v1/dashboard/first-response` 同一实现）跑一遍首响统计，再用 `reconcile.py`
对**同窗口同店铺**重算一遍，逐项抽查两者口径是否一致，并输出分布 / 超 5 分钟占比 /
回复率与分店铺明细供人工核对 dashboard。

**隔离边界**：只读（`pdd_chat_message` / `pdd_shop` / `sys_user` / `sys_role`），
不写任何业务表、不触发发送与通知、不拉浏览器。统计以管理员身份执行（避开数据范围
隔离干扰；隔离本身由 `backend/tests/test_first_response_api.py` 覆盖）。

### 7.2 跑法

```bash
# 默认最近 7 天
./.venv/Scripts/python tools/tiktok_acceptance/first_response_drill.py --shop 1

# 指定窗口 + 平台筛选
./.venv/Scripts/python tools/tiktok_acceptance/first_response_drill.py --shop 1 \
    --since 2026-08-23 --until 2026-08-29 --platform tiktok

# JSON 全量输出（便于归档）
./.venv/Scripts/python tools/tiktok_acceptance/first_response_drill.py --shop 1 --days 1 --json
```

退出码：`0` = 8 项口径抽查全部一致；`1` = 存在不一致或查询失败（无管理员用户 /
店铺不存在 / 平台与店铺不匹配等）。

> `--since/--until` 支持 `YYYY-MM-DD`（自动补齐 00:00:00 / 23:59:59）与 ISO 日期时间。

### 7.3 口径抽查项与判读

抽查 8 项：`responded_cycles` / `pending_cycles` / `pending_conversations` /
`over_threshold_count` / `over_threshold_ratio` / `mean_seconds` / `p50_seconds` /
`p90_seconds`。任一项不一致即退出码 1，并在报告 `mismatched_fields` 中列出。

比对前提：两者窗口须一致——backend 按自然日 `[start 00:00, end+1d 00:00)`，
`reconcile` 按 `[since, until]`（含端点）。演练 CLI 已统一为自然日边界，直接用
`--since/--until` 传同一组日期即可，无需手工换算。

### 7.4 回复率口径（TIK-026 告警同源）

`回复率 =（已回复周期 − 超时周期）/（已回复周期 + 待回复周期）`：

- 首响 >300 秒（5 分钟标准）计**超时**；窗口结束仍无回复计**待回复**；
- 超时与待回复**均计未达标**，待回复计入分母；
- 无任何周期时返回 `None`（无数据，不判 0 也不判 1）。

该口径实现于 `common.utils.latency.reply_rate`，TIK-026「24h 回复率跌破 85% 告警」
复用同一函数，保证看板与告警判定同源。
