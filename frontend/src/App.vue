<script setup>
import { computed, onMounted, ref, watch } from "vue"

const apiBase = ref("")
const loading = ref(true)
const syncing = ref(false)
const error = ref("")
const notice = ref("")
const status = ref(null)
const dashboard = ref(null)
const days = ref(30)
const store = ref("")

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
const riskLabels = { LOW: "低", MEDIUM: "中", HIGH: "高", UNKNOWN: "未知" }

const money = (value, digits = 0) => new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: dashboard.value?.currency || "USD",
  maximumFractionDigits: digits,
}).format(Number(value || 0))
const number = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0))
const percent = (value) => `${Number(value || 0).toFixed(1)}%`
const changeText = (value) => {
  if (value === null || value === undefined) return "上期为 0"
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
const averageOrderValue = computed(() => {
  const totals = dashboard.value?.totals
  return totals?.orders ? totals.net_sales / totals.orders : 0
})
const refundRate = computed(() => {
  const totals = dashboard.value?.totals
  return totals?.sales ? totals.refunds / totals.sales * 100 : 0
})
const topSku = computed(() => dashboard.value?.sku_breakdown?.[0] || null)
const commercePulse = computed(() => [
  { label: "平均客单价", value: money(averageOrderValue.value, 2), note: "每笔订单净销售", tone: "cyan" },
  { label: "退款率", value: percent(refundRate.value), note: "退款 / 销售额", tone: refundRate.value > 5 ? "gold" : "emerald" },
  { label: "履约完成率", value: percent(fulfillmentRate.value), note: `${number(fulfilledOrders.value)} / ${number(fulfillmentTotal.value)} 单`, tone: "violet" },
  { label: "热销 SKU", value: topSku.value?.sku || "—", note: topSku.value ? `${topSku.value.color} · ${number(topSku.value.units)} 件` : "暂无商品数据", tone: "gold" },
])

const maxDailySales = computed(() => Math.max(...(dashboard.value?.daily || []).map((row) => row.sales), 1))
const maxSkuUnits = computed(() => Math.max(...(dashboard.value?.sku_breakdown || []).map((row) => row.units), 1))
const maxFulfillment = computed(() => Math.max(...(dashboard.value?.fulfillment || []).map((row) => row.orders), 1))
const linePoints = computed(() => {
  const rows = dashboard.value?.daily || []
  if (!rows.length) return ""
  const width = 800
  const height = 238
  return rows.map((row, index) => {
    const x = rows.length === 1 ? width / 2 : index / (rows.length - 1) * width
    const y = height - (row.sales / maxDailySales.value * (height - 24)) - 6
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(" ")
})
const periodSales = computed(() => (dashboard.value?.daily || []).reduce((sum, item) => sum + item.sales, 0))
const peakDay = computed(() => (dashboard.value?.daily || []).reduce((peak, item) => !peak || item.sales > peak.sales ? item : peak, null))

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
  const response = await fetch(`${apiBase.value}${path}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || `请求失败：HTTP ${response.status}`)
  return body
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
    const query = new URLSearchParams({ days: String(days.value), auto_sync: String(autoSync) })
    if (store.value) query.set("store", store.value)
    dashboard.value = await api(`/api/dashboard?${query}`)
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
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

watch([days, store], () => loadDashboard({ autoSync: false }))
onMounted(async () => {
  await loadRuntime()
  await loadDashboard({ autoSync: true })
})
</script>

<template>
  <main class="app-shell" :data-ideadock-ready="!loading">
    <div class="ambient ambient-one"></div>
    <div class="ambient ambient-two"></div>

    <header class="topbar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <div class="brand-copy"><strong>Commerce OS</strong><span>SHOPIFY BUSINESS INTELLIGENCE</span></div>
      </div>
      <div class="topbar-center" aria-label="系统状态">
        <span class="live-dot"></span>
        <span>数据服务在线</span>
        <i></i>
        <span v-if="dashboard?.last_sync?.at">更新于 {{ new Date(dashboard.last_sync.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) }}</span>
      </div>
      <button class="sync-button" :disabled="syncing || !status?.configured" @click="syncNow">
        <svg :class="['sync-icon', { spinning: syncing }]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 9a7 7 0 0111.7-2L20 12M4 12l2.2 5a7 7 0 0011.7-2"/></svg>
        <span>{{ syncing ? "同步中" : "立即同步" }}</span>
      </button>
    </header>

    <div class="content-shell">
      <section class="hero">
        <div class="hero-copy-block">
          <div class="eyebrow"><span></span>EXECUTIVE COMMAND CENTER</div>
          <h1>电商经营<span>全域洞察</span></h1>
          <p>汇聚 Shopify 销售、商品、退款与履约信号，让关键增长机会和经营风险在一个视图中清晰呈现。</p>
        </div>
        <div class="filters" aria-label="看板筛选">
          <label>
            <span>分析周期</span>
            <div class="select-wrap"><select v-model="days"><option :value="7">最近 7 天</option><option :value="30">最近 30 天</option><option :value="90">最近 90 天</option></select><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></div>
          </label>
          <label>
            <span>经营主体</span>
            <div class="select-wrap"><select v-model="store"><option value="">全部店铺</option><option v-for="item in dashboard?.stores || []" :key="item.domain" :value="item.domain">{{ item.name }}</option></select><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></div>
          </label>
        </div>
      </section>

      <section v-if="error" class="message error-message" role="alert"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/></svg><div><strong>数据加载失败</strong><span>{{ error }}</span></div></section>
      <section v-if="notice" class="message success-message" role="status"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/></svg><span>{{ notice }}</span></section>
      <section v-if="loading" class="loading-grid" aria-label="正在加载"><div v-for="index in 10" :key="index" class="skeleton"></div></section>

      <section v-else-if="status && !status.configured" class="setup-card">
        <div class="setup-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path d="M12 15.5a3.5 3.5 0 100-7 3.5 3.5 0 000 7Z"/><path d="M19.4 15a1.7 1.7 0 00.34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 00-1.88-.34 1.7 1.7 0 00-1.03 1.56V21h-4v-.08A1.7 1.7 0 009 19.36a1.7 1.7 0 00-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 004.63 15a1.7 1.7 0 00-1.56-1.03H3v-4h.08A1.7 1.7 0 004.64 9a1.7 1.7 0 00-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 009 4.63 1.7 1.7 0 0010.03 3H10V3h4v.08A1.7 1.7 0 0015 4.64a1.7 1.7 0 001.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0019.37 9a1.7 1.7 0 001.56 1.03H21v4h-.08A1.7 1.7 0 0019.4 15Z"/></svg></div>
        <div><p class="eyebrow"><span></span>ONE-TIME SETUP</p><h2>连接 Shopify 数据源</h2><p>后端已经运行，但尚未配置 Shopify 店铺凭据。请在 IdeaDock 项目的后端服务配置页添加 Secret <code>SHOPIFY_STORES_JSON</code>，然后返回本页点击“立即同步”。</p><p class="security-note">凭据仅保存在 IdeaDock Secret 中，不会出现在前端、代码或数据库。</p></div>
      </section>

      <template v-else-if="dashboard">
        <section class="metrics-grid" aria-label="核心经营指标">
          <article v-for="metric in metricCards" :key="metric.label" :class="['metric-card', metric.tone]">
            <div class="metric-top"><span class="metric-label">{{ metric.label }}</span><span class="metric-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path :d="metric.icon"/></svg></span></div>
            <strong>{{ metric.value }}</strong>
            <div class="metric-bottom"><span :class="['metric-change', changeClass(metric.change)]"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path :d="metric.change >= 0 ? 'M3 10l4-4 3 3 3-4' : 'M3 6l4 4 3-3 3 4'"/></svg>{{ changeText(metric.change) }}</span><span class="metric-index">{{ String(metricCards.indexOf(metric) + 1).padStart(2, "0") }}</span></div>
          </article>
        </section>

        <section class="pulse-panel">
          <div class="pulse-heading"><div><span class="section-label">COMMERCE PULSE</span><h2>经营脉搏</h2></div><span>基于当前筛选周期实时计算</span></div>
          <div class="pulse-grid">
            <div v-for="item in commercePulse" :key="item.label" :class="['pulse-item', item.tone]"><span>{{ item.label }}</span><strong>{{ item.value }}</strong><small>{{ item.note }}</small></div>
          </div>
        </section>

        <section class="dashboard-grid">
          <article class="panel trend-panel">
            <div class="panel-heading">
              <div><span class="section-label">REVENUE VELOCITY</span><h2>销售动能趋势</h2></div>
              <div class="panel-summary"><span>周期销售额<strong>{{ money(periodSales) }}</strong></span><span>峰值日期<strong>{{ peakDay?.date || "—" }}</strong></span></div>
            </div>
            <div class="line-chart" v-if="dashboard.daily.length">
              <svg viewBox="0 0 800 260" role="img" aria-label="每日销售额趋势图">
                <defs>
                  <linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#22d3ee" stop-opacity=".3"/><stop offset=".65" stop-color="#2563eb" stop-opacity=".06"/><stop offset="1" stop-color="#2563eb" stop-opacity="0"/></linearGradient>
                  <linearGradient id="line" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#38bdf8"/><stop offset=".55" stop-color="#22d3ee"/><stop offset="1" stop-color="#818cf8"/></linearGradient>
                  <filter id="lineGlow"><feGaussianBlur stdDeviation="4" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
                </defs>
                <line v-for="y in [34, 86, 138, 190, 242]" :key="y" x1="0" :y1="y" x2="800" :y2="y" class="grid-line"/>
                <polyline :points="`0,250 ${linePoints} 800,250`" fill="url(#area)" stroke="none"/>
                <polyline :points="linePoints" fill="none" stroke="url(#line)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" filter="url(#lineGlow)"/>
              </svg>
              <div class="chart-labels"><span>{{ dashboard.daily[0]?.date.slice(5) }}</span><span>{{ dashboard.daily[Math.floor(dashboard.daily.length / 2)]?.date.slice(5) }}</span><span>{{ dashboard.daily.at(-1)?.date.slice(5) }}</span></div>
            </div>
            <div v-else class="empty-state">当前范围暂无销售数据</div>
          </article>

          <article class="panel fulfillment-panel">
            <div class="panel-heading"><div><span class="section-label">ORDER FLOW</span><h2>履约健康度</h2></div><span class="health-badge">实时</span></div>
            <div class="fulfillment-score">
              <div class="score-ring" :style="{ '--score': `${fulfillmentRate * 3.6}deg` }"><div><strong>{{ percent(fulfillmentRate) }}</strong><span>已完成</span></div></div>
              <div class="score-copy"><span>订单履约率</span><strong>{{ number(fulfilledOrders) }} <small>/ {{ number(fulfillmentTotal) }}</small></strong><p>当前筛选范围内已发货订单占比</p></div>
            </div>
            <div class="fulfillment-list">
              <div v-for="item in dashboard.fulfillment" :key="item.status" class="fulfillment-row"><div><span>{{ fulfillmentLabels[item.status] || item.status }}</span><strong>{{ item.orders }}</strong></div><div class="progress"><i :style="{ width: `${item.orders / maxFulfillment * 100}%` }"></i></div></div>
              <div v-if="!dashboard.fulfillment.length" class="empty-state compact">暂无履约数据</div>
            </div>
          </article>

          <article class="panel sku-panel">
            <div class="panel-heading"><div><span class="section-label">PRODUCT SIGNAL</span><h2>商品增长榜</h2></div><span>TOP {{ dashboard.sku_breakdown.length }}</span></div>
            <div class="sku-list">
              <div v-for="(item, index) in dashboard.sku_breakdown" :key="`${item.sku}-${item.color}`" class="sku-row">
                <span :class="['rank', { leading: index < 3 }]">{{ String(index + 1).padStart(2, "0") }}</span><div class="sku-name"><strong>{{ item.sku }}</strong><span>{{ item.color }}</span></div><div class="sku-bar"><i :style="{ width: `${item.units / maxSkuUnits * 100}%` }"></i></div><strong class="sku-units">{{ number(item.units) }}<small> 件</small></strong>
              </div>
              <div v-if="!dashboard.sku_breakdown.length" class="empty-state">暂无商品销量</div>
            </div>
          </article>

          <article class="panel orders-panel">
            <div class="panel-heading"><div><span class="section-label">LATEST ORDERS</span><h2>实时订单流</h2></div><span>{{ dashboard.recent_orders.length }} 条最新记录</span></div>
            <div class="table-wrap"><table><thead><tr><th>订单编号</th><th>店铺</th><th>下单日期</th><th>件数</th><th>销售额</th><th>风险</th><th>履约状态</th></tr></thead><tbody>
              <tr v-for="order in dashboard.recent_orders" :key="`${order.store}-${order.name}`"><td><strong>{{ order.name }}</strong></td><td>{{ order.store }}</td><td>{{ order.date }}</td><td>{{ number(order.units) }}</td><td class="money-cell">{{ money(order.sales) }}</td><td><span :class="['badge', `risk-${order.risk.toLowerCase()}`]"><i></i>{{ riskLabels[order.risk] || order.risk }}</span></td><td><span class="status-text"><i></i>{{ fulfillmentLabels[order.fulfillment] || order.fulfillment }}</span></td></tr>
            </tbody></table></div>
            <div v-if="!dashboard.recent_orders.length" class="empty-state">当前范围暂无订单</div>
          </article>
        </section>

        <footer><div><span class="live-dot"></span>{{ dashboard.last_sync.message }}</div><span>{{ dashboard.timezone_note }}</span></footer>
      </template>
    </div>
  </main>
</template>
