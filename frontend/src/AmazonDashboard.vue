<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { fetchWithDashboardAuth } from "./dashboardAuth"

const apiBase = ref("")
const comparison = ref("周")
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}` }
const toDate = (value) => {
  if (value instanceof Date) return new Date(value.getFullYear(), value.getMonth(), value.getDate())
  const [year, month, day] = String(value || "").split("-").map(Number)
  return year && month && day ? new Date(year, month - 1, day) : null
}
const formatDate = (value) => {
  const d = toDate(value) || new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}
const todayDate = () => toDate(today())
const startDate = ref(today())
const endDate = ref(today())
const site = ref("美国")
const selectedSeries = ref([])
const selectedProducts = ref([])
const rows = ref([])
const periods = ref([])
const loading = ref(true)
const error = ref("")
const currency = ref("USD")
const expanded = ref(new Set())

const seriesOptions = ["TN10系列（主链接）汇总", "TN10系列（小链接）汇总", "TN20系列（主链接）汇总"]
const productOptions = ["TN10-主链接-黑色", "TN10-主链接-银色", "TN10-主链接-橙色", "TN10-小链接-黑色", "TN10-小链接-银色", "TN10-小链接-橙色", "TN20-主链接-黑色", "TN20-主链接-银色", "TN20-主链接-红"]
const sites = ref(["美国"])

const fixedColumns = ref([
  { key: "period", label: "时间", width: 185 },
  { key: "series", label: "系列（展开查看产品）", width: 165 },
])
const dataColumns = ref([
  { key: "acoas", label: "ACoAS", width: 108, visible: true },
  { key: "ad_sales_share", label: "广告销量占比", width: 132, visible: true },
  { key: "ad_order_share", label: "广告订单占比", width: 132, visible: true },
  { key: "units", label: "销量", width: 92, visible: true },
  { key: "net_sales", label: "净销售额", width: 128, visible: true },
  { key: "orders", label: "订单量", width: 92, visible: true },
  { key: "b2b_units", label: "B2B销量", width: 100, visible: true },
  { key: "b2b_orders", label: "B2B订单量", width: 108, visible: true },
  { key: "ctr", label: "CTR", width: 92, visible: true },
  { key: "clicks", label: "点击", width: 92, visible: true },
  { key: "cpc", label: "CPC", width: 108, visible: true },
  { key: "ad_cost", label: "广告花费", width: 128, visible: true },
  { key: "ad_cvr", label: "广告CVR", width: 108, visible: true },
  { key: "ad_units", label: "广告销量", width: 100, visible: true },
  { key: "ad_orders", label: "广告订单量", width: 108, visible: true },
  { key: "cvr", label: "CVR", width: 92, visible: true },
  { key: "acos", label: "ACOS", width: 100, visible: true },
  { key: "sessions", label: "Session-Total", width: 132, visible: true },
])
const columnConfigOpen = ref(false)
const visibleDataColumns = computed(() => dataColumns.value.filter((column) => column.visible))
const tableStyle = computed(() => ({
  "--amazon-period-width": `${fixedColumns.value[0].width}px`,
  "--amazon-series-width": `${fixedColumns.value[1].width}px`,
  "--amazon-table-min-width": `${fixedColumns.value[0].width + fixedColumns.value[1].width + visibleDataColumns.value.reduce((sum, column) => sum + column.width, 0)}px`,
}))
const columnStyle = (column) => ({ "--amazon-column-width": `${column.width}px` })
const columnStorageKey = "ideadock.amazon-dashboard.columns.v1"
const draggedColumnKey = ref("")
let resizeCleanup = null

const displaySeries = (value) => ({
  "TN10系列（主链接）汇总": "TN10（主）",
  "TN10系列（小链接）汇总": "TN10（小）",
  "TN20系列（主链接）汇总": "TN20（主）",
}[value] || value)
const displayProduct = (value) => String(value || "")
  .replace(/系列（主链接）汇总/g, "（主）")
  .replace(/系列（小链接）汇总/g, "（小）")
  .replace(/-主链接-/g, "-主-")
  .replace(/-小链接-/g, "-小-")
  .replace(/-黑色$/g, "-黑")
  .replace(/-银色$/g, "-银")
  .replace(/-橙色$/g, "-橙")
  .replace(/TN20-主-樱桃红$/g, "TN20-主-红")

const number = (v) => v == null ? "—" : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(Number(v || 0))
const money = (v, c = currency.value) => v == null ? "—" : new Intl.NumberFormat("zh-CN", { style: "currency", currency: c || "USD", maximumFractionDigits: 2 }).format(Number(v || 0))
const percent = (v) => v == null ? "—" : `${(Number(v) * 100).toFixed(2)}%`
const normalize = (v) => v.replace(/-黑$/, "-黑色").replace(/-银$/, "-银色").replace(/-橙$/, "-橙色")

function monthEnd(value) {
  const d = toDate(value)
  return d ? new Date(d.getFullYear(), d.getMonth() + 1, 0) : null
}

function isAllowedDate(value, field) {
  const d = toDate(value)
  const today = todayDate()
  if (!d || !today || d > today) return false
  if (comparison.value === "日") return true
  if (comparison.value === "周") return field === "start" ? d.getDay() === 1 : d.getDay() === 0
  return field === "start" ? d.getDate() === 1 : d.getTime() === monthEnd(d).getTime()
}

function dateDisabled(date, field) {
  return !isAllowedDate(date, field)
}

function nearestStart(value) {
  const today = todayDate()
  let d = toDate(value) || today
  if (d > today) d = today
  if (comparison.value === "日") return d
  if (comparison.value === "周") {
    d = new Date(d)
    d.setDate(d.getDate() - ((d.getDay() + 6) % 7))
    return d
  }
  return new Date(d.getFullYear(), d.getMonth(), 1)
}

function nearestEnd(value) {
  const today = todayDate()
  let d = toDate(value) || today
  if (d > today) d = today
  if (comparison.value === "日") return d
  if (comparison.value === "周") {
    d = new Date(d)
    d.setDate(d.getDate() - ((d.getDay() + 0) % 7))
    return d
  }
  let end = monthEnd(d)
  if (end > today) end = monthEnd(new Date(d.getFullYear(), d.getMonth() - 1, 1))
  return end
}

function normalizeDateRange() {
  let start = nearestStart(startDate.value)
  let end = nearestEnd(endDate.value)
  if (start > end) {
    end = nearestEnd(start)
    if (start > end) start = nearestStart(end)
  }
  startDate.value = formatDate(start)
  endDate.value = formatDate(end)
}

function handleComparisonChange() {
  normalizeDateRange()
  load()
}

function handleDateChange() {
  normalizeDateRange()
  load()
}

const displayRows = computed(() => {
  const grouped = new Map()
  for (const row of rows.value) {
    if (!grouped.has(row.period)) grouped.set(row.period, new Map())
    const seriesMap = grouped.get(row.period)
    if (!seriesMap.has(row.series)) seriesMap.set(row.series, [])
    seriesMap.get(row.series).push(row)
  }
  const result = []
  const orderedPeriods = [...periods.value].sort((a, b) => String(b.label).localeCompare(String(a.label)))
  const visibleSeries = selectedSeries.value.length ? selectedSeries.value : seriesOptions
  for (const periodInfo of orderedPeriods) {
    const period = periodInfo.label
    const seriesMap = grouped.get(period) || new Map()
    const periodRows = []
    const periodDetails = []
    for (const series of visibleSeries) {
      const detail = seriesMap.get(series) || []
      periodDetails.push(...detail)
      const key = `${period}|${series}`
      periodRows.push({ type: "group", key, period, series, detail, expanded: expanded.value.has(key), metrics: aggregate(detail) })
      if (expanded.value.has(key)) detail.forEach((item) => periodRows.push({ type: "detail", key: `${key}|${item.product}`, period, series, product: item.product, metrics: item }))
    }
    periodRows.push({ type: "period-total", key: `${period}|total`, period, series: `${comparison.value}汇总`, metrics: aggregate(periodDetails) })
    if (periodRows.length) periodRows[0].periodFirst = true
    result.push(...periodRows)
  }
  return result
})

function aggregate(items) {
  const out = { currency: currency.value }
  for (const item of items) for (const key of ["units", "net_sales", "orders", "b2b_units", "b2b_orders", "impressions", "clicks", "ad_sales", "ad_cost", "ad_units", "ad_orders", "sessions"]) if (item[key] != null) out[key] = (out[key] || 0) + Number(item[key])
  out.acoas = out.net_sales && out.ad_cost != null ? out.ad_cost / out.net_sales : null
  out.ad_sales_share = out.units && out.ad_units != null ? out.ad_units / out.units : null
  out.ad_order_share = out.orders && out.ad_orders != null ? out.ad_orders / out.orders : null
  const weighted = (metric, weight) => {
    let numerator = 0
    let denominator = 0
    for (const item of items) {
      if (item[metric] != null && item[weight] != null && Number(item[weight]) > 0) {
        numerator += Number(item[metric]) * Number(item[weight])
        denominator += Number(item[weight])
      }
    }
    return denominator ? numerator / denominator : null
  }
  out.ctr = weighted("ctr", "impressions")
  out.cpc = weighted("cpc", "clicks")
  out.ad_cvr = weighted("ad_cvr", "clicks")
  out.cvr = weighted("cvr", "sessions")
  out.acos = weighted("acos", "ad_sales")
  return out
}

function cells(item) {
  return visibleDataColumns.value.map((column) => cellValue(item, column))
}

function cellValue(item, column) {
  const value = item[column.key]
  if (["acoas", "ad_sales_share", "ad_order_share", "ctr", "ad_cvr", "cvr", "acos"].includes(column.key)) return percent(value)
  if (["net_sales", "cpc", "ad_cost"].includes(column.key)) return money(value, item.currency)
  return number(value)
}

function toggleColumn(column) {
  column.visible = !column.visible
}

function loadColumnPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(columnStorageKey) || "null")
    if (!saved) return
    const savedColumns = saved.columns || saved
    const savedOrder = Array.isArray(saved.order) ? saved.order : []
    if (savedOrder.length) {
      dataColumns.value.sort((left, right) => {
        const leftIndex = savedOrder.indexOf(left.key)
        const rightIndex = savedOrder.indexOf(right.key)
        return (leftIndex < 0 ? savedOrder.length : leftIndex) - (rightIndex < 0 ? savedOrder.length : rightIndex)
      })
    }
    for (const column of [...fixedColumns.value, ...dataColumns.value]) {
      const preference = savedColumns[column.key]
      if (!preference) continue
      if (Number.isFinite(Number(preference.width))) column.width = Math.max(72, Math.min(360, Number(preference.width)))
      if (column.visible !== undefined && typeof preference.visible === "boolean") column.visible = preference.visible
    }
  } catch {}
}

function saveColumnPreferences() {
  try {
    const columns = Object.fromEntries([...fixedColumns.value, ...dataColumns.value].map((column) => [column.key, { width: column.width, visible: column.visible }]))
    localStorage.setItem(columnStorageKey, JSON.stringify({ order: dataColumns.value.map((column) => column.key), columns }))
  } catch {}
}

function startColumnDrag(event, column) {
  draggedColumnKey.value = column.key
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = "move"
    event.dataTransfer.setData("text/plain", column.key)
  }
}

function dropColumn(event, targetColumn) {
  event.preventDefault()
  const sourceKey = draggedColumnKey.value || event.dataTransfer?.getData("text/plain")
  if (!sourceKey || sourceKey === targetColumn.key) return
  const sourceIndex = dataColumns.value.findIndex((column) => column.key === sourceKey)
  const targetIndex = dataColumns.value.findIndex((column) => column.key === targetColumn.key)
  if (sourceIndex < 0 || targetIndex < 0) return
  const [moved] = dataColumns.value.splice(sourceIndex, 1)
  dataColumns.value.splice(targetIndex, 0, moved)
  saveColumnPreferences()
  draggedColumnKey.value = ""
}

function endColumnDrag() {
  draggedColumnKey.value = ""
}

function startResize(event, column) {
  event.preventDefault()
  event.stopPropagation()
  resizeCleanup?.()
  const startX = event.clientX
  const startWidth = column.width
  const move = (moveEvent) => {
    column.width = Math.max(72, Math.min(360, Math.round(startWidth + moveEvent.clientX - startX)))
  }
  const stop = () => {
    window.removeEventListener("pointermove", move)
    window.removeEventListener("pointerup", stop)
    document.body.classList.remove("resizing-amazon-column")
    resizeCleanup = null
    saveColumnPreferences()
  }
  resizeCleanup = stop
  document.body.classList.add("resizing-amazon-column")
  window.addEventListener("pointermove", move)
  window.addEventListener("pointerup", stop, { once: true })
}

function toggle(row) {
  const next = new Set(expanded.value)
  next.has(row.key) ? next.delete(row.key) : next.add(row.key)
  expanded.value = next
}

async function loadRuntime() {
  try { const response = await fetch("./ideadock.runtime.json", { cache: "no-store" }); apiBase.value = String((await response.json()).backend_base_url || "").replace(/\/$/, "") } catch { apiBase.value = "http://127.0.0.1:8000" }
}

async function load() {
  if (!startDate.value || !endDate.value || startDate.value > endDate.value) { error.value = "请选择有效日期范围"; return }
  loading.value = true; error.value = ""
  try {
    const query = new URLSearchParams({ comparison: comparison.value, start_date: startDate.value, end_date: endDate.value, site: site.value })
    selectedSeries.value.forEach((v) => query.append("series", v))
    selectedProducts.value.forEach((v) => query.append("products", normalize(v)))
    const response = await fetchWithDashboardAuth(`${apiBase.value}/api/amazon/dashboard?${query}`)
    const raw = await response.text()
    let data = {}
    try { data = raw ? JSON.parse(raw) : {} } catch { throw new Error(raw || `HTTP ${response.status}`) }
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
    currency.value = data.currency || "USD"; rows.value = data.rows || []; periods.value = data.periods || []; expanded.value = new Set()
  } catch (e) { error.value = e.message || "数据加载失败" } finally { loading.value = false }
}

async function loadSites() { try { const response = await fetchWithDashboardAuth(`${apiBase.value}/api/amazon/stores`); const data = await response.json(); const values = (data.stores || []).filter((x) => x.status === 1).map((x) => x.country).filter(Boolean); if (values.length) sites.value = [...new Set(values)]; } catch {} }
watch([fixedColumns, dataColumns], saveColumnPreferences, { deep: true })
onMounted(async () => { loadColumnPreferences(); normalizeDateRange(); await loadRuntime(); await loadSites(); await load() })
onBeforeUnmount(() => resizeCleanup?.())
</script>

<template>
  <div class="amazon-page">
    <div class="amazon-page-head"><div><div class="breadcrumb"><span>数据中心</span><i>/</i><strong>广告分析</strong></div><h1>数据看板</h1><p>按系列和产品查看销售、流量及广告核心指标。</p></div><span class="period-badge">{{ startDate }} 至 {{ endDate }}</span></div>
    <section class="amazon-filter-bar">
      <label><span>数据对比（日/周/月）</span><el-select v-model="comparison" @change="handleComparisonChange"><el-option label="日" value="日"/><el-option label="周" value="周"/><el-option label="月" value="月"/></el-select></label>
      <label><span>开始日期（选择）</span><el-date-picker v-model="startDate" type="date" value-format="YYYY-MM-DD" format="YYYY/MM/DD" :clearable="false" :disabled-date="(date) => dateDisabled(date, 'start')" @change="handleDateChange"/></label>
      <label><span>结束日期（选择）</span><el-date-picker v-model="endDate" type="date" value-format="YYYY-MM-DD" format="YYYY/MM/DD" :clearable="false" :disabled-date="(date) => dateDisabled(date, 'end')" @change="handleDateChange"/></label>
      <label><span>站点（单选）</span><el-select v-model="site" @change="load"><el-option v-for="item in sites" :key="item" :label="item" :value="item"/></el-select></label>
      <label><span>系列（多选）</span><el-select v-model="selectedSeries" multiple collapse-tags collapse-tags-tooltip placeholder="全部系列" @change="load"><el-option v-for="item in seriesOptions" :key="item" :label="displaySeries(item)" :value="item"/></el-select></label>
      <label><span>产品（多选）</span><el-select v-model="selectedProducts" multiple collapse-tags collapse-tags-tooltip placeholder="全部产品" @change="load"><el-option v-for="item in productOptions" :key="item" :label="displayProduct(item)" :value="item"/></el-select></label>
    </section>
    <section class="amazon-table-panel"><div class="amazon-panel-head"><div><span class="section-label">产品经营数据</span><h2>系列与产品汇总</h2></div><div class="amazon-panel-actions"><small>全部系列 · 全部产品 · {{ site }} · {{ comparison }}汇总</small><button class="column-config-button" type="button" @click="columnConfigOpen = !columnConfigOpen" :aria-expanded="columnConfigOpen" aria-controls="amazon-column-config"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16M4 12h16M4 19h16"/><circle cx="8" cy="5" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="10" cy="19" r="2"/></svg><span>列配置</span></button><div v-if="columnConfigOpen" id="amazon-column-config" class="column-config-panel" role="dialog" aria-label="列配置"><div class="column-config-title"><strong>列配置</strong><span>可隐藏或显示数据列</span></div><div class="column-config-list"><button v-for="column in dataColumns" :key="column.key" type="button" class="column-config-item" @click="toggleColumn(column)"><span>{{ column.label }}</span><svg viewBox="0 0 24 24" :class="{ 'is-hidden': !column.visible }" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/><path v-if="!column.visible" d="m4 4 16 16"/></svg></button></div></div></div></div>
      <div v-if="error" class="amazon-error">数据加载失败：{{ error }}</div><div v-else-if="loading" class="amazon-loading" role="status" aria-live="polite"><span class="amazon-loading-spinner" aria-hidden="true"></span><span>从领星同步数据...</span></div>
      <div v-else class="amazon-table-wrap"><table class="amazon-table" :style="tableStyle"><thead><tr><th :style="columnStyle(fixedColumns[0])"><span>{{ fixedColumns[0].label }}</span><i class="column-resize-handle" role="separator" aria-orientation="vertical" title="拖动调整列宽" @pointerdown="startResize($event, fixedColumns[0])"></i></th><th :style="columnStyle(fixedColumns[1])"><span>{{ fixedColumns[1].label }}</span><i class="column-resize-handle" role="separator" aria-orientation="vertical" title="拖动调整列宽" @pointerdown="startResize($event, fixedColumns[1])"></i></th><th v-for="column in visibleDataColumns" :key="column.key" :style="columnStyle(column)" @dragover.prevent @drop="dropColumn($event, column)"><span draggable="true" :class="{ 'column-dragging': draggedColumnKey === column.key }" @dragstart="startColumnDrag($event, column)" @dragend="endColumnDrag">{{ column.label }}</span><i class="column-resize-handle" role="separator" aria-orientation="vertical" :title="`拖动调整${column.label}列宽`" @pointerdown="startResize($event, column)"></i></th></tr></thead><tbody><tr v-for="row in displayRows" :key="row.key" :class="row.type"><td class="period-cell" :class="{ 'period-blank': !row.periodFirst }" :style="columnStyle(fixedColumns[0])">{{ row.periodFirst ? row.period : '' }}</td><td class="series-cell" :style="columnStyle(fixedColumns[1])"><span class="series-content"><button v-if="row.type === 'group'" class="amazon-toggle" @click="toggle(row)" :aria-label="`${row.expanded ? '收起' : '展开'}${displaySeries(row.series)}`">{{ row.expanded ? '−' : '+' }}</button><span v-else-if="row.type === 'detail'" class="tree-branch">└</span><span>{{ row.type === 'detail' ? displayProduct(row.product) : displaySeries(row.series) }}</span></span></td><td v-for="column in visibleDataColumns" :key="column.key" :style="columnStyle(column)">{{ cellValue(row.metrics, column) }}</td></tr><tr v-if="!displayRows.length"><td :colspan="2 + visibleDataColumns.length" class="amazon-empty">当前筛选范围暂无匹配数据</td></tr></tbody></table></div>
    </section>
  </div>
</template>
