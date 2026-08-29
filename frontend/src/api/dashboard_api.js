// 仪表盘与数据分析接口模块（对应 backend /dashboard 路由，需求 20）
// 职责：封装仪表盘关键指标与数据分析趋势的后端接口调用，统一经 @/utils/request 发起，
//       请求成功后由请求封装直接 resolve 后端 data 部分（统一响应体已在拦截器处理）。
// 说明：后端地址经环境变量配置（禁止写死 localhost，规范 21 / 需求 25.4）。
import { get } from '@/utils/request'


// 查询仪表盘关键指标（在线店铺数、今日消息数、今日自动回复数、AI 回复数、风控触发数，需求 20.1）
// 返回：{ online_shops, today_messages, today_auto_replies, today_ai_replies, today_risk_triggers }
export function fetchDashboardOverview() {
  return get(`/dashboard/overview`)
}

// 查询数据分析趋势（按天聚合的消息量与回复量，需求 20.2）
// 参数：{ start_date?, end_date? }（YYYY-MM-DD，北京时间口径，需求 20.3）
// 返回：{ start_date, end_date, points: [{ date, messages, replies }] }
export function fetchDashboardTrend(params = {}) {
  return get(`/dashboard/trend`, params)
}

// 查询首响时长统计（分布 / 超 5 分钟占比 / 回复率，TIK-025）
// 参数：{ start_date?, end_date?, platform?, shop_pk? }
//   platform: 'pdd' | 'tiktok'，缺省=全部平台；shop_pk 缺省=数据范围内全部店铺
// 返回：{ start_date, end_date, threshold_seconds, platform, shop_pk, shop_count,
//        summary: { responded_cycles, pending_cycles, over_threshold_count,
//                   over_threshold_ratio, reply_rate, p50_seconds, ... },
//        distribution: [{ label, lower, upper, count, ratio }],
//        shops: [{ shop_pk, shop_name, platform, ...同上指标 }] }
// 口径：首响 = 本店首次回复 - 该段第一条买家消息；>300s 计超时；待回复与超时均计未达标
export function fetchFirstResponseStats(params = {}) {
  return get(`/dashboard/first-response`, params)
}
