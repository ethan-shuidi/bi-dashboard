<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { init, use } from "echarts/core"
import { BarChart, LineChart } from "echarts/charts"
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components"
import { CanvasRenderer } from "echarts/renderers"

use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, AriaComponent, CanvasRenderer])

const apiBase = ref("")
const loading = ref(true)
const syncing = ref(false)
const error = ref("")
const notice = ref("")
const dateError = ref("")
const status = ref(null)
const dashboard = ref(null)
const period = ref("30")
const store = ref("")
const salesChartElement = ref(null)
const productChartElement = ref(null)

const isoDate = (value) => [value.getFullYear(), String(value.getMonth() + 1).padStart(2, "0"), String(value.getDate()).padStart(2, "0")].join("-")
const today = new Date()
const monthAgo = new Date(today)
monthAgo.setDate(monthAgo.getDate() - 29)
const customStart = ref(isoDate(monthAgo))
const customEnd = ref(isoDate(today))

let salesChart
let productChart
let resizeObserver

const fulfillmentLabels = {
  FULFILLED: "已发货",
  PARTIALLY_FULFILLED: "部分发货",
  UNFULFILLED: "待发货",
  ON_HOLD: "暂停发货",
  SCHEDULED: "已排期",
  IN_PROGRESS: "处理中",
  RESTOCKED: "已补货",
  UNKNOWN: "未知",
}

const money = (value, digits = 0) => new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: dashboard.value?.currency || "USD",
  maximumFractionDigits: digits,
}).format(Number(value || 0))
const number = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0))
const percent = (value) => `${Number(value || 0).toFixed(1)}%`
const changeText = (value) => {
  if (value === null || value === undefined) return "上期无可比数据"
  if (value === 0) return "与上期持平"
  return `${value > 0 ? "+" : ""}${value}% 较上期`
}
const changeClass = (value) => value > 0 ? "up" : value < 0 ? "down" : "flat"

const metricCards = computed(() => {
  const totals = dashboard.value?.totals
  if (!totals) return []
  return [
    { label: "净销售额", value: money(totals.net_sales), change: totals.changes.net_sales, tone: "cyan", icon: "M4 17l5-5 4 4 7-9M14 7h6v6" },
    { label: "订单总量", value: number(totals.orders), change: totals.changes.orders, tone: "violet", icon: "M6 7h12l-1 13H7L6 7Zm3 0V5a3 3 0 016 0v2" },
    { label: "商品销量", value: number(totals.units), change: totals.changes.units, tone: "emerald", icon: "M4 8l8-4 8 4-8 4-8-4Zm0 0v8l8 4 8-4V8M12 12v8" },
    { label: "退款金额", value: money(totals.refunds), change: totals.changes.refunds, tone: "gold", icon: "M4 10a8 8 0 101.8-5M4 4v6h6M9 9h6m-3-3v6" },
  ]
})

const fulfillmentTotal = computed(() => (dashboard.value?.fulfillment || []).reduce((sum, item) => sum + item.orders, 0))
const fulfilledOrders = computed(() => (dashboard.value?.fulfillment || []).filter((item) => item.status === "FULFILLED").reduce((sum, item) => sum + item.orders, 0))
const fulfillmentRate = computed(() => fulfillmentTotal.value ? fulfilledOrders.value / fulfillmentTotal.value * 100 : 0)
const averageOrderValue = computed(() => dashboard.value?.totals?.orders ? dashboard.value.totals.net_sales / dashboard.value.totals.orders : 0)
const refundRate = computed(() => dashboard.value?.totals?.sales ? dashboard.value.totals.refunds / dashboard.value.totals.sales * 100 : 0)
const topSku = computed(() => dashboard.value?.sku_breakdown?.[0] || null)
const abnormalOrders = computed(() => dashboard.value?.abnormal_orders || [])
const commercePulse = computed(() => [
  { label: "平均客单价", value: money(averageOrderValue.value, 2), note: "每笔订单净销售", tone: "cyan" },
  { label: "退款率", value: percent(refundRate.value), note: "退款金额 / 销售额", tone: refundRate.value > 5 ? "gold" : "emerald" },
  { label: "履约完成率", value: percent(fulfillmentRate.value), note: `${number(fulfilledOrders.value)} / ${number(fulfillmentTotal.value)} 单`, tone: "violet" },
  { label: "销量最高商品", value: topSku.value?.sku || "—", note: topSku.value ? `${topSku.value.color || "默认款"} · ${number(topSku.value.units)} 件` : "暂无商品数据", tone: "gold" },
])
const maxFulfillment = computed(() => Math.max(...(dashboard.value?.fulfillment || []).map((row) => row.orders), 1))
const periodSales = computed(() => (dashboard.value?.daily || []).reduce((sum, item) => sum + item.sales, 0))
const peakDay = computed(() => (dashboard.value?.daily || []).reduce((peak, item) => !peak || item.sales > peak.sales ? item : peak, null))
const periodLabel = computed(() => dashboard.value ? `${dashboard.value.period.start} 至 ${dashboard.value.period.end}` : "—")

function renderSalesChart() {
  if (!salesChartElement.value || !dashboard.value?.daily?.length) return
  if (!salesChart || salesChart.getDom() !== salesChartElement.value) {
    salesChart?.dispose()
    salesChart = init(salesChartElement.value)
  }
  const rows = dashboard.value.daily
  salesChart.setOption({
    aria: { enabled: true, description: `销售趋势图，展示${periodLabel.value}每日销售额和订单量。` },
    color: ["#2563eb", "#14b8a6"],
    animationDuration: 450,
    grid: { left: 18, right: 22, top: 48, bottom: 18, containLabel: true },
    legend: { top: 0, right: 0, itemWidth: 18, itemHeight: 8, textStyle: { color: "#52657f", fontSize: 13 } },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(15, 35, 68, .94)",
      borderWidth: 0,
      textStyle: { color: "#fff", fontSize: 13 },
      valueFormatter: (value) => number(value),
    },
    xAxis: { type: "category", boundaryGap: false, data: rows.map((row) => row.date.slice(5)), axisLine: { lineStyle: { color: "#dce7f5" } }, axisTick: { show: false }, axisLabel: { color: "#64748b", fontSize: 12, hideOverlap: true } },
    yAxis: [
      { type: "value", name: `销售额 (${dashboard.value.currency})`, nameTextStyle: { color: "#64748b", fontSize: 12 }, splitLine: { lineStyle: { color: "#e8eef6", type: "dashed" } }, axisLabel: { color: "#64748b", fontSize: 12, formatter: (value) => Intl.NumberFormat("zh-CN", { notation: "compact" }).format(value) } },
      { type: "value", name: "订单", nameTextStyle: { color: "#64748b", fontSize: 12 }, splitLine: { show: false }, axisLabel: { color: "#64748b", fontSize: 12 } },
    ],
    series: [
      { name: "销售额", type: "line", smooth: true, showSymbol: rows.length < 16, symbolSize: 7, lineStyle: { width: 3 }, areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: "rgba(37,99,235,.28)" }, { offset: 1, color: "rgba(37,99,235,.02)" }] } }, data: rows.map((row) => row.sales) },
      { name: "订单量", type: "line", yAxisIndex: 1, smooth: true, showSymbol: false, lineStyle: { width: 2, type: "dashed" }, data: rows.map((row) => row.orders) },
    ],
  }, true)
}

function renderProductChart() {
  if (!productChartElement.value || !dashboard.value?.sku_breakdown?.length) return
  if (!productChart || productChart.getDom() !== productChartElement.value) {
    productChart?.dispose()
    productChart = init(productChartElement.value)
  }
  const rows = [...dashboard.value.sku_breakdown].slice(0, 10).reverse()
  productChart.setOption({
    aria: { enabled: true, description: "商品销量排名横向柱状图，按销量从高到低排序。" },
    animationDuration: 450,
    grid: { left: 18, right: 64, top: 10, bottom: 10, containLabel: true },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, backgroundColor: "rgba(15, 35, 68, .94)", borderWidth: 0, textStyle: { color: "#fff", fontSize: 13 }, formatter: (items) => `${items[0].name}<br/>销量：${number(items[0].value)} 件` },
    xAxis: { type: "value", minInterval: 1, splitLine: { lineStyle: { color: "#e8eef6", type: "dashed" } }, axisLabel: { color: "#64748b", fontSize: 12 } },
    yAxis: { type: "category", data: rows.map((row) => `${row.sku}${row.color ? ` · ${row.color}` : ""}`), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: "#334e73", fontSize: 13, width: 180, overflow: "truncate" } },
    series: [{ type: "bar", data: rows.map((row) => row.units), barMaxWidth: 22, itemStyle: { color: "#3b82f6", borderRadius: [0, 6, 6, 0] }, label: { show: true, position: "right", color: "#173c6b", fontSize: 13, fontWeight: 700, formatter: ({ value }) => `${number(value)} 件` } }],
  }, true)
}

async function renderCharts() {
  await nextTick()
  if (salesChartElement.value) resizeObserver?.observe(salesChartElement.value)
  if (productChartElement.value) resizeObserver?.observe(productChartElement.value)
  renderSalesChart()
  renderProductChart()
}

async function loadRuntime() {
  try {
    const response = await fetch("./ideadock.runtime.json", { cache: "no-store" })
    if (!response.ok) throw new Error("runtime config unavailable")
    const config = await response.json()
    apiBase.value = String(config.backend_base_url || "").replace(/\/$/, "")
  } catch {
    apiBase.value = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000"
  }
}

async function api(path, options = {}) {
  const response = await fetch(`${apiBase.value}${path}`, { cache: "no-store", headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || `请求失败：HTTP ${response.status}`)
  return body
}

function buildDashboardQuery(autoSync) {
  const query = new URLSearchParams({ auto_sync: String(autoSync) })
  if (period.value === "custom") {
    query.set("start_date", customStart.value)
    query.set("end_date", customEnd.value)
  } else {
    query.set("days", period.value)
  }
  if (store.value) query.set("store", store.value)
  return query
}

async function loadDashboard({ autoSync = true } = {}) {
  loading.value = true
  error.value = ""
  try {
    status.value = await api("/api/status")
    if (!status.value.configured) {
      dashboard.value = null
      return
    }
    dashboard.value = await api(`/api/dashboard?${buildDashboardQuery(autoSync)}`)
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

function applyCustomDate() {
  dateError.value = ""
  if (!customStart.value || !customEnd.value) {
    dateError.value = "请选择完整的开始和结束日期"
    return
  }
  if (customStart.value > customEnd.value) {
    dateError.value = "开始日期不能晚于结束日期"
    return
  }
  const selectedDays = Math.round((new Date(`${customEnd.value}T00:00:00`) - new Date(`${customStart.value}T00:00:00`)) / 86400000) + 1
  if (selectedDays > 180) {
    dateError.value = "自定义日期范围不能超过 180 天"
    return
  }
  loadDashboard({ autoSync: false })
}

async function syncNow() {
  syncing.value = true
  error.value = ""
  notice.value = ""
  try {
    const result = await api("/api/sync?trigger=button", { method: "POST" })
    notice.value = result.message || "同步完成"
    await loadDashboard({ autoSync: false })
  } catch (exception) {
    error.value = exception.message
  } finally {
    syncing.value = false
  }
}

watch(period, (value) => {
  dateError.value = ""
  if (value !== "custom") loadDashboard({ autoSync: false })
})
watch(store, () => loadDashboard({ autoSync: false }))
watch(dashboard, renderCharts, { flush: "post" })

onMounted(async () => {
  resizeObserver = new ResizeObserver(() => {
    salesChart?.resize()
    productChart?.resize()
  })
  await loadRuntime()
  await loadDashboard({ autoSync: true })
  await nextTick()
  if (salesChartElement.value) resizeObserver.observe(salesChartElement.value)
  if (productChartElement.value) resizeObserver.observe(productChartElement.value)
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  salesChart?.dispose()
  productChart?.dispose()
})
</script>

<template>
  <main class="app-shell" :data-ideadock-ready="!loading">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <div class="brand-copy"><strong>Commerce OS</strong><span>SHOPIFY 数据分析</span></div>
      </div>
      <div class="topbar-center" aria-label="系统状态">
        <span class="live-dot"></span><span>数据服务在线</span><i></i>
        <span v-if="dashboard?.last_sync?.at">更新于 {{ new Date(dashboard.last_sync.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) }}</span>
      </div>
      <button class="sync-button" :disabled="syncing || !status?.configured" @click="syncNow">
        <svg :class="['sync-icon', { spinning: syncing }]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 9a7 7 0 0111.7-2L20 12M4 12l2.2 5a7 7 0 0011.7-2"/></svg>
        <span>{{ syncing ? "同步中" : "立即同步" }}</span>
      </button>
    </header>

    <div class="content-shell">
      <section class="page-header">
        <div>
          <div class="breadcrumb"><span>数据中心</span><i>/</i><strong>经营概览</strong></div>
          <h1>经营数据看板</h1>
          <p>集中查看销售、商品与履约表现，数据按需同步。</p>
        </div>
        <span class="period-badge" v-if="dashboard">{{ periodLabel }}</span>
      </section>

      <section class="filter-bar" aria-label="看板筛选">
        <label class="filter-field">
          <span>分析周期</span>
          <div class="select-wrap"><select v-model="period"><option value="1">今日</option><option value="7">最近 7 天</option><option value="30">最近 30 天</option><option value="90">最近 90 天</option><option value="custom">自定义日期</option></select><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></div>
        </label>
        <label class="filter-field">
          <span>经营主体</span>
          <div class="select-wrap"><select v-model="store"><option value="">全部店铺</option><option v-for="item in dashboard?.stores || []" :key="item.domain" :value="item.domain">{{ item.name }}</option></select><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></div>
        </label>
        <div v-if="period === 'custom'" class="custom-date-group">
          <label class="filter-field"><span>开始日期</span><input v-model="customStart" type="date" :max="customEnd || undefined"></label>
          <label class="filter-field"><span>结束日期</span><input v-model="customEnd" type="date" :min="customStart || undefined"></label>
          <button class="apply-button" @click="applyCustomDate">应用日期</button>
        </div>
        <span v-if="dateError" class="date-error" role="alert">{{ dateError }}</span>
      </section>

      <section v-if="error" class="message error-message" role="alert"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/></svg><div><strong>数据加载失败</strong><span>{{ error }}</span></div></section>
      <section v-if="notice" class="message success-message" role="status"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/></svg><span>{{ notice }}</span></section>
      <section v-if="loading" class="loading-grid" aria-label="正在加载"><div v-for="index in 8" :key="index" class="skeleton"></div></section>

      <section v-else-if="status && !status.configured" class="setup-card">
        <div class="setup-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path d="M12 15.5a3.5 3.5 0 100-7 3.5 3.5 0 000 7Z"/><path d="M19.4 15a1.7 1.7 0 00.34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 00-1.88-.34 1.7 1.7 0 00-1.03 1.56V21h-4v-.08A1.7 1.7 0 009 19.36a1.7 1.7 0 00-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 004.63 15a1.7 1.7 0 00-1.56-1.03H3v-4h.08A1.7 1.7 0 004.64 9a1.7 1.7 0 00-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 009 4.63 1.7 1.7 0 0010.03 3H10V3h4v.08A1.7 1.7 0 0015 4.64a1.7 1.7 0 001.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0019.37 9a1.7 1.7 0 001.56 1.03H21v4h-.08A1.7 1.7 0 0019.4 15Z"/></svg></div>
        <div><p class="section-label">一次性配置</p><h2>连接 Shopify 数据源</h2><p>请在 IdeaDock 后端服务中配置 Secret <code>SHOPIFY_STORES_JSON</code>，然后返回本页点击“立即同步”。</p><p class="security-note">凭据只保存在 IdeaDock Secret 中，不会进入前端或数据库。</p></div>
      </section>

      <template v-else-if="dashboard">
        <section class="metrics-grid" aria-label="核心经营指标">
          <article v-for="metric in metricCards" :key="metric.label" :class="['metric-card', metric.tone]">
            <div class="metric-top"><span class="metric-label">{{ metric.label }}</span><span class="metric-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path :d="metric.icon"/></svg></span></div>
            <strong>{{ metric.value }}</strong>
            <div class="metric-bottom"><span :class="['metric-change', changeClass(metric.change)]"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path :d="metric.change >= 0 ? 'M3 10l4-4 3 3 3-4' : 'M3 6l4 4 3-3 3 4'"/></svg>{{ changeText(metric.change) }}</span></div>
          </article>
        </section>

        <section class="pulse-panel">
          <div class="pulse-heading"><span class="section-label">经营摘要</span><h2>关键效率指标</h2><p>基于当前筛选范围计算</p></div>
          <div class="pulse-grid"><div v-for="item in commercePulse" :key="item.label" :class="['pulse-item', item.tone]"><span>{{ item.label }}</span><strong>{{ item.value }}</strong><small>{{ item.note }}</small></div></div>
        </section>

        <section class="dashboard-grid">
          <article class="panel trend-panel">
            <div class="panel-heading"><div><span class="section-label">销售趋势</span><h2>销售额与订单量</h2></div><div class="panel-summary"><span>周期销售额<strong>{{ money(periodSales) }}</strong></span><span>销售峰值<strong>{{ peakDay?.date || "—" }}</strong></span></div></div>
            <div v-if="dashboard.daily.length" ref="salesChartElement" class="echart sales-chart" role="img" aria-label="销售额与订单量趋势图"></div>
            <div v-else class="empty-state">当前范围暂无销售数据</div>
          </article>

          <article class="panel fulfillment-panel">
            <div class="panel-heading"><div><span class="section-label">订单履约</span><h2>履约健康度</h2></div><span class="health-badge">按筛选范围</span></div>
            <div class="fulfillment-score"><div class="score-ring" :style="{ '--score': `${fulfillmentRate * 3.6}deg` }"><div><strong>{{ percent(fulfillmentRate) }}</strong><span>已完成</span></div></div><div class="score-copy"><span>已发货订单</span><strong>{{ number(fulfilledOrders) }} <small>/ {{ number(fulfillmentTotal) }}</small></strong><p>当前筛选范围内已发货订单占比</p></div></div>
            <div class="fulfillment-list"><div v-for="item in dashboard.fulfillment" :key="item.status" class="fulfillment-row"><div><span>{{ fulfillmentLabels[item.status] || item.status }}</span><strong>{{ item.orders }}</strong></div><div class="progress"><i :style="{ width: `${item.orders / maxFulfillment * 100}%` }"></i></div></div><div v-if="!dashboard.fulfillment.length" class="empty-state compact">暂无履约数据</div></div>
          </article>

          <article class="panel product-panel">
            <div class="panel-heading"><div><span class="section-label">商品表现</span><h2>商品销量排名</h2></div><span>按 SKU 与颜色汇总 · TOP {{ Math.min(dashboard.sku_breakdown.length, 10) }}</span></div>
            <div v-if="dashboard.sku_breakdown.length" ref="productChartElement" class="echart product-chart" role="img" aria-label="商品销量排名横向柱状图"></div>
            <div v-else class="empty-state">当前范围暂无商品销量</div>
            <div v-if="abnormalOrders.length" class="abnormal-orders" aria-label="异常订单">
              <div class="abnormal-orders-heading"><div><span class="section-label">数据提醒</span><h3>异常订单</h3></div><span>{{ abnormalOrders.length }} 笔</span></div>
              <p>这些订单未提供 SKU，已从商品销量排名中剔除。</p>
              <div class="abnormal-order-list"><span v-for="orderName in abnormalOrders" :key="orderName">{{ orderName }}</span></div>
            </div>
          </article>
        </section>

        <footer><div><span class="live-dot"></span>{{ dashboard.last_sync.message }}</div><span>{{ dashboard.timezone_note }}</span></footer>
      </template>
    </div>
  </main>
</template>
