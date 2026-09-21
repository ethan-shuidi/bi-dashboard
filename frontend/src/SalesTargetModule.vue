<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { ElMessageBox } from "element-plus"
import { fetchWithDashboardAuth } from "./dashboardAuth"
import WeekPicker from "./WeekPicker.vue"

const props = defineProps({
  apiBase: { type: String, required: true },
  variant: {
    type: String,
    required: true,
    validator(value) {
      return ["month", "week"].includes(value)
    },
  },
})

const isWeek = computed(() => props.variant === "week")
const now = new Date()
const year = ref(now.getFullYear())
const month = ref(now.getMonth() + 1)
const weekStart = ref(previousWeekStart())
const pickerYear = ref(year.value)
const datePanelOpen = ref(false)
const dateTriggerRef = ref(null)
const datePanelPosition = ref({})
const model = ref("TN10")
const modelOptions = [
  { label: "全部型号", value: "ALL" },
  { label: "TN10", value: "TN10" },
  { label: "TN20", value: "TN20" },
]
const site = ref("全部站点")
const siteOptions = ["全部站点", "欧洲", "美国", "日本", "德国", "英国", "法国", "加拿大", "澳洲", "西班牙", "意大利", "荷兰", "比利时", "墨西哥", "爱尔兰", "波兰", "瑞典"]
const months = Array.from({ length: 12 }, (_, index) => index + 1)
const data = ref(null)
const loading = ref(true)
const saving = ref(false)
const error = ref("")
const notice = ref("")
const targetDraft = ref({})
const savedTargets = ref({})
const quickTargetOpen = ref(false)
const quickTargetDraft = ref({})
const quickTargetSaving = ref(false)
const quickTargetError = ref("")
let dashboardRequestSeq = 0
let columnResizeCleanup = null

const salesColumnDefaults = {
  metric: 128,
  target: 214,
  actual: 148,
  completion: 152,
}
const salesColumnBounds = {
  metric: [96, 280],
  target: [160, 340],
  actual: [112, 280],
  completion: [112, 280],
}
const salesColumns = [
  { key: "metric", label: "指标" },
  { key: "target", label: "目标" },
  { key: "actual", label: "实际完成" },
  { key: "completion", label: "完成率" },
]
const displaySalesColumns = computed(() => salesColumns)
const columnWidthStorageKey = computed(() => `sales-target-column-widths:${props.variant}`)
const columnWidths = ref(loadColumnWidths())
const tableWidth = computed(() => displaySalesColumns.value.reduce((total, column) => total + columnWidths.value[column.key], 0))

const metricRows = computed(() => data.value?.metrics || [])
const editableRows = computed(() => metricRows.value.filter(({ target_input }) => target_input))
const isEuropeSite = computed(() => site.value === "欧洲")
const dirty = computed(() => !isEuropeSite.value && editableRows.value.some(({ key }) => String(targetDraft.value[key] ?? "") !== String(savedTargets.value[key] ?? "")))
const countryTargets = computed(() => data.value?.country_targets || [])
const salesProgress = computed(() => data.value?.progress?.sales || {})
const timeProgress = computed(() => data.value?.progress?.time || {})
const salesProgressPercent = computed(() => {
  const rate = salesProgress.value.rate
  return rate === null || rate === undefined ? null : rate * 100
})
const salesProgressWidth = computed(() => salesProgressPercent.value === null ? 0 : Math.min(100, Math.max(0, salesProgressPercent.value)))
const timeProgressPercent = computed(() => Number(timeProgress.value.rate || 0) * 100)
const actualScopeLabel = computed(() => {
  if (data.value?.period?.actual_end) return `实际完成统计至 ${data.value.period.actual_end}`
  return isWeek.value ? "未来周暂无实际数据" : "未来月份暂无实际数据"
})
const dimensionName = computed(() => isWeek.value ? "周度" : "月度")
const tableTitle = computed(() => `${dimensionName.value}目标完成度看板`)
const targetColumnTitle = computed(() => `${dimensionName.value}目标`)
const weekEnd = computed(() => {
  const start = new Date(`${monday(weekStart.value)}T00:00:00`)
  start.setDate(start.getDate() + 6)
  return formatLocalDate(start)
})
const weekRangeLabel = computed(() => `${formatDotDate(weekStart.value)}~${formatDotDate(weekEnd.value)}`)

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
  for (const item of editableRows.value) {
    let value = targets[item.key]
    if (item.format === "percent" && value !== null && value !== undefined && value !== "") {
      value = String(Number((Number(value) * 100).toFixed(8)))
    }
    values[item.key] = value === null || value === undefined ? "" : String(value)
  }
  targetDraft.value = values
  savedTargets.value = { ...values }
}

async function loadDashboard({ refresh = false } = {}) {
  const requestSeq = ++dashboardRequestSeq
  loading.value = true
  error.value = ""
  try {
    const query = new URLSearchParams({ model: model.value, site: site.value })
    if (isWeek.value) query.set("week_start", weekStart.value)
    else query.set("year", String(year.value)), query.set("month", String(month.value))
    if (refresh) query.set("refresh", "true")
    data.value = await api(`/api/amazon/sales-dashboard${isWeek.value ? "/weekly" : ""}?${query}`)
    if (requestSeq !== dashboardRequestSeq) return
    syncDraft(data.value.targets)
  } catch (exception) {
    if (requestSeq !== dashboardRequestSeq) return
    error.value = exception.message
  } finally {
    if (requestSeq === dashboardRequestSeq) loading.value = false
  }
}

function confirmScopeChange() {
  if (!dirty.value) return true
  return window.confirm(`当前${dimensionName.value}目标尚未保存，切换后将丢失未保存的修改。是否继续？`)
}

function changeYear(delta) {
  const next = pickerYear.value + delta
  if (next < 2000 || next > 2100) return
  pickerYear.value = next
}

function toggleDatePicker() {
  pickerYear.value = year.value
  datePanelOpen.value = !datePanelOpen.value
  if (datePanelOpen.value) nextTick(updateDatePanelPosition)
}

function updateDatePanelPosition() {
  const rect = dateTriggerRef.value?.getBoundingClientRect()
  if (!rect) return
  const viewportGap = 16
  const preferredWidth = 292
  const width = Math.min(preferredWidth, window.innerWidth - viewportGap * 2)
  const estimatedHeight = 190
  let left = Math.min(rect.left, window.innerWidth - viewportGap - width)
  left = Math.max(viewportGap, left)
  let top = rect.bottom + 8
  if (top + estimatedHeight > window.innerHeight - viewportGap && rect.top > estimatedHeight) {
    top = Math.max(viewportGap, rect.top - estimatedHeight - 8)
  }
  datePanelPosition.value = { left: `${left}px`, top: `${top}px`, width: `${width}px` }
}

function chooseMonth(nextMonth) {
  if (nextMonth === month.value && pickerYear.value === year.value) {
    datePanelOpen.value = false
    return
  }
  if (!confirmScopeChange()) return
  year.value = pickerYear.value
  month.value = nextMonth
  datePanelOpen.value = false
}

function changeWeek(nextWeek) {
  const normalized = monday(nextWeek)
  if (normalized === weekStart.value || !confirmScopeChange()) return
  weekStart.value = normalized
}

function changeModel(nextModel) {
  if (nextModel === model.value || !confirmScopeChange()) return
  model.value = nextModel
}

function changeSite(nextSite) {
  if (nextSite === site.value || !confirmScopeChange()) return
  site.value = nextSite
}

async function saveTargets() {
  if (isEuropeSite.value) {
    error.value = "欧洲销量目标由各国目标汇总，请使用快速写入目标"
    return
  }
  saving.value = true
  error.value = ""
  notice.value = ""
  try {
    const targets = {}
    for (const item of editableRows.value) {
      const value = targetDraft.value[item.key] ?? ""
      if (item.format === "percent" && value !== "") {
        const percent = Number(value)
        if (!Number.isFinite(percent)) throw new Error(`${item.label}目标必须是数字`)
        targets[item.key] = percent / 100
      } else {
        targets[item.key] = value
      }
    }
    const payload = isWeek.value
      ? { week_start: weekStart.value, model: model.value, site: site.value, targets }
      : { year: year.value, month: month.value, model: model.value, site: site.value, targets }
    await api(`/api/amazon/sales-dashboard${isWeek.value ? "/weekly" : ""}/targets`, {
      method: "POST",
      body: JSON.stringify(payload),
    })
    await loadDashboard()
    notice.value = `${dimensionName.value}目标已保存`
  } catch (exception) {
    error.value = exception.message
  } finally {
    saving.value = false
  }
}

function openQuickTargets() {
  if (quickTargetSaving.value) return
  if (dirty.value && !window.confirm(`当前${dimensionName.value}目标尚未保存，打开快速写入后将丢失未保存的修改。是否继续？`)) return
  quickTargetError.value = ""
  quickTargetDraft.value = Object.fromEntries(countryTargets.value.map((item) => [
    item.site,
    item.targets?.units === null || item.targets?.units === undefined ? "" : String(item.targets.units),
  ]))
  quickTargetOpen.value = true
}

function closeQuickTargets() {
  if (quickTargetSaving.value) return
  quickTargetOpen.value = false
  quickTargetError.value = ""
}

async function saveQuickTargets() {
  if (quickTargetSaving.value) return
  quickTargetSaving.value = true
  quickTargetError.value = ""
  try {
    const items = countryTargets.value.map((item) => {
      const value = quickTargetDraft.value[item.site] ?? ""
      if (value !== "" && !Number.isFinite(Number(value))) throw new Error(`${item.site}销量目标必须是数字`)
      if (value !== "" && Number(value) < 0) throw new Error(`${item.site}销量目标不能小于 0`)
      return { site: item.site, target_units: value === "" ? null : Number(value) }
    })
    const payload = isWeek.value
      ? { week_start: weekStart.value, model: model.value, items }
      : { year: year.value, month: month.value, model: model.value, items }
    await api(`/api/amazon/sales-dashboard${isWeek.value ? "/weekly" : ""}/targets/bulk`, {
      method: "POST",
      body: JSON.stringify(payload),
    })
    quickTargetOpen.value = false
    await loadDashboard()
    notice.value = `${dimensionName.value}各国销量目标已保存`
  } catch (exception) {
    quickTargetError.value = exception.message
  } finally {
    quickTargetSaving.value = false
  }
}

async function refreshTargets() {
  if (loading.value) return
  if (dirty.value) {
    try {
      await ElMessageBox.confirm("有未保存内容，是否刷新", "刷新确认", {
        confirmButtonText: "是",
        cancelButtonText: "否",
        type: "warning",
      })
    } catch {
      return
    }
  }
  notice.value = ""
  await loadDashboard({ refresh: true })
  if (!error.value) notice.value = `${dimensionName.value}数据已刷新`
}

function formatLocalDate(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`
}

function formatDotDate(value) {
  return String(value || "").replaceAll("-", ".")
}

function previousWeekStart() {
  const value = new Date()
  value.setHours(0, 0, 0, 0)
  value.setDate(value.getDate() - ((value.getDay() + 6) % 7) - 7)
  return formatLocalDate(value)
}

function monday(value) {
  const valueDate = new Date(`${value}T00:00:00`)
  if (Number.isNaN(valueDate.getTime())) return previousWeekStart()
  valueDate.setDate(valueDate.getDate() - ((valueDate.getDay() + 6) % 7))
  return formatLocalDate(valueDate)
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
  if (row.format === "money") return `${value < 0 ? "-" : "+"}${formatMoney(Math.abs(value))}`
  return `${value > 0 ? "+" : ""}${formatNumber(value * 100)} 个百分点`
}

function displayPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—"
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(Number(value))}%`
}

function loadColumnWidths() {
  try {
    const stored = JSON.parse(window.localStorage.getItem(columnWidthStorageKey.value) || "{}")
    const widths = { ...salesColumnDefaults }
    for (const column of salesColumns) {
      const value = Number(stored[column.key])
      const [minWidth, maxWidth] = salesColumnBounds[column.key]
      if (Number.isFinite(value)) widths[column.key] = Math.round(Math.min(maxWidth, Math.max(minWidth, value)))
    }
    return widths
  } catch {
    return { ...salesColumnDefaults }
  }
}

function saveColumnWidths() {
  try {
    window.localStorage.setItem(columnWidthStorageKey.value, JSON.stringify(columnWidths.value))
  } catch {
    // Local storage can be unavailable in private browsing; resizing still works for this session.
  }
}

function startColumnResize(event, columnKey) {
  event.preventDefault()
  event.stopPropagation()
  columnResizeCleanup?.()
  const startX = event.clientX
  const startWidth = columnWidths.value[columnKey]
  const [minWidth, maxWidth] = salesColumnBounds[columnKey]
  const move = (moveEvent) => {
    const nextWidth = Math.round(startWidth + moveEvent.clientX - startX)
    columnWidths.value = { ...columnWidths.value, [columnKey]: Math.min(maxWidth, Math.max(minWidth, nextWidth)) }
  }
  const stop = () => {
    window.removeEventListener("pointermove", move)
    window.removeEventListener("pointerup", stop)
    columnResizeCleanup = null
    saveColumnWidths()
  }
  columnResizeCleanup = stop
  window.addEventListener("pointermove", move)
  window.addEventListener("pointerup", stop, { once: true })
}

function closeDatePicker(event) {
  if (!event.target.closest?.(".sales-date-field, .sales-date-panel")) datePanelOpen.value = false
}

onMounted(() => {
  document.addEventListener("click", closeDatePicker)
  window.addEventListener("resize", updateDatePanelPosition)
  window.addEventListener("scroll", updateDatePanelPosition, true)
  loadDashboard()
})
onBeforeUnmount(() => {
  columnResizeCleanup?.()
  document.removeEventListener("click", closeDatePicker)
  window.removeEventListener("resize", updateDatePanelPosition)
  window.removeEventListener("scroll", updateDatePanelPosition, true)
})
watch(isWeek.value ? [weekStart, model, site] : [year, month, model, site], () => loadDashboard())
</script>

<template>
  <section class="sales-target-module" :aria-label="`${dimensionName}目标模块（筛选仅作用于本模块）`">
    <section class="sales-filter-bar" :aria-label="`${dimensionName}目标模块筛选`">
      <div v-if="!isWeek" class="sales-date-field">
        <button ref="dateTriggerRef" class="sales-date-trigger" type="button" @click.stop="toggleDatePicker">
          <span>日期</span>
          <strong>{{ year }}年{{ month }}月</strong>
          <i :class="{ open: datePanelOpen }">‹</i>
        </button>
        <Teleport to="body">
          <div v-if="datePanelOpen" class="sales-date-panel" :style="datePanelPosition" @click.stop>
            <div class="sales-year-picker" aria-label="年份筛选">
              <button type="button" :disabled="pickerYear <= 2000" @click="changeYear(-1)">‹</button>
              <strong>{{ pickerYear }}年</strong>
              <button type="button" :disabled="pickerYear >= 2100" @click="changeYear(1)">›</button>
            </div>
            <div class="sales-month-grid" aria-label="月份筛选">
              <button v-for="item in months" :key="item" :class="{ active: item === month && pickerYear === year }" type="button" @click="chooseMonth(item)">{{ item }}月</button>
            </div>
          </div>
        </Teleport>
      </div>
      <div v-else class="sales-week-field">
        <span>周 <b>{{ weekRangeLabel }}</b></span>
        <div class="sales-week-picker-control">
          <WeekPicker :model-value="weekStart" @change="changeWeek" />
        </div>
      </div>
      <label class="sales-model-field">
        <span>型号</span>
        <select :value="model" @change="changeModel($event.target.value)">
          <option v-for="item in modelOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <label class="sales-model-field">
        <span>站点</span>
        <select :value="site" @change="changeSite($event.target.value)">
          <option v-for="item in siteOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <div class="sales-filter-scope">
        <span>筛选范围</span>
        <small>仅作用于销量进度和{{ dimensionName }}目标完成度</small>
      </div>
      <div class="sales-filter-actions">
        <button class="sales-save-button" type="button" :disabled="saving || loading || isEuropeSite" :title="isEuropeSite ? '欧洲目标由各国销量目标汇总，请使用快速写入目标' : undefined" @click="saveTargets">{{ saving ? "保存中" : dirty ? "保存*" : "保存" }}</button>
        <button class="sales-refresh-button" type="button" :disabled="saving || loading" @click="refreshTargets">刷新</button>
        <button class="sales-quick-target-button" type="button" :disabled="saving || loading || quickTargetSaving" @click="openQuickTargets">快速写入目标</button>
      </div>
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
          <span class="section-label">{{ dimensionName }}目标完成度</span>
          <h2>{{ tableTitle }}</h2>
        </div>
        <small>手填销量、客单、CPC、广告销量占比、广告CVR；其他目标自动计算。</small>
      </header>
      <div class="sales-table-wrap">
        <table class="sales-target-table" :style="{ width: `${tableWidth}px`, minWidth: `${tableWidth}px` }">
          <colgroup>
            <col v-for="column in displaySalesColumns" :key="column.key" :style="{ width: `${columnWidths[column.key]}px` }">
          </colgroup>
          <thead>
            <tr>
              <th v-for="column in displaySalesColumns" :key="column.key" scope="col">
                <span>{{ column.key === "target" ? targetColumnTitle : column.label }}</span>
                <i
                  class="sales-column-resize-handle"
                  role="separator"
                  aria-orientation="vertical"
                  :title="`拖动调整${column.key === 'target' ? targetColumnTitle : column.label}列宽`"
                  @pointerdown="startColumnResize($event, column.key)"
                ></i>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in metricRows" :key="row.key">
              <th scope="row">{{ row.label }}</th>
              <td>
                <div v-if="isEuropeSite && row.target_input" class="sales-region-target" :title="row.key === 'units' ? '由欧洲各国销量目标汇总' : '欧洲范围不做跨国家汇总'">
                  {{ row.key === "units" ? "各国销量目标汇总" : "不汇总" }}
                </div>
                <div v-else-if="row.target_input && row.format === 'percent'" class="sales-percent-input">
                  <input v-model="targetDraft[row.key]" type="number" min="0" step="any" placeholder="请输入目标" :aria-label="`${row.label} ${dimensionName}目标（百分比）`">
                  <span>%</span>
                </div>
                <input v-else-if="row.target_input" v-model="targetDraft[row.key]" type="number" min="0" step="any" placeholder="请输入目标" :aria-label="`${row.label} ${dimensionName}目标`">
                <div v-else class="sales-auto-target">自动计算</div>
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

    <Teleport to="body">
      <div v-if="quickTargetOpen" class="sales-quick-target-overlay" role="presentation" @click.self="closeQuickTargets">
        <section class="sales-quick-target-dialog" role="dialog" aria-modal="true" :aria-label="`${dimensionName}销量目标快速写入`">
          <header>
            <div>
              <span>{{ dimensionName }}目标</span>
              <h3>快速写入各国销量目标</h3>
            </div>
            <button type="button" :disabled="quickTargetSaving" @click="closeQuickTargets">×</button>
          </header>
          <p>仅写入销量目标；空值会保存为空。其它指标仍保留各国家原值，欧洲只汇总销量目标。</p>
          <div class="sales-quick-target-grid">
            <label v-for="item in countryTargets" :key="item.site">
              <span>{{ item.site }}</span>
              <input v-model="quickTargetDraft[item.site]" type="number" min="0" step="any" placeholder="请输入目标" :aria-label="`${item.site}${dimensionName}销量目标`">
            </label>
          </div>
          <div v-if="quickTargetError" class="sales-quick-target-error" role="alert">{{ quickTargetError }}</div>
          <footer>
            <span>{{ model === "ALL" ? "全部型号" : model }} · {{ isWeek ? weekRangeLabel : `${year}年${month}月` }}</span>
            <div>
              <button type="button" :disabled="quickTargetSaving" @click="closeQuickTargets">取消</button>
              <button type="button" :disabled="quickTargetSaving" @click="saveQuickTargets">{{ quickTargetSaving ? "保存中" : "保存" }}</button>
            </div>
          </footer>
        </section>
      </div>
    </Teleport>
  </section>
</template>
