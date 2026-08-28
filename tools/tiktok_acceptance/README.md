# TIK-018 端到端验收工具与演练清单

> 配套计划：[PLAN_TIKTOK.md](../../PLAN_TIKTOK.md) §10「关键验收」｜工单：[TICKETS_TIKTOK.md](../../TICKETS_TIKTOK.md) TIK-018
> 本目录为 TIK-018 的验收工具（代码侧已就绪，不依赖测试店账号即可先行验证逻辑）。

## 1. 工具组成

| 文件 | 用途 |
| --- | --- |
| `latency.py` | 首响时长统计纯函数（不依赖 common，可单测）：回复周期切分、>300s 超时判定 |
| `reconcile.py` | 对账 CLI：按店铺 + 时间窗口统计收发消息与首响时长，供与 seller center 人工对账 |
| `tests/`（`../tests/`） | 纯函数单测 + SQLite 内存库集成测试（16 个） |

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

## 3. 验收前置条件（周末窗口实测前确认）

1. 测试店账号已人工登录一次（user-data-dir 登录态有效，二次免登通过）；
2. 生产 `.env`：`TIKTOK_SHOP_ENABLED=true`，其余 `TIKTOK_*` 按需调整；
3. 管理端已建 `scheduled_task` 记录启用 `tiktok_window`（scheduler 无自动种子，须手工建）；
4. 店铺已启用企微通知渠道（`notify` 配置），企微群机器人可收到消息；
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
- **告警没收到**：查 `pdd_notify_record` 是否落库（落库未达企微 = 渠道配置问题；未落库 = 事件未触发，检查通道状态与 AlertDedup 静默窗口）；
- **`--db` sqlite 验证**：sqlite 下 BigInteger 不自增，需先用 `tools/tests/conftest.py` 的方言适配建表再插数；
- **周末窗口对账**：验收 1/2 建议用 `--json` 输出留存，作为验收记录附件。
