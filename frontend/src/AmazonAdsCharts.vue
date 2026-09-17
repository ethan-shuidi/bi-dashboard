<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { init } from "echarts/core"
import { fetchWithDashboardAuth } from "./dashboardAuth"
import WeekPicker from "./WeekPicker.vue"

const props = defineProps({ apiBase: { type: String, default: "" } })

const siteOptions = ["全部站点", "美国", "日本", "德国", "英国", "法国", "加拿大", "澳洲", "西班牙", "意大利", "荷兰", "比利时", "墨西哥", "爱尔兰", "波兰", "瑞典"]
const modelOptions = ["TN10", "TN20"]
const site = ref("全部站点")
const model = ref("TN10")
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

function monday(value) {
  const date = value ? new Date(`${value}T00:00:00`) : new Date()
  date.setHours(0, 0, 0, 0)
  date.setDate(date.getDate() - ((date.getDay() + 6) % 7))
  return date
}
function isoDate(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`
}
const lastWeek = monday()
lastWeek.setDate(lastWeek.getDate() - 7)
const endWeek = ref(isoDate(lastWeek))
const rangeStart = monday(lastWeek)
rangeStart.setDate(rangeStart.getDate() - 49)
const startWeek = ref(isoDate(rangeStart))

const rows = ref([])
const currency = ref("USD")
const fieldAvailability = ref(null)
const weekLabels = computed(() => rows.value.map((row) => `${row.period_start.slice(5)}~${row.period_end.slice(5)}`))

const compactNumber = (value) => new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value || 0))
const money = (value) => `${currency.value} ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Number(value || 0))}`
const percent = (value) => `${(Number(value || 0) * 100).toFixed(2)}%`

async function load({ refresh = false } = {}) {
  if (!props.apiBase) return
  loading.value = true
  error.value = ""
  try {
    const query = new URLSearchParams({ start_week: startWeek.value, end_week: endWeek.value, site: site.value, model: model.value })
    if (refresh) query.set("refresh", "true")
    const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/ads-charts?${query}`)
    const body = await response.json()
    if (!response.ok) throw new Error(body.detail || `请求失败：HTTP ${response.status}`)
    data.value = body
    rows.value = body.rows || []
    currency.value = body.currency || "USD"
    fieldAvailability.value = body.field_availability || null
    await nextTick()
    renderCharts()
  } catch (e) {
    error.value = e.message || "广告图表加载失败"
    rows.value = []
    fieldAvailability.value = null
    await nextTick()
    renderCharts()
  } finally {
    loading.value = false
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

function renderSalesChart() {
  if (!salesChartElement.value) return
  salesChart ||= init(salesChartElement.value)
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

function renderConversionChart() {
  if (!conversionChartElement.value) return
  conversionChart ||= init(conversionChartElement.value)
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

function renderTrafficChart() {
  if (!trafficChartElement.value) return
  trafficChart ||= init(trafficChartElement.value)
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

function renderCharts() {
  renderSalesChart()
  renderConversionChart()
  renderTrafficChart()
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
      <label><span>开始周</span><WeekPicker v-model="startWeek" /></label>
      <label><span>结束周</span><WeekPicker v-model="endWeek" /></label>
      <label><span>站点</span><el-select v-model="site"><el-option v-for="item in siteOptions" :key="item" :label="item" :value="item" /></el-select></label>
      <label><span>型号</span><el-select v-model="model"><el-option v-for="item in modelOptions" :key="item" :label="item" :value="item" /></el-select></label>
    </div>
    <div v-if="error" class="ads-chart-error">{{ error }}</div>
    <div v-else-if="loading && !rows.length" class="ads-chart-loading">正在同步领星产品表现数据…</div>
    <div v-else-if="!rows.length" class="ads-chart-empty">当前周度范围暂无数据</div>
    <div v-else class="ads-chart-grid">
      <article>
        <header><span class="ads-chart-icon money" aria-hidden="true">￥</span><div><strong>销售看板</strong><small>销售额 · 广告销售额 · 费比</small></div></header>
        <div ref="salesChartElement" class="ads-chart-canvas"></div>
      </article>
      <article>
        <header><span class="ads-chart-icon click" aria-hidden="true">➤</span><div><strong>点击与转化</strong><small>点击数 · 广告转化率</small></div></header>
        <div ref="conversionChartElement" class="ads-chart-canvas"></div>
      </article>
      <article>
        <header><span class="ads-chart-icon traffic" aria-hidden="true">◎</span><div><strong>流量看板</strong><small>Sessions-Total · PV-Total</small></div></header>
        <div ref="trafficChartElement" class="ads-chart-canvas"></div>
        <p v-if="fieldAvailability && !fieldAvailability.page_views_present" class="ads-chart-missing">领星未返回 PV-Total，已保留空值，未用其他流量替代。</p>
      </article>
    </div>
  </section>
</template>

<style scoped>
.ads-chart-module{position:relative;z-index:1;margin-top:20px;padding:18px;border:1px solid var(--line);border-radius:18px;background:#fff;box-shadow:var(--shadow-sm)}
.ads-chart-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.ads-chart-head span{color:#73849a;font-size:12px;font-weight:750}.ads-chart-head h2{margin:3px 0 0;color:#132f5b;font-size:18px}.ads-chart-head p{margin:5px 0 0;color:#73849a;font-size:12px}.ads-chart-head button{height:36px;padding:0 14px;border:0;border-radius:9px;background:#1e57c8;color:#fff;font:inherit;font-size:13px;font-weight:700;cursor:pointer}.ads-chart-head button:disabled{opacity:.65;cursor:not-allowed}
.ads-chart-filters{display:grid;grid-template-columns:repeat(4,minmax(170px,1fr));gap:12px;margin-top:16px;padding:14px;border:0;border-radius:12px;background:#f7fbff}.ads-chart-filters label{display:grid;gap:6px;min-width:0}.ads-chart-filters span{color:#5f7188;font-size:12px;font-weight:750}
.ads-chart-error,.ads-chart-loading,.ads-chart-empty{margin-top:16px;padding:34px 16px;border-radius:12px;text-align:center;color:#75869c;background:#f8fbff}
.ads-chart-error{color:#b52e45;background:#fff4f6}
.ads-chart-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:16px}.ads-chart-grid article{position:relative;min-width:0;padding:14px;border:1px solid #e5edf7;border-radius:14px;background:#fff}.ads-chart-grid header{display:flex;align-items:center;gap:10px;margin-bottom:8px}.ads-chart-grid header div{display:grid;min-width:0}.ads-chart-grid strong{color:#173d70;font-size:14px}.ads-chart-grid small{overflow:hidden;color:#75869c;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.ads-chart-icon{display:grid;width:34px;height:34px;flex:0 0 34px;place-items:center;border-radius:10px;font-size:16px;font-weight:800}.ads-chart-icon.money{color:#1d4ed8;background:#e8f1ff}.ads-chart-icon.click{color:#b45309;background:#fff4df}.ads-chart-icon.traffic{color:#7c3aed;background:#f2ecff}.ads-chart-canvas{width:100%;height:310px}.ads-chart-missing{position:absolute;right:14px;bottom:14px;left:14px;margin:0;padding:7px 10px;border-radius:8px;background:rgba(255,247,235,.94);color:#a05a00;font-size:11px;text-align:center}
@media (max-width:1500px){.ads-chart-grid{grid-template-columns:1fr}.ads-chart-filters{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:720px){.ads-chart-filters{grid-template-columns:1fr}}
</style>
