# 风控规则配置指引（频率断路器 / RiskRule）

> 工单：[TICKETS_TIKTOK.md](./TICKETS_TIKTOK.md) TIK-020「频率断路器配置化」
> 适用平台：PDD 与 TikTok 通用（风控判定与平台无关，只按店铺主键 `shop.id` 生效）。
> 首次落地：TikTok 泰国站测试店（`shop_pk=1`，店铺名 `18023103936`），2026-08-29 实测通过。

## 1. 它解决什么问题

自动回复引擎在异常场景（买家刷屏、会话循环触发、规则误配导致的连环回复）下可能在短时间内向同一店铺/同一买家发出大量消息，存在被平台风控与打扰买家的风险。风控规则就是这道「频率断路器」：在统计窗口内的回复次数达到上限后，**暂停自动回复并落一条风控日志**，宁可少回也不失控。

## 2. 字段与判定口径

规则表 `pdd_risk_rule`，按店铺主键 `shop_pk`（即 `shop.id`）upsert，**同一店铺只保留一条**。

| 字段 | 含义 | 为空时 |
| --- | --- | --- |
| `session_reply_limit` | 单会话（单个买家）窗口内回复次数上限 | 该维度不限制 |
| `shop_reply_limit` | 单店铺（全店合计）窗口内回复次数上限 | 该维度不限制 |
| `window_seconds` | 统计窗口秒数 | 不按时间截断，统计全部历史回复 |
| `enabled` | 该规则是否启用 | ——（默认 true） |

判定口径（`websocket/engine/risk_control.py`，决策链第 4 级）：

- 统计窗口为 `(now - window_seconds, now]`，窗口外的旧回复自动失效，滑出后即可恢复回复；
- 达上限语义是「次数 **≥** 上限」：窗口内已回满 N 条时，第 N+1 条被暂停；
- 判定顺序**先单会话、后单店铺**，任一维度达上限即暂停，只生成一条风控日志；
- 命中后：不发送回复、消息日志记为 `risk_paused`、写一条 `pdd_risk_log`（`risk_type=frequency_limit`）、并推送 `risk_triggered` 系统事件（店铺已配企微渠道时进群）；
- 优先级在风控之前还有三道闸：**黑名单 → 过滤规则 → 非营业时间**，它们命中时不会走到风控。

## 3. 配置方式（三选一）

### 方式 A：管理端（推荐，人工操作）

店铺管理 → 选中店铺 → 「店铺设置」 → **风控管理** 标签页 → 填写上限与统计窗口 → 保存。

- 接口：`PUT /api/v1/shops/{shop_pk}/risk-rule`（幂等 upsert，重复保存为覆盖更新）；
- 权限：归「店铺管理」资源判权，有店铺管理权限即可；非管理员仅能操作本人（或被授权）店铺；
- 未配置时查询接口返回 `data=null`，前端显示为空表单。

### 方式 B：SQL（批量配置 / 无人值守场景）

```sql
-- 新建（MySQL 为例）
INSERT INTO pdd_risk_rule
    (shop_pk, session_reply_limit, shop_reply_limit, window_seconds,
     enabled, created_by, created_at, updated_at)
VALUES
    (1, NULL, 20, 3600, 1, 1, NOW(), NOW());

-- 已有规则则改为更新（同一店铺只保留一条）
UPDATE pdd_risk_rule
   SET shop_reply_limit = 20, window_seconds = 3600, enabled = 1, updated_at = NOW()
 WHERE shop_pk = 1;

-- 查看当前配置
SELECT id, shop_pk, session_reply_limit, shop_reply_limit, window_seconds, enabled
  FROM pdd_risk_rule;
```

> 无 alembic 迁移、无 seed 脚本：表由 backend 启动自检迁移器（`common/db/init_database.py`）自动创建，无需手工建表。

### 方式 C：演练 CLI（配置 + 实测一步到位）

```bash
# 在仓库根目录，使用 .venv
./.venv/Scripts/python tools/tiktok_acceptance/risk_rule_drill.py --shop-pk 1

# 自定义演练上限 / 固化取值
./.venv/Scripts/python tools/tiktok_acceptance/risk_rule_drill.py \
    --shop-pk 1 --drill-limit 3 --window 3600 --final-limit 20
```

脚本会依次验证「不限流阶段全部照常回复、限流阶段超上限即暂停并落库风控日志」，收尾把规则固化为 `--final-limit` 指定值。发送器与聊天记录均为内存桩，**不会真的向买家发消息**。

## 4. 当前生效配置

| 店铺 | 单会话上限 | 单店铺上限 | 统计窗口 | 启用 | 配置时间 |
| --- | --- | --- | --- | --- | --- |
| `shop_pk=1`（TikTok 测试店） | 不限制 | 20 条 | 3600 秒 | 是 | 2026-08-29 |

## 5. 验证方法

1. **演练 CLI**：两阶段结论均输出「符合预期」，退出码 0；
2. **查风控日志**（风控日志属审计数据，不做物理删除）：

   ```sql
   SELECT id, shop_pk, risk_type, log_time, trigger_reason
     FROM pdd_risk_log
    WHERE risk_type = 'frequency_limit'
    ORDER BY id DESC LIMIT 20;
   ```

   触发原因形如：`单店铺3600秒统计窗口内回复次数已达上限（20/20），暂停自动回复`；

3. **管理端**：「风控日志」页（`frontend/src/pages/risk_logs.vue`）按风控类型筛选 `frequency_limit`；
4. **单元测试**：`cd websocket && ../.venv/Scripts/python -m pytest tests/test_risk_control.py -q`（17 个）。

## 6. 取值建议

| 阶段 | 建议配置 | 说明 |
| --- | --- | --- |
| 测试 / 联调 | 不配置（恒放行） | 避免干扰功能验证；未配置与 `enabled=false` 行为等价 |
| 首店灰度（TIK-024） | 单店铺 20 条 / 3600 秒 | 正常咨询不会误伤，可挡住异常刷屏与循环回复 |
| 稳定运行后 | 单店铺 20~30 条 / 3600 秒，可选叠加单会话 3~5 条 | 叠加会话维度可更精准地防单买家刷屏 |

## 7. 注意事项

1. **计数是内存态**：回复时刻记在 websocket 进程内存里，进程重启后清零，重启后窗口内计数从 0 重新开始（极端情况下重启可"绕过"上限，属已知取舍）；
2. **窗口是全店共享**：`shop_reply_limit` 统计该店铺全部会话的回复，配得太小会让后面的买家被前面的买家"挤掉"，灰度期建议先只配店铺维度且取值宽松；
3. **暂停 ≠ 丢弃**：被风控暂停的消息仍会落消息日志（`process_result=risk_paused`），人工可在消息日志中回看并手动补回；
4. **未配置 = 不限制**：表里没有该店铺记录时 `risk_enabled` 为 false，行为与 `enabled=false` 一致，对存量店铺零影响；
5. **规则按 `shop_pk` 关联**：表内是普通列无外键，删除店铺不会级联清理规则行，换店时需按新 `shop_pk` 重新配置；
6. **不要为压测临时把上限改小后忘记改回**：演练 CLI 收尾会自动固化 `--final-limit`，手工 SQL 调整时请同步更新本文件第 4 节。
