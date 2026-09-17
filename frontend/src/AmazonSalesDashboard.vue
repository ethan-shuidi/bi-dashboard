<script setup>
import { computed, onMounted, ref, watch } from "vue"
import { fetchWithDashboardAuth } from "./dashboardAuth"

const props = defineProps({
  apiBase: { type: String, required: true },
})

const now = new Date()
const year = ref(now.getFullYear())
const month = ref(now.getMonth() + 1)
const model = ref("TN10")
const models = ["TN10", "TN20"]
const months = Array.from({ length: 12 }, (_, index) => index + 1)
const data = ref(null)
const loading = ref(true)
const saving = ref(false)
const error = ref("")
const notice = ref("")
const targetDraft = ref({})
const savedTargets = ref({})

const metricRows = computed(() => data.value?.metrics || [])
const dirty = computed(() => metricRows.value.some(({ key }) => String(targetDraft.value[key] ?? "") !== String(savedTargets.value[key] ?? "")))
const salesProgress = computed(() => data.value?.progress?.sales || {})
const timeProgress = computed(() => data.value?.progress?.time || {})
const salesProgressPercent = computed(() => {
  const rate = salesProgress.value.rate
  return rate === null || rate === undefined ? null : rate * 100
})
const salesProgressWidth = computed(() => salesProgressPercent.value === null ? 0 : Math.min(100, Math.max(0, salesProgressPercent.value)))
const timeProgressPercent = computed(() => Number(timeProgress.value.rate || 0) * 100)
const actualScopeLabel = computed(() => data.value?.period?.actual_end ? `实际完成统计至 ${data.value.period.actual_end}` : "未来月份暂无实际数据")

async function api(path, options = {}) {
  const response = await fetchWithDashboardAuth(`${props.apiBase}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || `请求失败：HTTP ${response.status}`)
  return body
}

function syncDraft(targets = {}) {
  const values = {}
  for (const item of data.value?.metrics || []) {
    const value = targets[item.key]
    values[item.key] = value === null || value === undefined ? "" : String(value)
  }
  targetDraft.value = values
  savedTargets.value = { ...values }
}

async function loadDashboard({ refresh = false } = {}) {
  loading.value = true
  error.value = ""
  try {
    const query = new URLSearchParams({ year: String(year.value), month: String(month.value), model: model.value })
    if (refresh) query.set("refresh", "true")
    data.value = await api(`/api/amazon/sales-dashboard?${query}`)
    syncDraft(data.value.targets)
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

function confirmScopeChange() {
  if (!dirty.value) return true
  return window.confirm("当前月度目标尚未保存，切换后将丢失未保存的修改。是否继续？")
}

function changeYear(delta) {
  if (!confirmScopeChange()) return
  const next = year.value + delta
  if (next < 2000 || next > 2100) return
  year.value = next
}

function changeMonth(nextMonth) {
  if (nextMonth === month.value || !confirmScopeChange()) return
  month.value = nextMonth
}

function changeModel(nextModel) {
  if (nextModel === model.value || !confirmScopeChange()) return
  model.value = nextModel
}

async function saveTargets() {
  saving.value = true
  error.value = ""
  notice.value = ""
  try {
    const targets = {}
    for (const item of metricRows.value) {
      targets[item.key] = targetDraft.value[item.key] ?? ""
    }
    await api("/api/amazon/sales-dashboard/targets", {
      method: "POST",
      body: JSON.stringify({ year: year.value, month: month.value, model: model.value, targets }),
    })
    await loadDashboard()
    notice.value = "月度目标已保存"
  } catch (exception) {
    error.value = exception.message
  } finally {
    saving.value = false
  }
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—"
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(Number(value))
}

function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "—"
  return new Intl.NumberFormat("zh-CN", { style: "currency", currency: data.value?.currency || "USD", maximumFractionDigits: 2 }).format(Number(value))
}

function formatPercent(value) {
  if (value === null || value === undefined || value === "") return "—"
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(Number(value) * 100)}%`
}

function formatMetric(row, field = "actual") {
  const value = row[field]
  if (value === null || value === undefined) return "—"
  if (row.format === "count") return formatNumber(value)
  if (row.format === "money") return formatMoney(value)
  return formatPercent(value)
}

function formatCompletion(row) {
  const completion = row.completion
  if (!completion || completion.value === null || completion.value === undefined) return "—"
  const value = Number(completion.value)
  if (["units", "net_sales"].includes(row.key)) return formatPercent(value)
  if (row.format === "count") return `${value > 0 ? "+" : ""}${formatNumber(value)}`
  if (row.format === "money") {
    return `${value < 0 ? "-" : "+"}${formatMoney(Math.abs(value))}`
  }
  return `${value > 0 ? "+" : ""}${formatNumber(value * 100)} 个百分点`
}

function displayPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—"
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(Number(value))}%`
}

onMounted(() => loadDashboard())
watch([year, month, model], () => loadDashboard())
</script>

<template>
  <section class="sales-dashboard content-shell">
    <section class="page-header">
      <div>
        <div class="breadcrumb"><span>数据中心</span><i>/</i><strong>销售分析</strong></div>
        <h1>Amazon-销售看板</h1>
        <p>按型号合并主链接与小链接，跟踪月度目标完成情况。</p>
      </div>
      <div class="page-header-actions">
        <div class="topbar-center"><span class="live-dot"></span><span>领星数据在线</span></div>
        <span v-if="data" class="period-badge">{{ data.year }}年{{ data.month }}月 · {{ data.model }}</span>
      </div>
    </section>

    <section class="sales-filter-bar" aria-label="销售看板筛选">
      <div class="sales-year-picker" aria-label="年份筛选">
        <button type="button" :disabled="year <= 2000" @click="changeYear(-1)">‹</button>
        <strong>{{ year }}年</strong>
        <button type="button" :disabled="year >= 2100" @click="changeYear(1)">›</button>
      </div>
      <div class="sales-month-grid" aria-label="月份筛选">
        <button v-for="item in months" :key="item" :class="{ active: item === month }" type="button" @click="changeMonth(item)">{{ item }}月</button>
      </div>
      <label class="sales-model-field">
        <span>型号</span>
        <select :value="model" @change="changeModel($event.target.value)">
          <option v-for="item in models" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="sales-save-button" type="button" :disabled="saving || loading" @click="saveTargets">{{ saving ? "保存中" : dirty ? "保存*" : "保存" }}</button>
    </section>

    <section class="sales-progress-panel" aria-label="销量目标进度">
      <div class="sales-progress-copy">
        <div>
          <span>销量目标进度</span>
          <strong>{{ salesProgressPercent === null ? "未设置目标" : displayPercent(salesProgressPercent) }}</strong>
        </div>
        <small>{{ actualScopeLabel }}</small>
      </div>
      <div class="sales-progress-track" role="progressbar" :aria-valuenow="salesProgressPercent === null ? undefined : Math.round(salesProgressPercent)" aria-valuemin="0" aria-valuemax="100" :aria-label="salesProgressPercent === null ? '销量完成进度未设置目标' : `销量完成 ${displayPercent(salesProgressPercent)}`">
        <i :style="{ width: `${salesProgressWidth}%` }" :class="{ over: salesProgressPercent > 100 }"></i>
      </div>
      <div class="sales-progress-meta">
        <span>实际销量 {{ formatNumber(salesProgress.actual) }} / 目标销量 {{ formatNumber(salesProgress.target) }}</span>
        <span>时间进度：{{ displayPercent(timeProgressPercent) }}</span>
      </div>
    </section>

    <section v-if="error" class="message error-message" role="alert"><strong>数据加载失败</strong><span>{{ error }}</span></section>
    <section v-if="notice" class="message success-message" role="status"><span>{{ notice }}</span></section>
    <section v-if="loading" class="sales-loading">正在获取领星产品表现和云端目标…</section>

    <section v-else class="sales-table-panel">
      <header>
        <div>
          <span class="section-label">月度目标完成度</span>
          <h2>月度目标完成度看板</h2>
        </div>
        <small>百分比目标请按小数填写：10% 填 0.1。目标保存后完成率立即重算。</small>
      </header>
      <div class="sales-table-wrap">
        <table class="sales-target-table">
          <thead>
            <tr><th>指标</th><th>月度目标</th><th>实际完成</th><th>完成率</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in metricRows" :key="row.key">
              <th scope="row">{{ row.label }}</th>
              <td>
                <input v-model="targetDraft[row.key]" type="number" min="0" step="any" :placeholder="row.format === 'percent' ? '如 0.1' : '请输入目标'" aria-label="{{ row.label }} 月度目标">
              </td>
              <td>{{ formatMetric(row, "actual") }}</td>
              <td><span :class="['sales-completion', row.completion?.status]">{{ formatCompletion(row) }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <footer v-if="data?.data_quality">
        <span>数据源：领星产品表现</span>
        <span v-if="data.data_quality.source">来源状态：{{ data.data_quality.source }}</span>
        <span>时间基准：{{ data.progress?.time?.site_date || "—" }}（{{ data.progress?.time?.timezone_basis || "站点日期" }}）</span>
      </footer>
    </section>
  </section>
</template>
