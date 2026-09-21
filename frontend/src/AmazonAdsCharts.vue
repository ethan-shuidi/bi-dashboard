<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { fetchWithDashboardAuth } from "./dashboardAuth"
import DashboardState from "./DashboardState.vue"
import WeekPicker from "./WeekPicker.vue"

const props = defineProps({ apiBase: { type: String, default: "" } })

const siteOptions = ["全部站点", "美国", "日本", "德国", "英国", "法国", "加拿大", "澳洲", "西班牙", "意大利", "荷兰", "比利时", "墨西哥", "爱尔兰", "波兰", "瑞典"]
const modelOptions = [
  { label: "全部型号", value: "ALL" },
  { label: "TN10", value: "TN10" },
  { label: "TN20", value: "TN20" },
]
const quickRangeOptions = [
  { label: "前5周", value: 5 },
  { label: "前10周", value: 10 },
  { label: "前15周", value: 15 },
]
const site = ref("全部站点")
const model = ref("TN10")
const quickRange = ref(5)
const loading = ref(false)
const error = ref("")
const data = ref(null)
const salesChartElement = ref(null)
const conversionChartElement = ref(null)
const trafficChartElement = ref(null)
let salesChart
let conversionChart
let trafficChart
let resizeObserver
let chartRequestSeq = 0
let eChartsLoader

function ensureECharts() {
  eChartsLoader ??= Promise.all([
    import("echarts/core"),
    import("echarts/charts"),
    import("echarts/components"),
    import("echarts/renderers"),
  ]).then(([{ init, use }, charts, components, renderers]) => {
    use([
      charts.BarChart,
      charts.LineChart,
      components.GridComponent,
      components.TooltipComponent,
      components.LegendComponent,
      components.AriaComponent,
      renderers.CanvasRenderer,
    ])
    return init
  })
  return eChartsLoader
}

function parseDate(value) {
  if (!value) return new Date()
  if (value instanceof Date) return new Date(value)
  const parsed = new Date(`${value}T00:00:00`)
  return parsed
}

function monday(value) {
  const date = parseDate(value)
  date.setHours(0, 0, 0, 0)
  date.setDate(date.getDate() - ((date.getDay() + 6) % 7))
  return date
}
function isoDate(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`
}
function addDays(value, amount) {
  const date = new Date(value)
  date.setDate(date.getDate() + amount)
  return date
}
function isoWeek(value) {
  const start = monday(value)
  const thursday = addDays(start, 3)
  const firstThursday = new Date(thursday.getFullYear(), 0, 4)
  const firstMonday = monday(firstThursday)
  return Math.floor((start - firstMonday) / 604800000) + 1
}
const endWeek = ref(isoDate(addDays(monday(), -7)))
const startWeek = ref(isoDate(addDays(monday(), -35)))

const rows = ref([])
const currency = ref("USD")
const fieldAvailability = ref(null)
const weekLabels = computed(() => rows.value.map((row) => row.week_label || `W${String(isoWeek(row.period_start)).padStart(2, "0")}`))

const compactNumber = (value) => new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value || 0))
const money = (value) => `${currency.value} ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Number(value || 0))}`
const percent = (value) => `${(Number(value || 0) * 100).toFixed(2)}%`
const fixedDecimal = (value) => new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value || 0))

function formatChartTooltipValue(seriesName, value) {
  if (seriesName === "销售额" || seriesName === "广告销售额") return fixedDecimal(value)
  if (seriesName === "费比" || seriesName === "广告转化率") return percent(value)
  return value
}

function chartTooltipFormatter(params) {
  const items = Array.isArray(params) ? params : [params]
  if (!items.length) return ""
  const lines = items.map((item) => `${item.marker} ${item.seriesName}: ${formatChartTooltipValue(item.seriesName, item.value)}`)
  return `${items[0].name}<br/>${lines.join("<br/>")}`
}

function requestErrorMessage(body, status) {
  const detail = body?.detail
  if (typeof detail === "string" && detail) return detail
  if (Array.isArray(detail)) {
    const first = detail[0] || {}
    const field = Array.isArray(first.loc) ? first.loc.filter((item) => typeof item === "string").join(".") : ""
    const message = first.msg || first.message
    if (message) return field ? `${field}: ${message}` : String(message)
  } else if (detail && typeof detail === "object") {
    const message = detail.message || detail.msg
    if (message) return String(message)
  }
  return `请求失败：HTTP ${status}`
}

function applyQuickRange(weekCount) {
  const end = addDays(monday(), -7)
  endWeek.value = isoDate(end)
  startWeek.value = isoDate(addDays(end, -(weekCount - 1) * 7))
}

function syncQuickRange() {
  const currentWeek = monday()
  const completedWeek = addDays(currentWeek, -7)
  const matched = quickRangeOptions.find(({ value }) => startWeek.value === isoDate(addDays(completedWeek, -(value - 1) * 7)) && endWeek.value === isoDate(completedWeek))
  quickRange.value = matched?.value ?? null
}

async function load({ refresh = false } = {}) {
  if (!props.apiBase) return
  const requestSeq = ++chartRequestSeq
  loading.value = true
  error.value = ""
  try {
    const query = new URLSearchParams({ start_week: startWeek.value, end_week: endWeek.value, site: site.value, model: model.value })
    if (refresh) query.set("refresh", "true")
    const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/ads-charts?${query}`)
    const body = await response.json()
    if (!response.ok) throw new Error(requestErrorMessage(body, response.status))
    if (requestSeq !== chartRequestSeq) return
    data.value = body
    rows.value = body.rows || []
    currency.value = body.currency || "USD"
    fieldAvailability.value = body.field_availability || null
    await nextTick()
    await renderCharts()
  } catch (e) {
    if (requestSeq !== chartRequestSeq) return
    error.value = e.message || "广告图表加载失败"
    rows.value = []
    fieldAvailability.value = null
    await nextTick()
    await renderCharts()
  } finally {
    if (requestSeq === chartRequestSeq) loading.value = false
  }
}

function baseOption(ariaText) {
  return {
    aria: { enabled: true, description: ariaText },
    animationDuration: 350,
    grid: { left: 14, right: 18, top: 54, bottom: 18, containLabel: true },
    legend: { top: 4, right: 0, itemWidth: 16, itemHeight: 8, textStyle: { color: "#5d7188", fontSize: 12 } },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(15, 35, 68, .94)",
      borderWidth: 0,
      textStyle: { color: "#fff", fontSize: 12 },
      formatter: chartTooltipFormatter,
    },
    xAxis: {
      type: "category",
      data: weekLabels.value,
      axisLine: { lineStyle: { color: "#dbe6f3" } },
      axisTick: { show: false },
      axisLabel: { color: "#67788f", fontSize: 10, hideOverlap: true },
    },
  }
}

function yAxis(label, options = {}) {
  return {
    type: "value",
    name: label,
    nameTextStyle: { color: "#67788f", fontSize: 11 },
    splitLine: { lineStyle: { color: "#e9eff7", type: "dashed" } },
    axisLabel: { color: "#67788f", fontSize: 11, formatter: (value) => compactNumber(value) },
    ...options,
  }
}

function ensureChart(chart, element, init) {
  if (!chart || chart.getDom() !== element) {
    chart?.dispose()
    return init(element)
  }
  return chart
}

function renderSalesChart(init) {
  if (!salesChartElement.value) return
  salesChart = ensureChart(salesChart, salesChartElement.value, init)
  salesChart.setOption({
    ...baseOption("按周展示销售额、广告销售额和费比。"),
    color: ["#3b82f6", "#22c55e", "#f97316"],
    yAxis: [
      yAxis(`销售额 (${currency.value})`),
      yAxis("费比", { splitLine: { show: false }, axisLabel: { color: "#67788f", fontSize: 11, formatter: (value) => `${Math.round(value * 100)}%` } }),
    ],
    series: [
      { name: "销售额", type: "bar", barMaxWidth: 20, data: rows.value.map((row) => row.net_sales), itemStyle: { borderRadius: [5, 5, 0, 0] } },
      { name: "广告销售额", type: "bar", barMaxWidth: 20, data: rows.value.map((row) => row.ad_sales), itemStyle: { borderRadius: [5, 5, 0, 0] } },
      { name: "费比", type: "line", yAxisIndex: 1, smooth: true, symbolSize: 6, data: rows.value.map((row) => row.fee_ratio), lineStyle: { width: 3 } },
    ],
  }, true)
}

function renderConversionChart(init) {
  if (!conversionChartElement.value) return
  conversionChart = ensureChart(conversionChart, conversionChartElement.value, init)
  conversionChart.setOption({
    ...baseOption("按周展示点击数和广告转化率。"),
    color: ["#6366f1", "#f59e0b"],
    yAxis: [
      yAxis("点击数", { minInterval: 1 }),
      yAxis("广告转化率", { splitLine: { show: false }, axisLabel: { color: "#67788f", fontSize: 11, formatter: (value) => `${Math.round(value * 100)}%` } }),
    ],
    series: [
      { name: "点击数", type: "bar", barMaxWidth: 24, data: rows.value.map((row) => row.clicks), itemStyle: { borderRadius: [5, 5, 0, 0] } },
      { name: "广告转化率", type: "line", yAxisIndex: 1, smooth: true, symbolSize: 6, data: rows.value.map((row) => row.ad_cvr), lineStyle: { width: 3 } },
    ],
  }, true)
}

function renderTrafficChart(init) {
  if (!trafficChartElement.value) return
  trafficChart = ensureChart(trafficChart, trafficChartElement.value, init)
  trafficChart.setOption({
    ...baseOption("按周展示领星 Sessions-Total 和 PV-Total。"),
    color: ["#0ea5e9", "#a855f7"],
    yAxis: yAxis("流量", { minInterval: 1 }),
    series: [
      { name: "Sessions", type: "line", smooth: true, symbolSize: 6, data: rows.value.map((row) => row.sessions), lineStyle: { width: 3 }, areaStyle: { opacity: 0.08 } },
      { name: "PV", type: "line", smooth: true, symbolSize: 6, data: rows.value.map((row) => row.page_views), lineStyle: { width: 3 }, areaStyle: { opacity: 0.08 } },
    ],
  }, true)
}

async function renderCharts() {
  if (!rows.value.length) return
  const init = await ensureECharts()
  renderSalesChart(init)
  renderConversionChart(init)
  renderTrafficChart(init)
}

onMounted(async () => {
  if (typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(() => {
      salesChart?.resize()
      conversionChart?.resize()
      trafficChart?.resize()
    })
  }
  await nextTick()
  ;[salesChartElement.value, conversionChartElement.value, trafficChartElement.value].filter(Boolean).forEach((element) => resizeObserver?.observe(element))
  await load()
})

watch(startWeek, (value) => {
  if (value && endWeek.value && value > endWeek.value) endWeek.value = value
  syncQuickRange()
})
watch(endWeek, (value) => {
  if (value && startWeek.value && value < startWeek.value) startWeek.value = value
  syncQuickRange()
})
watch(() => [props.apiBase, startWeek.value, endWeek.value, site.value, model.value], () => load())

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  salesChart?.dispose()
  conversionChart?.dispose()
  trafficChart?.dispose()
})
</script>

<template>
  <section class="ads-chart-module" aria-label="广告周度图表">
    <div class="ads-chart-head">
      <div>
        <span>广告后台数据</span>
        <h2>广告周度图表</h2>
        <p>按周查看销售、点击转化和流量趋势；型号自动合并对应主/小链接。</p>
      </div>
      <button type="button" :disabled="loading" @click="load({ refresh: true })">{{ loading ? "同步中…" : "刷新" }}</button>
    </div>
    <div class="ads-chart-filters">
      <label><span>快速选择</span><el-select v-model="quickRange" placeholder="自定义范围" @change="applyQuickRange"><el-option v-for="item in quickRangeOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select></label>
      <label><span>开始周</span><WeekPicker v-model="startWeek" /></label>
      <label><span>结束周</span><WeekPicker v-model="endWeek" /></label>
      <label><span>站点</span><el-select v-model="site"><el-option v-for="item in siteOptions" :key="item" :label="item" :value="item" /></el-select></label>
      <label><span>型号</span><el-select v-model="model"><el-option v-for="item in modelOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select></label>
    </div>
    <DashboardState v-if="error" class="ads-chart-state" state="error" title="广告周度图表加载失败" :message="error" />
    <DashboardState v-else-if="loading && !rows.length" class="ads-chart-state" state="loading" title="正在同步领星产品表现数据" message="正在读取产品表现与广告指标，请稍候。" />
    <DashboardState v-else-if="!rows.length" class="ads-chart-state" state="empty" title="当前周度范围暂无数据" message="可调整开始周、结束周、站点或型号后重新加载。" />
    <div v-else class="ads-chart-grid">
      <article>
        <header><span class="ads-chart-icon money" aria-hidden="true"><svg viewBox="0 0 24 24" role="presentation"><path d="M12 3.75a1 1 0 0 1 .97.757L13.45 7h2.3a1 1 0 1 1 0 2h-1.85l-.65 3h1.75a1 1 0 1 1 0 2h-2.18l-.48 2.24a1 1 0 0 1-1.955-.21 1 1 0 0 1 0-.21L10.88 14H8.7l-.48 2.24a1 1 0 0 1-1.955-.21 1 1 0 0 1 0-.21L6.28 14H5a1 1 0 1 1 0-2h1.68l.65-3H5.75a1 1 0 1 1 0-2h2.05l.48-2.24a1 1 0 0 1 1.955.42L10.12 7h2.18l.35-1.49A1 1 0 0 1 12 3.75ZM9.78 9l-.65 3h2.18l.65-3H9.78Z"/></svg></span><div><strong>销售看板</strong><small>销售额 · 广告销售额 · 费比</small></div></header>
        <div ref="salesChartElement" class="ads-chart-canvas"></div>
      </article>
      <article>
        <header><span class="ads-chart-icon click" aria-hidden="true"><svg viewBox="0 0 24 24" role="presentation"><path d="M12 2.25a9.75 9.75 0 1 0 0 19.5 9.75 9.75 0 0 0 0-19.5Zm0 2a7.75 7.75 0 1 1 0 15.5 7.75 7.75 0 0 1 0-15.5Zm3.43 4.02-6.68 2.76c-.78.32-.74 1.45.06 1.71l2.4.78.82 2.32c.27.77 1.37.8 1.68.05l2.7-6.51c.3-.72-.49-1.45-1.2-1.16l.22-.95Z"/></svg></span><div><strong>点击与转化</strong><small>点击数 · 广告转化率</small></div></header>
        <div ref="conversionChartElement" class="ads-chart-canvas"></div>
      </article>
      <article>
        <header><span class="ads-chart-icon traffic" aria-hidden="true"><svg viewBox="0 0 24 24" role="presentation"><path d="M12 4.75a7.25 7.25 0 1 0 0 14.5 7.25 7.25 0 0 0 0-14.5Zm0 2c.7 0 1.36.14 1.97.4a2.68 2.68 0 0 1 2.5 4.29A5.25 5.25 0 0 1 12 17.25c-.93 0-1.8-.24-2.56-.66a2.68 2.68 0 0 1 3.51-3.96 2.68 2.68 0 0 1 1.28-3.7A5.23 5.23 0 0 0 8.1 10.9a2.68 2.68 0 0 1 .67 4.86 5.21 5.21 0 0 1-.77-2.76c0-.85.2-1.65.56-2.36a2.68 2.68 0 0 1 3.4-3.78c.01-.04.03-.08.04-.11Zm5.25.25a7.25 7.25 0 0 0-4.3-1.42 2.68 2.68 0 1 1 4.3 1.42Z"/></svg></span><div><strong>流量看板</strong><small>Sessions-Total · PV-Total</small></div></header>
        <div ref="trafficChartElement" class="ads-chart-canvas"></div>
        <p v-if="fieldAvailability && !fieldAvailability.page_views_present" class="ads-chart-missing">领星未返回 PV-Total，已保留空值，未用其他流量替代。</p>
      </article>
    </div>
  </section>
</template>

<style scoped>
.ads-chart-module{position:relative;z-index:1;margin-top:20px;padding:18px;border:1px solid var(--line);border-radius:18px;background:#fff;box-shadow:var(--shadow-sm)}
.ads-chart-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.ads-chart-head span{color:#73849a;font-size:12px;font-weight:750}.ads-chart-head h2{margin:3px 0 0;color:#132f5b;font-size:18px}.ads-chart-head p{margin:5px 0 0;color:#73849a;font-size:12px}.ads-chart-head button{height:36px;padding:0 14px;border:0;border-radius:9px;background:#1e57c8;color:#fff;font:inherit;font-size:13px;font-weight:700;cursor:pointer}.ads-chart-head button:disabled{opacity:.65;cursor:not-allowed}
.ads-chart-filters{display:grid;grid-template-columns:repeat(5,minmax(136px,1fr));gap:12px;margin-top:16px;padding:14px;border:0;border-radius:12px;background:#f7fbff}.ads-chart-filters label{display:grid;gap:6px;min-width:0}.ads-chart-filters span{color:#5f7188;font-size:12px;font-weight:750}
.ads-chart-state{margin-top:16px}
.ads-chart-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:16px}.ads-chart-grid article{position:relative;min-width:0;padding:14px;border:1px solid #e5edf7;border-radius:14px;background:#fff}.ads-chart-grid header{display:flex;align-items:center;gap:10px;margin-bottom:8px}.ads-chart-grid header div{display:grid;min-width:0}.ads-chart-grid strong{color:#173d70;font-size:14px}.ads-chart-grid small{overflow:hidden;color:#75869c;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.ads-chart-icon{display:grid;width:34px;height:34px;flex:0 0 34px;place-items:center;border-radius:10px}.ads-chart-icon svg{width:18px;height:18px;fill:currentColor}.ads-chart-icon.money{color:#1d4ed8;background:#e8f1ff}.ads-chart-icon.click{color:#b45309;background:#fff4df}.ads-chart-icon.traffic{color:#7c3aed;background:#f2ecff}.ads-chart-canvas{width:100%;height:310px}.ads-chart-missing{position:absolute;right:14px;bottom:14px;left:14px;margin:0;padding:7px 10px;border-radius:8px;background:rgba(255,247,235,.94);color:#a05a00;font-size:11px;text-align:center}
@media (max-width:1500px){.ads-chart-grid{grid-template-columns:1fr}.ads-chart-filters{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:720px){.ads-chart-filters{grid-template-columns:1fr}}
</style>
