// 店铺平台常量与选项（TIK-002 平台分派 / TIK-025 dashboard 平台维度）
// 职责：集中维护「平台枚举 -> 中文标签」映射，供店铺管理页（建店表单 / 徽标）与
//       数据分析页（首响统计按平台筛选）复用，避免同一份枚举在多处各写一遍（规范 52）。
// 说明：取值须与后端 app.services.account_service.VALID_PLATFORMS 及 sys_dict
//       的 platform 字典保持一致（pdd=拼多多 / tiktok=TikTok Shop）。

// 平台枚举值（与后端一致，禁止随意改动）
export const PLATFORM_PDD = 'pdd'
export const PLATFORM_TIKTOK = 'tiktok'

// 平台下拉选项：value 为枚举值，label 为中文展示名
export const PLATFORM_OPTIONS = [
  { value: PLATFORM_PDD, label: '拼多多' },
  { value: PLATFORM_TIKTOK, label: 'TikTok Shop' },
]

// 平台枚举 -> 中文标签（用于列表徽标等仅展示场景，未收录的枚举原样返回）
const PLATFORM_LABELS = PLATFORM_OPTIONS.reduce((acc, item) => {
  acc[item.value] = item.label
  return acc
}, {})

export function platformLabel(value) {
  return PLATFORM_LABELS[value] || value || '-'
}
