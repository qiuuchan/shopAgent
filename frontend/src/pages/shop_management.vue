<!--
  店铺管理页面（拼多多自动回复系统前端，任务 17.2）
  覆盖需求：
    - 3.1 新增店铺（账号密码登录 / Cookie 导入两种入口）；
    - 3.3 店铺列表后端分页（默认 20 条，可选 10/20/50/100）；
    - 3.4 修改备注 / 启停用；3.5 停用断连（逻辑删除）；
    - 4.1 账号密码登录入口；4.3 手动粘贴 Cookie 导入入口。
  规范要点：全中文、showToast 由请求层统一处理、弹窗仅关闭按钮关闭、
           加载遮罩 + 转圈、表格固定高度内部滚动、响应式、XSS 防范（SafeHtml）。
-->
<script setup>
import { ref, reactive, onMounted } from 'vue'
import {
  Loading,
  TableContainer,
  Pagination,
  FormModal,
  ConfirmModal,
  SafeHtml,
  Select,
} from '@/components/common'
import { showToast, TOAST_TYPE } from '@/utils/toast'
import { formatDateTime } from '@/utils/format'
import ShopSettingsModal from '@/components/shop_settings/ShopSettingsModal.vue'
import { shopApi } from '@/api'

// -------------------- 列表状态 --------------------
const loading = ref(false) // 列表加载遮罩
const shops = ref([]) // 当前页店铺列表
const total = ref(0) // 总记录数
const page = ref(1) // 当前页码
const pageSize = ref(20) // 每页条数（默认 20）
const statusFilter = ref('') // 状态筛选：''=全部，1=启用，0=停用

// 状态筛选可选项（全中文）
const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 1, label: '启用' },
  { value: 0, label: '停用' },
]

// 加载店铺列表（后端分页，需求 3.3）
async function loadShops() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (statusFilter.value !== '') {
      params.status = statusFilter.value
    }
    const data = await shopApi.fetchShops(params)
    // 后端统一分页结构 {list, total, page, page_size}
    shops.value = (data && data.list) || []
    total.value = (data && data.total) || 0
  } catch (e) {
    // 失败已由请求拦截器统一 showToast，此处不再二次提示（规范 2）
  } finally {
    loading.value = false
  }
}

// 切换筛选：回到第 1 页重新加载
function onStatusChange() {
  page.value = 1
  loadShops()
}

// -------------------- 新增 / 编辑弹窗 --------------------
const formVisible = ref(false)
const saving = ref(false)
const formMode = ref('password') // password=账号密码登录，cookie=Cookie 导入，edit=编辑
const editingShopPk = ref(null)
// 编辑模式下密码是否明文显示（隐藏查看切换）
const showPassword = ref(false)
// 编辑模式下店铺详情（含凭据）是否已成功拉取（避免拉取失败时误清空凭据）
const editDetailLoaded = ref(false)

// 表单数据模型
const form = reactive({
  shop_id: '',
  shop_name: '',
  remark: '',
  username: '',
  password: '',
  cookies: '',
  platform: 'pdd', // 店铺平台：pdd=拼多多 / tiktok=TikTok Shop（TIK-004）
  proxy_server: '', // 店铺出口代理服务器地址（Phase 2 前置，仅 TikTok 通道消费）
})

// 平台选项（新增店铺时选择；TikTok 仅支持账号密码登录）
const PLATFORM_OPTIONS = [
  { value: 'pdd', label: '拼多多' },
  { value: 'tiktok', label: 'TikTok Shop' },
]

// 切换平台：TikTok 仅支持账号密码登录，Cookie 导入态强制回到密码登录
function onPlatformChange() {
  if (form.platform === 'tiktok' && formMode.value === 'cookie') {
    formMode.value = 'password'
  }
}

// 弹窗标题（按模式区分）
const formTitle = ref('')

// 重置表单（platform 保留上次选择，便于连续新增同平台店铺）
function resetForm() {
  form.shop_id = ''
  form.shop_name = ''
  form.remark = ''
  form.username = ''
  form.password = ''
  form.cookies = ''
  form.proxy_server = ''
  editingShopPk.value = null
}

// 打开「账号密码登录」新增（需求 4.1）
function openPasswordLogin() {
  resetForm()
  formMode.value = 'password'
  formTitle.value = '账号密码登录新增店铺'
  formVisible.value = true
}

// 打开「Cookie 导入」新增（需求 4.3；Cookie 导入仅拼多多支持）
function openCookieImport() {
  resetForm()
  formMode.value = 'cookie'
  form.platform = 'pdd'
  formTitle.value = 'Cookie 导入新增店铺'
  formVisible.value = true
}

// 打开「编辑」弹窗（需求 3.4 / 3.6）：可改备注 / 名称，并反显账号 / 密码 / Cookie 供修改
async function openEdit(shop) {
  resetForm()
  formMode.value = 'edit'
  formTitle.value = '编辑店铺'
  editingShopPk.value = shop.id
  form.shop_id = shop.shop_id
  form.shop_name = shop.shop_name || ''
  form.remark = shop.remark || ''
  form.platform = shop.platform || 'pdd'
  showPassword.value = false
  editDetailLoaded.value = false
  formVisible.value = true
  // 拉取详情以反显账号 / 密码 / Cookie 明文（前端以隐藏查看展示，支持编辑保存）
  const detail = await shopApi.fetchShopDetail(shop.id).catch(() => null)
  if (detail) {
    form.username = detail.username || ''
    form.password = detail.password || ''
    form.cookies = detail.cookies || ''
    form.proxy_server = detail.proxy_server || ''
    editDetailLoaded.value = true
  }
}

// 前端基础校验（必填项）；返回 true 表示校验通过
function validateForm() {
  if (formMode.value === 'edit') {
    return true
  }
  if (formMode.value === 'password') {
    if (!form.username.trim() || !form.password.trim()) {
      showToast(
        form.platform === 'tiktok' ? '请填写 TikTok 登录账号与密码' : '请填写拼多多登录账号与密码',
        TOAST_TYPE.WARNING,
      )
      return false
    }
  }
  if (formMode.value === 'cookie') {
    if (!form.cookies.trim()) {
      showToast('请粘贴有效的 Cookie 文本', TOAST_TYPE.WARNING)
      return false
    }
  }
  return true
}

// 提交表单（按模式调用不同接口）
async function submitForm() {
  if (!validateForm()) {
    return
  }
  saving.value = true
  try {
    if (formMode.value === 'edit') {
      const payload = {
        shop_name: form.shop_name.trim() || undefined,
        remark: form.remark.trim() || undefined,
      }
      // 仅在详情成功拉取（凭据已反显）时回传凭据，避免拉取失败误清空已存凭据。
      // 反显编辑为「所见即所得」：提交当前展示值（留空即清空对应凭据）。
      if (editDetailLoaded.value) {
        payload.username = form.username.trim()
        payload.password = form.password
        payload.cookies = form.cookies.trim()
        // 出口代理：提交当前展示值（留空即清空不走代理；仅 TikTok 店铺有意义）。
        payload.proxy_server = form.proxy_server.trim()
      }
      await shopApi.updateShop(editingShopPk.value, payload)
      showToast('店铺已更新', TOAST_TYPE.SUCCESS)
    } else if (formMode.value === 'password') {
      await shopApi.loginShopByPassword({
        username: form.username.trim(),
        password: form.password,
        remark: form.remark.trim() || undefined,
        platform: form.platform,
      })
      showToast('账号密码登录成功，已自动获取店铺信息', TOAST_TYPE.SUCCESS)
    } else {
      await shopApi.importShopByCookie({
        cookies: form.cookies.trim(),
        remark: form.remark.trim() || undefined,
      })
      showToast('Cookie 导入成功，已自动获取店铺信息', TOAST_TYPE.SUCCESS)
    }
    formVisible.value = false
    // 新增后回到第 1 页，编辑保持当前页
    if (formMode.value !== 'edit') {
      page.value = 1
    }
    await loadShops()
  } catch (e) {
    // 失败已统一提示
  } finally {
    saving.value = false
  }
}

// -------------------- 停用确认 --------------------
const disableVisible = ref(false)
const disabling = ref(false)
const disableTarget = ref(null)

// 打开停用确认（需求 3.5）
function openDisable(shop) {
  disableTarget.value = shop
  disableVisible.value = true
}

// 确认停用：逻辑删除并断连
async function confirmDisable() {
  if (!disableTarget.value) {
    return
  }
  disabling.value = true
  try {
    await shopApi.disableShop(disableTarget.value.id)
    showToast('店铺已停用并断开连接', TOAST_TYPE.SUCCESS)
    disableVisible.value = false
    await loadShops()
  } catch (e) {
    // 失败已统一提示
  } finally {
    disabling.value = false
  }
}

// 启用店铺（需求 3.4）
async function enableShop(shop) {
  try {
    await shopApi.updateShop(shop.id, { enabled: true })
    showToast('店铺已启用', TOAST_TYPE.SUCCESS)
    await loadShops()
  } catch (e) {
    // 失败已统一提示
  }
}

// -------------------- 店铺设置弹窗 --------------------
// 将原本独立菜单的店铺级设置（默认回复 / AI / 营业时间 / 过滤黑名单 / 风控 / 转人工）
// 统一收敛到本页行内「设置」入口，不再单独开菜单。
const settingsVisible = ref(false)
const settingsShop = ref(null)

// 打开某店铺的设置弹窗
function openSettings(shop) {
  settingsShop.value = shop
  settingsVisible.value = true
}

// 状态文案
function statusText(status) {
  return status === 1 ? '启用' : '停用'
}

onMounted(loadShops)
</script>

<template>
  <div class="shop-page">
    <!-- 操作栏 -->
    <div class="shop-page__toolbar">
      <div class="shop-page__filters">
        <div class="shop-page__filter-item">
          <Select
            v-model="statusFilter"
            :options="STATUS_OPTIONS"
            placeholder="全部状态"
            @change="onStatusChange"
          />
        </div>
      </div>
      <div class="shop-page__actions">
        <button class="btn btn--primary" type="button" @click="openPasswordLogin">账号密码登录</button>
        <button class="btn btn--default" type="button" @click="openCookieImport">Cookie 导入</button>
      </div>
    </div>

    <!-- 列表区：固定高度内部滚动（规范 29），加载遮罩 + 转圈（规范 23） -->
    <div class="shop-page__table-wrap">
      <Loading :visible="loading" text="加载中..." />
      <TableContainer>
        <table class="data-table">
          <thead>
            <tr>
              <th>平台</th>
              <th>店铺标识</th>
              <th>店铺名称</th>
              <th>备注</th>
              <th>状态</th>
              <th>创建时间</th>
              <th>更新时间</th>
              <th class="data-table__op">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="shop in shops" :key="shop.id">
              <td>
                <span
                  class="tag"
                  :class="shop.platform === 'tiktok' ? 'tag--tiktok' : 'tag--pdd'"
                >
                  {{ shop.platform === 'tiktok' ? 'TikTok' : '拼多多' }}
                </span>
              </td>
              <td><SafeHtml :content="shop.shop_id" /></td>
              <td><SafeHtml :content="shop.shop_name || '-'" /></td>
              <td><SafeHtml :content="shop.remark || '-'" /></td>
              <td>
                <span class="tag" :class="shop.status === 1 ? 'tag--on' : 'tag--off'">
                  {{ statusText(shop.status) }}
                </span>
              </td>
              <td>{{ formatDateTime(shop.created_at) }}</td>
              <td>{{ formatDateTime(shop.updated_at) }}</td>
              <td class="data-table__op">
                <button class="link-btn" type="button" @click="openSettings(shop)">设置</button>
                <button class="link-btn" type="button" @click="openEdit(shop)">编辑</button>
                <button
                  v-if="shop.status === 1"
                  class="link-btn link-btn--danger"
                  type="button"
                  @click="openDisable(shop)"
                >
                  停用
                </button>
                <button v-else class="link-btn" type="button" @click="enableShop(shop)">启用</button>
              </td>
            </tr>
            <tr v-if="!loading && shops.length === 0">
              <td class="data-table__empty" colspan="8">暂无店铺数据</td>
            </tr>
          </tbody>
        </table>
      </TableContainer>
    </div>

    <!-- 后端分页（规范 28） -->
    <Pagination
      :total="total"
      v-model:page="page"
      v-model:page-size="pageSize"
      @change="loadShops"
    />

    <!-- 新增 / 编辑弹窗（仅关闭按钮关闭，规范 7） -->
    <FormModal
      v-model="formVisible"
      :title="formTitle"
      :loading="saving"
      confirm-text="保存"
      @confirm="submitForm"
    >
      <div class="form">
        <!-- 平台选择（新增时选定后不可改；TikTok 仅支持账号密码登录，TIK-004） -->
        <div v-if="formMode !== 'edit'" class="form__item">
          <label class="form__label">平台</label>
          <div class="form__radio-group">
            <label
              v-for="opt in PLATFORM_OPTIONS"
              :key="opt.value"
              class="form__radio"
            >
              <input
                type="radio"
                :value="opt.value"
                v-model="form.platform"
                :disabled="formMode === 'cookie' && opt.value !== 'pdd'"
                @change="onPlatformChange"
              />
              {{ opt.label }}
            </label>
          </div>
        </div>

        <!-- 账号密码登录 / Cookie 导入：店铺标识与名称由登录后自动获取，无需手填 -->
        <p v-if="formMode !== 'edit'" class="form__hint">
          {{
            formMode === 'cookie'
              ? 'Cookie 导入仅支持拼多多，登录成功后将自动获取店铺信息。'
              : form.platform === 'tiktok'
                ? '登录成功后将自动获取 TikTok 店铺信息（泰国站卖家后台）。'
                : '登录成功后将自动获取店铺标识、名称与 Logo，无需手动填写。'
          }}
        </p>

        <div v-if="formMode === 'edit'" class="form__item">
          <label class="form__label">店铺名称</label>
          <input v-model="form.shop_name" class="form__input" type="text" placeholder="选填，店铺展示名称" />
        </div>

        <!-- 编辑模式：反显并可修改账号 / 密码 / Cookie（隐藏查看，需求 3.6） -->
        <template v-if="formMode === 'edit'">
          <div class="form__item">
            <label class="form__label">登录账号</label>
            <input v-model="form.username" class="form__input" type="text" placeholder="拼多多登录账号" />
          </div>
          <div class="form__item">
            <label class="form__label">登录密码</label>
            <div class="form__password">
              <input
                v-model="form.password"
                class="form__input"
                :type="showPassword ? 'text' : 'password'"
                placeholder="拼多多登录密码"
              />
              <button
                type="button"
                class="form__password-toggle"
                :title="showPassword ? '隐藏' : '显示'"
                @click="showPassword = !showPassword"
              >
                {{ showPassword ? '隐藏' : '显示' }}
              </button>
            </div>
          </div>
          <div class="form__item">
            <label class="form__label">Cookie</label>
            <textarea
              v-model="form.cookies"
              class="form__textarea"
              rows="5"
              placeholder="登录 Cookie 文本（可查看 / 修改）"
            ></textarea>
          </div>
          <div v-if="form.platform === 'tiktok'" class="form__item">
            <label class="form__label">出口代理</label>
            <input
              v-model="form.proxy_server"
              class="form__input"
              type="text"
              placeholder="选填，如 http://127.0.0.1:7890 或 socks5://host:port"
            />
            <p class="form__hint">
              仅 TikTok 店铺生效：连接时浏览器会话经该代理出口（Phase 2 前置）；留空表示不走代理。
            </p>
          </div>
          <p class="form__hint">修改账号 / 密码 / Cookie 后保存即生效；店铺在线时将以新 Cookie 自动重连。</p>
        </template>

        <!-- 账号密码登录字段（TikTok 仅支持此方式） -->
        <template v-if="formMode === 'password'">
          <div class="form__item">
            <label class="form__label">登录账号<span class="form__required">*</span></label>
            <input
              v-model="form.username"
              class="form__input"
              type="text"
              :placeholder="form.platform === 'tiktok' ? 'TikTok 卖家后台账号' : '拼多多商家后台账号'"
            />
          </div>
          <div class="form__item">
            <label class="form__label">登录密码<span class="form__required">*</span></label>
            <input
              v-model="form.password"
              class="form__input"
              type="password"
              :placeholder="form.platform === 'tiktok' ? 'TikTok 卖家后台密码' : '拼多多商家后台密码'"
            />
          </div>
          <p class="form__hint">
            {{
              form.platform === 'tiktok'
                ? '提交后将通过浏览器自动登录 TikTok 泰国站卖家后台获取凭据，若出现验证码 / 滑块请按提示完成验证。'
                : '提交后将通过浏览器自动登录获取凭据，若出现验证码 / 滑块请按提示完成验证。'
            }}
          </p>
        </template>

        <!-- Cookie 导入字段（仅拼多多支持） -->
        <template v-if="formMode === 'cookie'">
          <div class="form__item">
            <label class="form__label">Cookie 文本<span class="form__required">*</span></label>
            <textarea
              v-model="form.cookies"
              class="form__textarea"
              rows="5"
              placeholder="请粘贴从拼多多商家后台导出的完整 Cookie 文本"
            ></textarea>
          </div>
          <p class="form__hint">系统将校验 Cookie 有效性，校验通过后保存店铺与凭据。</p>
        </template>

        <div class="form__item">
          <label class="form__label">备注</label>
          <input v-model="form.remark" class="form__input" type="text" placeholder="选填，便于识别的备注" />
        </div>
      </div>
    </FormModal>

    <!-- 停用确认弹窗 -->
    <ConfirmModal
      v-model="disableVisible"
      type="danger"
      title="停用店铺"
      :message="`确认停用店铺「${disableTarget && (disableTarget.shop_name || disableTarget.shop_id)}」吗？停用后将断开其拼多多连接，数据将保留。`"
      confirm-text="确认停用"
      :loading="disabling"
      @confirm="confirmDisable"
    />

    <!-- 店铺设置弹窗（默认回复 / AI / 营业时间 / 过滤黑名单 / 风控 / 转人工） -->
    <ShopSettingsModal v-model="settingsVisible" :shop="settingsShop" />
  </div>
</template>

<style scoped>
.shop-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.shop-page__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.shop-page__filters {
  display: flex;
  gap: 12px;
}

.shop-page__filter-item {
  width: 160px;
}

.shop-page__actions {
  display: flex;
  gap: 10px;
}

/* 表格外层：相对定位以承载区域加载遮罩；固定高度内部滚动 */
.shop-page__table-wrap {
  position: relative;
  flex: 1;
  min-height: 240px;
}

/* 通用按钮样式 */
.btn {
  padding: 8px 16px;
  border-radius: 8px;
  font-size: 14px;
  cursor: pointer;
  border: 1px solid var(--color-border, #e5e6eb);
  transition: background 0.15s ease, border-color 0.15s ease;
}
.btn--primary {
  background: var(--color-primary, #1677ff);
  color: var(--color-on-primary, #ffffff);
  border-color: var(--color-primary, #1677ff);
}
.btn--primary:hover {
  background: var(--color-primary-hover, #4096ff);
}
.btn--default {
  background: var(--color-bg-elevated, #ffffff);
  color: var(--color-text, #1f2329);
}
.btn--default:hover {
  border-color: var(--color-primary, #1677ff);
  color: var(--color-primary, #1677ff);
}

/* 数据表格 */
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}
.data-table th,
.data-table td {
  padding: 10px 12px;
  text-align: left;
  border-bottom: 1px solid var(--color-border, #e5e6eb);
  color: var(--color-text, #1f2329);
  white-space: nowrap;
}
.data-table thead th {
  font-weight: 600;
  color: var(--color-text-secondary, #5c6573);
  background: var(--color-bg-elevated, #ffffff);
}
.data-table__op {
  width: 220px;
}
.data-table__empty {
  text-align: center;
  color: var(--color-text-secondary, #5c6573);
  padding: 32px 0;
}

/* 状态标签 */
.tag {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 12px;
}
.tag--on {
  background: #f0f9eb;
  color: #237804;
}
.tag--off {
  background: #f2f3f5;
  color: #8c8c8c;
}
/* 平台徽标（TIK-004）：拼多多蓝 / TikTok 黑 */
.tag--pdd {
  background: #e8f1ff;
  color: #1677ff;
}
.tag--tiktok {
  background: #f2f3f5;
  color: #111111;
}

/* 平台单选（TIK-004） */
.form__radio-group {
  display: flex;
  gap: 16px;
}
.form__radio {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  color: var(--color-text, #1f2329);
  cursor: pointer;
}
.form__radio input {
  accent-color: var(--color-primary, #1677ff);
  cursor: pointer;
}
.form__radio input:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

/* 行内操作按钮 */
.link-btn {
  border: none;
  background: transparent;
  color: var(--color-primary, #1677ff);
  cursor: pointer;
  font-size: 13px;
  padding: 2px 6px;
}
.link-btn:hover {
  text-decoration: underline;
}
.link-btn--danger {
  color: #ff4d4f;
}

/* 表单 */
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.form__item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.form__label {
  font-size: 13px;
  color: var(--color-text-secondary, #5c6573);
}
.form__required {
  color: #ff4d4f;
  margin-left: 2px;
}
.form__input,
.form__textarea {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--color-border, #e5e6eb);
  border-radius: 6px;
  font-size: 14px;
  background: var(--color-bg-elevated, #ffffff);
  color: var(--color-text, #1f2329);
  box-sizing: border-box;
}
.form__input:focus,
.form__textarea:focus {
  outline: none;
  border-color: var(--color-primary, #1677ff);
}
.form__textarea {
  resize: vertical;
}
.form__password {
  position: relative;
  display: flex;
}
.form__password .form__input {
  padding-right: 56px;
}
.form__password-toggle {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  padding: 2px 8px;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 13px;
  color: var(--color-primary, #1677ff);
}
.form__hint {
  font-size: 12px;
  color: var(--color-text-secondary, #5c6573);
  line-height: 1.5;
}
</style>
