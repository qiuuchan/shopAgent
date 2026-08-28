<!--
  营业时间设置面板（店铺管理「设置」弹窗内嵌，需求 11.1）
  职责：按店铺维度配置营业时间起止时刻（北京时间口径，可跨午夜）。
  说明：店铺由父级弹窗经 shopPk 传入，本面板不再自带店铺选择器；
        加载有遮罩 + 转圈（规范 23）、文案全中文（规范 27）。
-->
<script setup>
import { reactive, ref, onMounted } from 'vue'
import { Loading } from '@/components/common'
import { showToast, TOAST_TYPE } from '@/utils/toast'
import { autoReplyApi } from '@/api'

const props = defineProps({
  // 当前店铺主键（由父弹窗传入）
  shopPk: {
    type: [Number, String],
    default: '',
  },
})

const loading = ref(false)
const saving = ref(false)

// 表单：起止时刻（HH:MM）、启用开关与生效星期（TIK-007）
const form = reactive({
  start_time: '',
  end_time: '',
  enabled: true,
  weekdays: [],
})

// 星期选项：1=周一 … 7=周日（与后端 weekdays 表达式约定一致，TIK-006）
const WEEKDAY_OPTIONS = [
  { value: 1, label: '周一' },
  { value: 2, label: '周二' },
  { value: 3, label: '周三' },
  { value: 4, label: '周四' },
  { value: 5, label: '周五' },
  { value: 6, label: '周六' },
  { value: 7, label: '周日' },
]

// 将后端 weekdays 表达式（"1,2,3" 逗号串）解析为星期序号数组；
// 空 / 无值表示「每天」，回显为全选（与旧数据兼容，TIK-007）
function parseWeekdays(value) {
  const nums = String(value || '')
    .split(',')
    .map((s) => parseInt(s, 10))
    .filter((n) => n >= 1 && n <= 7)
  return nums.length ? nums : WEEKDAY_OPTIONS.map((o) => o.value)
}

// 星期序号数组序列化为后端表达式；全选（=每天）归一为空串存储
function serializeWeekdays(nums) {
  const all = WEEKDAY_OPTIONS.length
  const selected = (nums || []).filter((n) => n >= 1 && n <= 7)
  if (selected.length === all) {
    return ''
  }
  return selected.slice().sort((a, b) => a - b).join(',')
}

// 将后端可能返回的 HH:MM:SS 归一化为 HH:MM 供 time 输入使用
function normalizeTime(value) {
  if (!value) {
    return ''
  }
  const parts = String(value).split(':')
  if (parts.length >= 2) {
    return `${parts[0].padStart(2, '0')}:${parts[1].padStart(2, '0')}`
  }
  return ''
}

// 拉取营业时间配置
async function loadConfig() {
  if (!props.shopPk) {
    return
  }
  loading.value = true
  const data = await autoReplyApi.getBusinessHours(props.shopPk).catch(() => null)
  loading.value = false
  if (!data) {
    // 未配置：默认全天，表单清空、启用为真、星期全选（=每天）
    Object.assign(form, { start_time: '', end_time: '', enabled: true, weekdays: parseWeekdays('') })
    return
  }
  Object.assign(form, {
    start_time: normalizeTime(data.start_time),
    end_time: normalizeTime(data.end_time),
    enabled: data.enabled !== false,
    weekdays: parseWeekdays(data.weekdays),
  })
}

// 保存营业时间
async function save() {
  if (!props.shopPk) {
    showToast('请先选择店铺', TOAST_TYPE.WARNING)
    return
  }
  const payload = {
    start_time: form.start_time || null,
    end_time: form.end_time || null,
    enabled: form.enabled,
    weekdays: serializeWeekdays(form.weekdays),
  }
  saving.value = true
  const result = await autoReplyApi.saveBusinessHours(props.shopPk, payload).catch(() => null)
  saving.value = false
  if (result === null) {
    return
  }
  showToast('保存成功', TOAST_TYPE.SUCCESS)
}

onMounted(loadConfig)
</script>

<template>
  <div class="panel">
    <p class="panel__desc">配置自动回复运作的营业时间区间（北京时间，可跨午夜）；未配置时默认全天运作。</p>
    <div class="panel__body">
      <Loading :visible="loading" text="加载中..." />
      <label class="field field--inline">
        <input v-model="form.enabled" type="checkbox" />
        <span class="field__label">启用营业时间限制（关闭则全天运作）</span>
      </label>
      <label class="field">
        <span class="field__label">营业开始时刻（北京时间）</span>
        <input v-model="form.start_time" class="field__input" type="time" :disabled="!form.enabled" />
      </label>
      <label class="field">
        <span class="field__label">营业结束时刻（北京时间，可小于开始时刻表示跨午夜）</span>
        <input v-model="form.end_time" class="field__input" type="time" :disabled="!form.enabled" />
      </label>
      <!-- 生效星期多选（TIK-007）：默认全选=每天；后端 weekdays 空串即每天，兼容旧数据 -->
      <div class="field">
        <span class="field__label">生效星期（默认全选=每天）</span>
        <div class="weekdays">
          <label
            v-for="opt in WEEKDAY_OPTIONS"
            :key="opt.value"
            class="weekdays__item"
          >
            <input
              v-model="form.weekdays"
              type="checkbox"
              :value="opt.value"
              :disabled="!form.enabled"
            />
            <span>{{ opt.label }}</span>
          </label>
        </div>
      </div>
      <p class="hint">说明：结束时刻早于开始时刻时表示跨午夜区间（如 22:00 ~ 06:00）。</p>
      <div class="panel__actions">
        <button class="btn btn--primary" type="button" :disabled="saving || !shopPk" @click="save">
          {{ saving ? '保存中...' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
@import '@/components/shop_settings/panel.css';

.hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 16px;
}

/* 生效星期多选（TIK-007） */
.weekdays {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.weekdays__item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
}
.weekdays__item input {
  accent-color: var(--color-primary);
  cursor: pointer;
}
.weekdays__item input:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
</style>
