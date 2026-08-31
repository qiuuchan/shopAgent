# TIK-018 周末验收 Checklist（用户侧操作清单）

> 使用方式：逐项勾选，全部完成后跑「对账」一节命令。
> 详细演练步骤见同目录 [README.md](README.md)；代码侧全部就绪，本清单只含运行时操作。
> 验收窗口：2026-08-29 ~ 2026-08-31（周末，北京时间）。

## A. 前置准备（周五晚 / 窗口开始前）

- [x] A1. 建测试店：管理端新增店铺，平台选 **TikTok Shop**，账密登录一次（人工过验证码）
      - 2026-08-28 已实测完成：账号 18023103936 登录成功（免登复用），店铺 id=1 落库
      - 登录态目录：`websocket/browser_data/tiktok_367516054`（已落库 Shop.browser_data_dir，连接自动复用）
      - 说明：首次登录 TikTok 强制短信验证码（平台风控），登录成功后目录持久化免登
- [x] A2. 配置企微告警（**2026-08-28 已配通**）：
      - 群机器人 webhook（企微新版入口名「消息推送」，管理员后台「应用管理 → 消息推送」开通后可见）
      - 已创建 `notify_channel` id=1（wecom，店铺 1，enabled）并实测：测试发送 + `connection_disconnected`
        事件均 success 且企微群实收（errcode=0）；`send_via_channel` 已按企微 msgtype/text 协议适配并校验 errcode
- [x] A3. 确认 `tiktok_window` 定时任务存在且启用（backend 启动时按内置种子自动补齐，管理端 `/admin/scheduled-tasks` 检查即可）：
      - task_key = `tiktok_window`，schedule_type = `interval`，schedule_config = `60`（秒），enabled = true
      - 若缺失：确认 backend 已启动并访问过一次任务列表页（触发幂等补齐）
      - 2026-08-28 已实测：列表返回 `tiktok_window interval 60 enabled=True`，scheduler 已注册并执行（run-log: success「无启用 TikTok 店铺，跳过时间窗控制」）
- [ ] A4. 营业时间配周末值守窗口 + 「生效星期」勾选周六/周日（BusinessHoursPanel）——**待用户提供值守时间**
- [x] A5. 确认 `.env`（已配好，重启服务生效）：`TIKTOK_SHOP_ENABLED=true`，其余 `TIKTOK_*` 按需
- [x] A6. 重启 backend / websocket / scheduler 服务（2026-08-28 已全部启动并健康）：
      - backend :8089 ✅ / websocket :8090 ✅ / scheduler :8091 ✅（重启后注册 4 任务）/ frontend :9100 ✅
- [x] A7. 确认店铺状态为在线（2026-08-28 已实测：connected=True，TikTok 通道启动、聊天页已打开、监控循环运行）

## B. 周末实测（窗口内）

- [ ] B1. 买家账号发一条真实新消息 → 5 分钟内收到模板首响（记录发出时刻）
- [ ] B2. 顺带完成 spike 补测（s2/s3/s4，需真实买家消息）：
      - 会话列表项/输入框/发送按钮选择器稳定性
      - 收消息协议（WS 监听 vs DOM 轮询）定论
      - 发送链路成功率与端到端耗时
      - 结论回填 `websocket/channel_tiktok/selectors.py`
- [ ] B3. 人工抽查：对账 CLI 数字与 seller center「客户消息」页一致

## C. 告警演练（窗口内，选一个非值守时段）

- [ ] C1. 演练 A（connection_disconnected）：杀掉浏览器进程 → 企微群收到 `connection_disconnected` 告警，且 `pdd_notify_record` 落库 success
- [ ] C2. 演练 B（login_expired）：停服务 → 删除/改名 `websocket_browser_data/tiktok_<pk>` → 重启服务 → `login_expired` 事件落库
- [ ] C3. 静默窗口验证：保持故障 5 分钟无第二条同事件记录；恢复后再次故障可再告警
- [ ] C4. 复核落库：`pdd_notify_record` 有对应事件 success 记录，且企微群实收（渠道已配通）

## D. 营业时间窗口验证

- [ ] D1. 窗口外连接不建立：scheduler `task_run_log` 显示已 disconnect；状态接口 DISCONNECTED；无新浏览器进程
- [ ] D2. 窗口内自动恢复：一个周期（60s）内自动 connect 回在线

## E. 对账（验收收尾）

- [ ] E1. 跑对账 CLI，留存 `--json` 输出作为验收记录：
      ```bash
      ./.venv/Scripts/python tools/tiktok_acceptance/reconcile.py --shop <pk> \
          --since 2026-08-29T00:00:00 --until 2026-08-31T23:59:59 --json
      ```
- [ ] E2. 通过判定：`over_threshold_count = 0`（首响均 ≤ 300s）；不一致项均有明确原因

## F. 验收通过后 → 生产前（Phase 2 前置，可后置安排）

- [ ] F1. 提供代理 IP（4 店防关联），填 `Shop.proxy_server`（支持 http(s):// 与 socks5(5h)://）
- [ ] F2. 追加 2~3 家测试店（灰度 1 家 → 4 家）
- [x] F3. ~~websocket 容器内存限额调高（预留 4×1GB）、`websocket_browser_data` 卷扩容~~ —— **已随 TIK-027（扩量至 4 店）取消（2026-08-29 口径调整：仅监督 1 店）**。单店口径：`mem_limit: 2g`（满足 1×1GB 预留 + PDD/系统余量）、卷内单店 `tiktok_{shop_pk}` 子目录，TIK-022 已核对
- [ ] F4. 确认企微群长期有效（告警投递目标）

## 常见问题速查

| 现象 | 排查 |
| --- | --- |
| 没自动回复 | 店铺是否启用 + 营业时间窗口内 + 通道在线；查 `pdd_message_log` 是否落库 |
| 告警没收到 | `pdd_notify_record` 落库未达企微 = 渠道配置问题；未落库 = 事件未触发/静默窗口 |
| 对账对不上 | 窗口口径（北京时间）与店铺 pk；`TIKTOK_SHOP_ENABLED` 与店铺启用状态 |
| 窗口外浏览器还在 | `tiktok_window` 任务是否启用；BusinessHours 是否配了 weekdays |
