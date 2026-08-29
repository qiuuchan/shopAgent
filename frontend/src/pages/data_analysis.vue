<!--
  数据分析页面（需求 20.2 / 20.3，TIK-025 扩展首响统计）
  职责：按北京时间口径展示指定时间范围内「按天聚合」的消息量与回复量趋势；
        并按平台 / 店铺展示「首响时长分布、超 5 分钟占比、回复率」
        （数据经 backend /dashboard/trend 与 /dashboard/first-response 获取，前端仅展示）。
  实现：以内联 SVG 绘制双折线图与柱状图（工程未引入图表库），并提供数据表格便于核对。
  规范：加载遮罩 + 转圈（规范 23）、全中文（规范 27）、响应式（规范 20）、
        表格固定高度内部滚动（规范 29）、错误提示统一 showToast（规范 2/4）、不写死 localhost（规范 21）。
-->
<script setup>
import { computed, onMounted, ref } from 'vue'
import { Loading, TableContainer, Select } from '@/components/common'
import { fetchDashboardTrend, fetchFirstResponseStats } from '@/api/dashboard_api'
import { fetchShopOptions } from '@/api/shop_api'
import { PLATFORM_OPTIONS, platformLabel } from '@/config/platforms'
import { formatNumber } from '@/utils/format'

// 趋势加载态 / 首响统计加载态（两块数据各自独立请求，互不影响展示）
const loading = ref(false)
const frLoading = ref(false)

// 起止日期筛选（YYYY-MM-DD，北京时间口径）；为空时由后端默认最近 7 天
const startDate = ref('')
const endDate = ref('')

// 平台 / 店铺筛选（首响统计维度；'' 表示不过滤）
const platform = ref('')
const shopPk = ref('')

// 平台下拉选项：首项「全部平台」+ 枚举选项（枚举集中维护于 config/platforms.js）
const platformOptions = [{ value: '', label: '全部平台' }, ...PLATFORM_OPTIONS]

// 店铺下拉选项：首项「全部店铺」，列表由接口拉取
const shopOptions = ref([{ value: '', label: '全部店铺' }])

// 趋势数据点：[{ date, messages, replies }]
const points = ref([])

// 首响统计结果：{ summary, distribution, shops, threshold_seconds, ... }
const firstResponse = ref(null)

// 拉取店铺下拉选项（失败提示由请求封装统一处理）
async function loadShopOptions() {
  const options = await fetchShopOptions().catch(() => [])
  shopOptions.value = [{ value: '', label: '全部店铺' }, ...options]
}

// 拉取趋势数据（失败提示由请求封装统一处理）
async function loadTrend() {
  loading.value = true
  try {
    const params = {}
    if (startDate.value) {
      params.start_date = startDate.value
    }
    if (endDate.value) {
      params.end_date = endDate.value
    }
    const data = await fetchDashboardTrend(params)
    points.value = (data && data.points) || []
  } finally {
    loading.value = false
  }
}

// 拉取首响时长统计（平台 / 店铺 / 日期三个维度共用同一份筛选条件）
async function loadFirstResponse() {
  frLoading.value = true
  try {
    const params = {}
    if (startDate.value) {
      params.start_date = startDate.value
    }
    if (endDate.value) {
      params.end_date = endDate.value
    }
    if (platform.value) {
      params.platform = platform.value
    }
    if (shopPk.value !== '') {
      params.shop_pk = shopPk.value
    }
    const data = await fetchFirstResponseStats(params)
    firstResponse.value = data || null
  } finally {
    frLoading.value = false
  }
}

// 一次点击同时刷新趋势与首响统计
function onSearch() {
  loadTrend()
  loadFirstResponse()
}

// 重置筛选并重新查询
function onReset() {
  startDate.value = ''
  endDate.value = ''
  platform.value = ''
  shopPk.value = ''
  onSearch()
}

onMounted(() => {
  loadShopOptions()
  onSearch()
})

// ---------------------------------------------------------------------------
// 首响统计展示辅助
// ---------------------------------------------------------------------------
// 汇总指标（无数据时返回空对象，模板统一按 null 处理）
const frSummary = computed(() => (firstResponse.value && firstResponse.value.summary) || null)

// 分布桶：[{ label, count, ratio }]
const frBuckets = computed(() => (firstResponse.value && firstResponse.value.distribution) || [])

// 分店铺明细
const frShops = computed(() => (firstResponse.value && firstResponse.value.shops) || [])

// 是否已取到首响数据（有已回复周期或待回复周期即视为有数据）
const frHasData = computed(() => {
  const s = frSummary.value
  return Boolean(s && (s.responded_cycles || s.pending_cycles))
})

// 秒数格式化：不足 1 分钟按秒展示，否则按「x 分 y 秒」展示，空值统一显示占位符
function formatSeconds(value) {
  if (value === null || value === undefined) {
    return '—'
  }
  const total = Math.round(Number(value))
  if (total < 60) {
    return `${total} 秒`
  }
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`
}

// 比例格式化：0~1 的小数转百分比，空值统一显示占位符
function formatPercent(value) {
  if (value === null || value === undefined) {
    return '—'
  }
  return `${(Number(value) * 100).toFixed(1)}%`
}

// ---------------------------------------------------------------------------
// 折线图几何计算（内联 SVG，不依赖图表库）
// ---------------------------------------------------------------------------
// 图表视图盒尺寸（viewBox 坐标系，随容器自适应缩放）
const VIEW_WIDTH = 760
const VIEW_HEIGHT = 280
// 内边距（留出坐标轴与标签空间）
const PADDING = { top: 20, right: 20, bottom: 36, left: 48 }

// 绘图区宽高
const plotWidth = VIEW_WIDTH - PADDING.left - PADDING.right
const plotHeight = VIEW_HEIGHT - PADDING.top - PADDING.bottom

// Y 轴最大值（消息量与回复量的最大值，至少为 1，避免除零）
const maxValue = computed(() => {
  let max = 0
  for (const p of points.value) {
    max = Math.max(max, Number(p.messages) || 0, Number(p.replies) || 0)
  }
  return Math.max(max, 1)
})

// 计算某数据点在某序列下的 X/Y 坐标
function pointX(index) {
  const count = points.value.length
  if (count <= 1) {
    return PADDING.left + plotWidth / 2
  }
  return PADDING.left + (plotWidth * index) / (count - 1)
}

function pointY(value) {
  const ratio = (Number(value) || 0) / maxValue.value
  return PADDING.top + plotHeight - ratio * plotHeight
}

// 生成某序列（messages / replies）的折线 polyline points 字符串
function buildPolyline(field) {
  return points.value
    .map((p, idx) => `${pointX(idx)},${pointY(p[field])}`)
    .join(' ')
}

const messageLine = computed(() => buildPolyline('messages'))
const replyLine = computed(() => buildPolyline('replies'))

// Y 轴刻度（0、1/2、最大值三档）
const yTicks = computed(() => {
  const max = maxValue.value
  return [
    { value: max, y: pointY(max) },
    { value: Math.round(max / 2), y: pointY(max / 2) },
    { value: 0, y: pointY(0) },
  ]
})

// X 轴标签：点较多时稀疏显示，避免重叠（最多约 8 个标签）
const xLabels = computed(() => {
  const count = points.value.length
  if (count === 0) {
    return []
  }
  const step = Math.max(1, Math.ceil(count / 8))
  const labels = []
  points.value.forEach((p, idx) => {
    if (idx % step === 0 || idx === count - 1) {
      // 仅展示 MM-DD，节省横向空间
      labels.push({ x: pointX(idx), text: String(p.date).slice(5) })
    }
  })
  return labels
})

// 是否有数据（控制空状态展示）
const hasData = computed(() => points.value.length > 0)

// ---------------------------------------------------------------------------
// 首响分布柱状图几何计算（内联 SVG，不依赖图表库）
// ---------------------------------------------------------------------------
// 柱状图视图盒尺寸与内边距（与折线图同宽，保证两张图左右对齐）
const BAR_VIEW_WIDTH = 760
const BAR_VIEW_HEIGHT = 220
const BAR_PADDING = { top: 20, right: 20, bottom: 48, left: 48 }

const barPlotWidth = BAR_VIEW_WIDTH - BAR_PADDING.left - BAR_PADDING.right
const barPlotHeight = BAR_VIEW_HEIGHT - BAR_PADDING.top - BAR_PADDING.bottom

// Y 轴最大值（各桶计数的最大值，至少为 1，避免除零）
const barMaxCount = computed(() => {
  let max = 0
  for (const b of frBuckets.value) {
    max = Math.max(max, Number(b.count) || 0)
  }
  return Math.max(max, 1)
})

// 柱状图柱子几何：按桶均分宽度，中心对齐，柱宽取格宽的 56%
const bars = computed(() => {
  const count = frBuckets.value.length
  if (count === 0) {
    return []
  }
  const slot = barPlotWidth / count
  const width = slot * 0.56
  return frBuckets.value.map((bucket, index) => {
    const value = Number(bucket.count) || 0
    const height = (value / barMaxCount.value) * barPlotHeight
    return {
      key: bucket.label,
      x: BAR_PADDING.left + slot * index + (slot - width) / 2,
      y: BAR_PADDING.top + barPlotHeight - height,
      width,
      height,
      label: bucket.label,
      count: value,
      // 末桶即「超 5 分钟」，单独高亮，便于一眼看到超时量
      overtime: bucket.upper === null || bucket.upper === undefined,
    }
  })
})

// 柱状图 Y 轴刻度（0、1/2、最大值三档）
const barYTicks = computed(() => {
  const max = barMaxCount.value
  return [max, Math.round(max / 2), 0].map((value) => ({
    value,
    y: BAR_PADDING.top + barPlotHeight - (value / max) * barPlotHeight,
  }))
})
</script>

<template>
  <div class="analysis">
    <h2 class="analysis__title">数据分析</h2>

    <!-- 筛选区：起止日期 + 平台 / 店铺（首响统计维度）+ 查询 / 重置 -->
    <div class="analysis__filters">
      <label class="filter-item">
        <span class="filter-item__label">起始日期</span>
        <input v-model="startDate" type="date" class="filter-item__input" />
      </label>
      <label class="filter-item">
        <span class="filter-item__label">结束日期</span>
        <input v-model="endDate" type="date" class="filter-item__input" />
      </label>
      <div class="filter-item filter-item--select">
        <span class="filter-item__label">平台</span>
        <Select v-model="platform" :options="platformOptions" placeholder="全部平台" />
      </div>
      <div class="filter-item filter-item--select">
        <span class="filter-item__label">店铺</span>
        <Select v-model="shopPk" :options="shopOptions" placeholder="全部店铺" />
      </div>
      <div class="analysis__filter-actions">
        <button
          type="button"
          class="btn btn--primary"
          :disabled="loading || frLoading"
          @click="onSearch"
        >查询</button>
        <button type="button" class="btn" :disabled="loading || frLoading" @click="onReset">重置</button>
      </div>
    </div>

    <!-- 图表与数据表（相对定位承载加载遮罩） -->
    <div class="analysis__body">
      <!-- 折线图卡片 -->
      <div class="chart-card">
        <div class="chart-card__legend">
          <span class="legend-item"><i class="legend-dot legend-dot--msg"></i>消息量</span>
          <span class="legend-item"><i class="legend-dot legend-dot--reply"></i>回复量</span>
        </div>

        <svg
          v-if="hasData"
          class="chart-svg"
          :viewBox="`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`"
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label="消息量与回复量趋势折线图"
        >
          <!-- Y 轴刻度线与标签 -->
          <g class="chart-axis">
            <line
              v-for="tick in yTicks"
              :key="`grid-${tick.value}`"
              :x1="PADDING.left"
              :y1="tick.y"
              :x2="VIEW_WIDTH - PADDING.right"
              :y2="tick.y"
              class="chart-grid-line"
            />
            <text
              v-for="tick in yTicks"
              :key="`ylabel-${tick.value}`"
              :x="PADDING.left - 8"
              :y="tick.y + 4"
              text-anchor="end"
              class="chart-axis-label"
            >{{ tick.value }}</text>
          </g>

          <!-- X 轴标签 -->
          <g class="chart-axis">
            <text
              v-for="label in xLabels"
              :key="`xlabel-${label.x}`"
              :x="label.x"
              :y="VIEW_HEIGHT - PADDING.bottom + 20"
              text-anchor="middle"
              class="chart-axis-label"
            >{{ label.text }}</text>
          </g>

          <!-- 消息量折线 -->
          <polyline :points="messageLine" class="chart-line chart-line--msg" />
          <!-- 回复量折线 -->
          <polyline :points="replyLine" class="chart-line chart-line--reply" />
        </svg>

        <!-- 空状态 -->
        <div v-else class="chart-empty">所选范围内暂无数据</div>
      </div>

      <!-- 日趋势数据表（固定高度内部滚动，规范 29） -->
      <div class="analysis__table-wrap">
        <TableContainer max-height="320px">
          <table class="data-table">
            <thead>
              <tr>
                <th>日期</th>
                <th>消息量</th>
                <th>回复量</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in points" :key="p.date">
                <td>{{ p.date }}</td>
                <td>{{ formatNumber(p.messages) }}</td>
                <td>{{ formatNumber(p.replies) }}</td>
              </tr>
              <tr v-if="!hasData">
                <td colspan="3" class="data-table__empty">暂无数据</td>
              </tr>
            </tbody>
          </table>
        </TableContainer>
      </div>

      <Loading :visible="loading" text="加载中..." />
    </div>

    <!-- 首响统计：分布 / 超 5 分钟占比 / 回复率（TIK-025） -->
    <div class="analysis__body analysis__body--fr">
      <div class="chart-card">
        <div class="fr-head">
          <h3 class="fr-head__title">
            首响统计
            <span v-if="firstResponse" class="fr-head__range">
              （{{ firstResponse.start_date }} ~ {{ firstResponse.end_date }}，阈值
              {{ formatSeconds(firstResponse.threshold_seconds) }}）
            </span>
          </h3>
          <p class="fr-head__hint">
            口径：首响 = 本店首次回复 − 该段第一条买家消息；超阈值与窗口结束仍待回复均计未达标。
          </p>
        </div>

        <!-- 关键指标卡片 -->
        <div class="fr-metrics">
          <div class="fr-metric">
            <span class="fr-metric__label">回复率</span>
            <span class="fr-metric__value fr-metric__value--strong">
              {{ formatPercent(frSummary && frSummary.reply_rate) }}
            </span>
          </div>
          <div class="fr-metric">
            <span class="fr-metric__label">已回复周期</span>
            <span class="fr-metric__value">{{ formatNumber(frSummary && frSummary.responded_cycles) }}</span>
          </div>
          <div class="fr-metric">
            <span class="fr-metric__label">待回复周期</span>
            <span class="fr-metric__value">{{ formatNumber(frSummary && frSummary.pending_cycles) }}</span>
          </div>
          <div class="fr-metric">
            <span class="fr-metric__label">超 5 分钟占比</span>
            <span class="fr-metric__value fr-metric__value--warn">
              {{ formatPercent(frSummary && frSummary.over_threshold_ratio) }}
            </span>
          </div>
          <div class="fr-metric">
            <span class="fr-metric__label">首响 P50</span>
            <span class="fr-metric__value">{{ formatSeconds(frSummary && frSummary.p50_seconds) }}</span>
          </div>
          <div class="fr-metric">
            <span class="fr-metric__label">首响 P90</span>
            <span class="fr-metric__value">{{ formatSeconds(frSummary && frSummary.p90_seconds) }}</span>
          </div>
        </div>

        <!-- 首响时长分布柱状图 -->
        <svg
          v-if="frHasData"
          class="chart-svg"
          :viewBox="`0 0 ${BAR_VIEW_WIDTH} ${BAR_VIEW_HEIGHT}`"
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label="首响时长分布柱状图"
        >
          <g class="chart-axis">
            <line
              v-for="tick in barYTicks"
              :key="`bar-grid-${tick.value}`"
              :x1="BAR_PADDING.left"
              :y1="tick.y"
              :x2="BAR_VIEW_WIDTH - BAR_PADDING.right"
              :y2="tick.y"
              class="chart-grid-line"
            />
            <text
              v-for="tick in barYTicks"
              :key="`bar-ylabel-${tick.value}`"
              :x="BAR_PADDING.left - 8"
              :y="tick.y + 4"
              text-anchor="end"
              class="chart-axis-label"
            >{{ tick.value }}</text>
          </g>

          <!-- 柱子：末桶（超 5 分钟）高亮 -->
          <rect
            v-for="bar in bars"
            :key="`bar-${bar.key}`"
            :x="bar.x"
            :y="bar.y"
            :width="bar.width"
            :height="bar.height"
            :class="bar.overtime ? 'chart-bar chart-bar--overtime' : 'chart-bar'"
          />
          <!-- 柱顶计数与 X 轴桶标签 -->
          <text
            v-for="bar in bars"
            :key="`bar-value-${bar.key}`"
            :x="bar.x + bar.width / 2"
            :y="bar.y - 6"
            text-anchor="middle"
            class="chart-bar-value"
          >{{ bar.count }}</text>
          <text
            v-for="bar in bars"
            :key="`bar-label-${bar.key}`"
            :x="bar.x + bar.width / 2"
            :y="BAR_VIEW_HEIGHT - BAR_PADDING.bottom + 22"
            text-anchor="middle"
            class="chart-axis-label"
          >{{ bar.label }}</text>
        </svg>
        <div v-else class="chart-empty">所选范围内暂无首响数据</div>
      </div>

      <!-- 分店铺明细（固定高度内部滚动，规范 29） -->
      <div class="analysis__table-wrap">
        <TableContainer max-height="280px">
          <table class="data-table">
            <thead>
              <tr>
                <th>店铺</th>
                <th>平台</th>
                <th>已回复周期</th>
                <th>待回复周期</th>
                <th>超 5 分钟</th>
                <th>超 5 分钟占比</th>
                <th>首响 P50</th>
                <th>首响 P90</th>
                <th>回复率</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in frShops" :key="row.shop_pk">
                <td>{{ row.shop_name || `店铺 #${row.shop_pk}` }}</td>
                <td>{{ platformLabel(row.platform) }}</td>
                <td>{{ formatNumber(row.responded_cycles) }}</td>
                <td>{{ formatNumber(row.pending_cycles) }}</td>
                <td>{{ formatNumber(row.over_threshold_count) }}</td>
                <td>{{ formatPercent(row.over_threshold_ratio) }}</td>
                <td>{{ formatSeconds(row.p50_seconds) }}</td>
                <td>{{ formatSeconds(row.p90_seconds) }}</td>
                <td>{{ formatPercent(row.reply_rate) }}</td>
              </tr>
              <tr v-if="!frShops.length">
                <td colspan="9" class="data-table__empty">暂无数据</td>
              </tr>
            </tbody>
          </table>
        </TableContainer>
      </div>

      <Loading :visible="frLoading" text="加载中..." />
    </div>
  </div>
</template>

<style scoped>
.analysis {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 16px;
  /* 趋势与首响两块内容纵向排列，超出容器高度时整页滚动（表格内部仍有独立滚动条） */
  overflow-y: auto;
}

.analysis__title {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text);
}

/* 筛选区 */
.analysis__filters {
  display: flex;
  align-items: flex-end;
  gap: 16px;
  flex-wrap: wrap;
  padding: 16px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: 10px;
}

.filter-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.filter-item__label {
  font-size: 13px;
  color: var(--color-text-secondary);
}

/* 下拉筛选（平台 / 店铺）：固定宽度，与日期输入框视觉对齐 */
.filter-item--select {
  width: 180px;
}

.filter-item__input {
  padding: 8px 12px;
  font-size: 14px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.analysis__filter-actions {
  display: flex;
  gap: 10px;
}

.btn {
  padding: 8px 18px;
  font-size: 14px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg-elevated);
  color: var(--color-text);
  cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
}

.btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn--primary {
  background: var(--color-primary);
  color: var(--color-on-primary);
  border-color: var(--color-primary);
}

.btn--primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
  color: var(--color-on-primary);
}

.btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

/* 图表与表格区（相对定位承载加载遮罩） */
.analysis__body {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 16px;
  flex: 0 0 auto;
}

/* 首响统计区：与趋势区间隔分隔线，视觉上区分两块统计 */
.analysis__body--fr {
  padding-top: 16px;
  border-top: 1px dashed var(--color-border);
}

/* 首响统计标题区 */
.fr-head {
  margin-bottom: 12px;
}

.fr-head__title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.fr-head__range {
  font-size: 13px;
  font-weight: 400;
  color: var(--color-text-secondary);
}

.fr-head__hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* 关键指标卡片（响应式栅格，窄屏自动折行） */
.fr-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.fr-metric {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.fr-metric__label {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.fr-metric__value {
  font-size: 20px;
  font-weight: 600;
  color: var(--color-text);
}

.fr-metric__value--strong {
  color: var(--color-primary);
}

.fr-metric__value--warn {
  color: #d97706;
}

/* 柱状图 */
.chart-bar {
  fill: #1677ff;
  opacity: 0.85;
}

.chart-bar--overtime {
  fill: #d97706;
}

.chart-bar-value {
  fill: var(--color-text-secondary);
  font-size: 12px;
}

.chart-card {
  padding: 16px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  box-shadow: var(--shadow-card);
}

.chart-card__legend {
  display: flex;
  gap: 20px;
  margin-bottom: 8px;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.legend-dot {
  width: 12px;
  height: 4px;
  border-radius: 2px;
  display: inline-block;
}

.legend-dot--msg {
  background: #1677ff;
}

.legend-dot--reply {
  background: #15a97c;
}

.chart-svg {
  width: 100%;
  height: auto;
}

.chart-grid-line {
  stroke: var(--color-border);
  stroke-width: 1;
}

.chart-axis-label {
  fill: var(--color-text-secondary);
  font-size: 12px;
}

.chart-line {
  fill: none;
  stroke-width: 2;
}

.chart-line--msg {
  stroke: #1677ff;
}

.chart-line--reply {
  stroke: #15a97c;
}

.chart-empty {
  padding: 60px 0;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 14px;
}

/* 数据表 */
.analysis__table-wrap {
  flex: 0 0 auto;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.data-table th,
.data-table td {
  padding: 10px 14px;
  text-align: left;
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text);
  white-space: nowrap;
}

.data-table thead th {
  background: var(--color-bg-elevated);
  color: var(--color-text-secondary);
  font-weight: 600;
}

.data-table__empty {
  text-align: center;
  color: var(--color-text-secondary);
  padding: 32px 0;
}
</style>
