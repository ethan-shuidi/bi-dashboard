<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { fetchWithDashboardAuth } from "./dashboardAuth"
import zhCn from "element-plus/es/locale/lang/zh-cn"
import WeekPicker from "./WeekPicker.vue"

const props = defineProps({ apiBase: { type: String, default: "" } })
const SITE_ORDER = ["美国", "日本", "德国", "英国", "法国", "加拿大", "澳洲", "西班牙", "意大利", "荷兰", "比利时", "墨西哥", "爱尔兰", "波兰", "瑞典"]
const DEFAULT_SERIES = ["TN10系列（主链接）汇总", "TN10系列（小链接）汇总", "TN20系列（主链接）汇总", "TN20系列（小链接）汇总"]
const STRATEGIES = ["品类词", "品牌防御", "竞品词", "自动", "SB/SBV", "SD", "B2B", "bundle", "/"]
const currencySymbols = { USD: "$", JPY: "¥", EUR: "€", GBP: "£", CAD: "CA$", AUD: "A$", SEK: "kr", MXN: "MX$", PLN: "zł" }
const rows = ref([]); const previousRows = ref([]); const stores = ref([]); const seriesOptions = ref([...DEFAULT_SERIES])
const loading = ref(false); const error = ref(""); const saveError = ref(""); const saving = ref(""); const toast = ref(""); const expanded = ref(new Set()); const noteDrafts = ref({}); const campaignDrafts = ref({})
const strategySite = ref("美国"); const strategyStore = ref(""); const strategySeries = ref(""); const strategyWeekStart = ref("")
const showComparison = ref(false)
const configOpen = ref(false); const configPosition = ref({ top: 0, left: 0 }); const configButton = ref(null)
const columns = [{ key: "clicks", label: "点击量", width: 100 }, { key: "cpc", label: "CPC", width: 110, money: true }, { key: "ad_cost", label: "广告花费", width: 130, money: true }, { key: "ad_sales", label: "广告销售额", width: 135, money: true }, { key: "ad_orders", label: "广告订单量", width: 115 }, { key: "ad_units", label: "广告销量", width: 105 }, { key: "acos", label: "ACOS", width: 105, percent: true }, { key: "roas", label: "ROAS", width: 105 }, { key: "ad_cvr", label: "广告CVR", width: 115, percent: true }]
const nameColumnWidth = ref(430)
const noteColumnWidth = ref(290)
const widths = ref(Object.fromEntries(columns.map((column) => [column.key, column.width]))); const visible = ref(Object.fromEntries(columns.map((column) => [column.key, true]))); const columnOrder = ref(columns.map((column) => column.key)); const sort = ref({ key: "clicks", direction: "desc" }); const draggingColumn = ref(""); const tableHeight = ref(null)
const columnStorageKey = "ideadock.amazon-strategy-board.columns.v1"
let resizeCleanup = null; let toastTimer = null; let strategyRequestSeq = 0
const orderedSites = computed(() => [...new Set([...SITE_ORDER, ...stores.value.map((item) => item.country).filter(Boolean)])].sort((a, b) => (SITE_ORDER.indexOf(a) < 0 ? 999 : SITE_ORDER.indexOf(a)) - (SITE_ORDER.indexOf(b) < 0 ? 999 : SITE_ORDER.indexOf(b)) || a.localeCompare(b, "zh-CN")))
const visibleColumns = computed(() => columnOrder.value.map((key) => columns.find((column) => column.key === key)).filter((column) => column && visible.value[column.key]))
const strategyTableWidth = computed(() => visibleColumns.value.reduce((sum, column) => sum + widths.value[column.key], 0) + nameColumnWidth.value + noteColumnWidth.value)
const tableStyle = computed(() => ({ "--strategy-name-width": `${nameColumnWidth}px`, "--strategy-note-width": `${noteColumnWidth}px`, "--strategy-table-width": `${strategyTableWidth.value}px` }))
const campaignCount = computed(() => new Set(rows.value.flatMap((row) => row.campaigns || []).map(campaignKey)).size)
const availableStores = computed(() => stores.value.filter((item) => item.country === strategySite.value && item.status !== 0))
const percentMetricKeys = new Set(["acos", "ad_cvr"])
const moneyMetricKeys = new Set(["cpc", "ad_cost", "ad_sales"])
const lowerIsBetterMetricKeys = new Set(["cpc", "ad_cost", "acos"])
function formatLocalDate(date) { return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}` }
function formatDotDate(value) { return String(value || "").replaceAll("-", ".") }
const weekEndFrom = (start) => { const date = new Date(`${monday(start || previousWeekStart())}T00:00:00`); date.setDate(date.getDate() + 6); return formatLocalDate(date) }
const weekEnd = (start) => weekEndFrom(start)
const strategyEndDate = computed(() => weekEnd(strategyWeekStart.value))
function weekNumber(value) { const date = new Date(`${monday(value)}T00:00:00`); const thursday = new Date(date); thursday.setDate(date.getDate() + 3); const firstThursday = new Date(thursday.getFullYear(), 0, 4); const firstMonday = new Date(firstThursday); firstMonday.setDate(firstThursday.getDate() - ((firstThursday.getDay() + 6) % 7)); return Math.floor((date - firstMonday) / 604800000) + 1 }
const strategyWeekRangeLabel = computed(() => `${formatDotDate(strategyWeekStart.value)}~${formatDotDate(strategyEndDate.value)}`)
const groups = computed(() => rows.value.map((row) => ({ ...row, campaigns: [...(row.campaigns || [])].sort((a, b) => compare(a[sort.value.key], b[sort.value.key], sort.value.direction)) })).sort((a, b) => compare(a.metrics?.[sort.value.key], b.metrics?.[sort.value.key], sort.value.direction)))
function aggregateStrategyMetrics(sourceRows) {
  const total = { clicks: 0, ad_cost: 0, ad_sales: 0, ad_orders: 0, ad_units: 0 }
  const currencies = [...new Set(sourceRows.map((row) => row.currency).filter(Boolean))]
  const mixedCurrency = currencies.length > 1
  sourceRows.forEach((row) => {
    const metrics = row.metrics || {}
    const clicks = Number(metrics.clicks) || 0
    total.clicks += clicks
    total.ad_cost += Number(metrics.ad_cost) || 0
    total.ad_sales += Number(metrics.ad_sales) || 0
    total.ad_orders += Number(metrics.ad_orders) || 0
    total.ad_units += Number(metrics.ad_units) || 0
  })
  return {
    clicks: Math.round(total.clicks),
    cpc: mixedCurrency ? null : total.clicks ? total.ad_cost / total.clicks : null,
    ad_cost: mixedCurrency ? null : total.ad_cost,
    ad_sales: mixedCurrency ? null : total.ad_sales,
    ad_orders: total.ad_orders,
    ad_units: total.ad_units,
    acos: mixedCurrency ? null : total.ad_sales ? total.ad_cost / total.ad_sales : null,
    roas: mixedCurrency ? null : total.ad_cost ? total.ad_sales / total.ad_cost : null,
    ad_cvr: total.clicks ? total.ad_orders / total.clicks : null,
  }
}
const summaryMetrics = computed(() => aggregateStrategyMetrics(rows.value))
const previousSummaryMetrics = computed(() => aggregateStrategyMetrics(previousRows.value))
const summaryCurrency = computed(() => new Set(rows.value.map((row) => row.currency).filter(Boolean)).size > 1 ? "MIXED" : rows.value[0]?.currency || "")
const previousStrategyByKey = computed(() => new Map(previousRows.value.map((row) => [groupKey(row), row])))
const previousCampaignByKey = computed(() => new Map(previousRows.value.flatMap((row) => row.campaigns || []).map((campaign) => [campaignKey(campaign), campaign])))
function previousWeekStart() { const date = new Date(); date.setHours(0, 0, 0, 0); date.setDate(date.getDate() - ((date.getDay() + 6) % 7) - 7); return formatLocalDate(date) }
function monday(value) { const date = new Date(`${value}T00:00:00`); if (Number.isNaN(date.getTime())) return previousWeekStart(); date.setDate(date.getDate() - ((date.getDay() + 6) % 7)); return formatLocalDate(date) }
function compare(left, right, direction) { const a = Number(left); const b = Number(right); if (Number.isNaN(a) && Number.isNaN(b)) return 0; if (Number.isNaN(a)) return 1; if (Number.isNaN(b)) return -1; return direction === "asc" ? a - b : b - a }
function display(value, column, currency) { if (value == null) return "—"; if (column.percent) return `${(Number(value) * 100).toFixed(2)}%`; if (column.money) return `${currencySymbols[currency] || currency || ""} ${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`; return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 }) }
function formatSignedNumber(value) { const numericValue = Number(value || 0); const formatted = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(Math.abs(numericValue)); return `${numericValue > 0 ? "+" : numericValue < 0 ? "-" : ""}${formatted}` }
function comparisonResult(current, previous, column, currency) {
  if (!showComparison.value || current == null || previous == null || !Number.isFinite(Number(current)) || !Number.isFinite(Number(previous))) return null
  const difference = Number(current) - Number(previous)
  if (!Number.isFinite(difference)) return null
  let text
  if (column.percent) text = `${difference > 0 ? "+" : difference < 0 ? "-" : ""}${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(Math.abs(difference * 100))}%`
  else if (column.money) text = `${difference < 0 ? "-" : difference > 0 ? "+" : ""}${currencySymbols[currency] || currency || ""}${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(Math.abs(difference))}`
  else text = formatSignedNumber(difference)
  const improved = lowerIsBetterMetricKeys.has(column.key) ? difference < 0 : difference > 0
  const declined = lowerIsBetterMetricKeys.has(column.key) ? difference > 0 : difference < 0
  return { status: improved ? "red" : declined ? "green" : "gray", text }
}
function rowComparison(row, column) {
  if (!showComparison.value) return null
  const previous = previousStrategyByKey.value.get(groupKey(row))
  return comparisonResult(row.metrics?.[column.key], previous?.metrics?.[column.key], column, row.currency) || { status: "gray", text: "—" }
}
function campaignComparison(campaign, column) {
  if (!showComparison.value) return null
  const previous = previousCampaignByKey.value.get(campaignKey(campaign))
  return comparisonResult(campaign[column.key], previous?.[column.key], column, campaign.currency) || { status: "gray", text: "—" }
}
function summaryComparison(column) {
  if (!showComparison.value || !previousRows.value.length) return null
  return comparisonResult(summaryMetrics.value[column.key], previousSummaryMetrics.value[column.key], column, summaryCurrency.value) || { status: "gray", text: "—" }
}
function displaySeries(value) { return ({ "TN10系列（主链接）汇总": "TN10（主）", "TN10系列（小链接）汇总": "TN10（小）", "TN20系列（主链接）汇总": "TN20（主）", "TN20系列（小链接）汇总": "TN20（小）" }[value] || value || "") }
function groupMeta(row) { return [row.series ? displaySeries(row.series) : "", row.site, `${row.campaigns.length} 个活动`].filter(Boolean).join(" · ") }
function groupKey(row) { return `${row.site_code}:${row.strategy}:${row.series || ""}:${row.product || ""}` }; function noteKey(row) { return `${strategyWeekStart.value}:${groupKey(row)}` }
function toggle(row) { const next = new Set(expanded.value); const key = groupKey(row); next.has(key) ? next.delete(key) : next.add(key); expanded.value = next }
function cycleSort(column) { sort.value = sort.value.key !== column.key ? { key: column.key, direction: "desc" } : { key: column.key, direction: sort.value.direction === "desc" ? "asc" : "desc" } }
function toggleColumn(column) { visible.value[column.key] = !visible.value[column.key]; saveColumnPreferences() }
function boundedWidth(value, minimum, maximum, fallback) { const width = Number(value); return Number.isFinite(width) ? Math.max(minimum, Math.min(maximum, Math.round(width))) : fallback }
function loadColumnPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(columnStorageKey) || "null")
    if (!saved) return
    const savedOrder = Array.isArray(saved.order) ? saved.order.filter((key) => columns.some((column) => column.key === key)) : []
    columnOrder.value = [...savedOrder, ...columns.map((column) => column.key).filter((key) => !savedOrder.includes(key))]
    for (const column of columns) {
      const preference = saved.columns?.[column.key]
      if (!preference) continue
      widths.value[column.key] = boundedWidth(preference.width, 76, 320, column.width)
      if (typeof preference.visible === "boolean") visible.value[column.key] = preference.visible
    }
    nameColumnWidth.value = boundedWidth(saved.nameColumnWidth, 260, 720, nameColumnWidth.value)
    noteColumnWidth.value = boundedWidth(saved.noteColumnWidth, 220, 560, noteColumnWidth.value)
  } catch {}
}
function saveColumnPreferences() {
  try {
    const preferences = Object.fromEntries(columns.map((column) => [column.key, { width: widths.value[column.key], visible: visible.value[column.key] }]))
    localStorage.setItem(columnStorageKey, JSON.stringify({ nameColumnWidth: nameColumnWidth.value, noteColumnWidth: noteColumnWidth.value, order: columnOrder.value, columns: preferences }))
  } catch {}
}
function startDrag(column) { draggingColumn.value = column.key }
function dropColumn(column) { if (!draggingColumn.value || draggingColumn.value === column.key) return; const source = columnOrder.value.indexOf(draggingColumn.value); const target = columnOrder.value.indexOf(column.key); if (source < 0 || target < 0) return; const [moved] = columnOrder.value.splice(source, 1); columnOrder.value.splice(target, 0, moved); draggingColumn.value = ""; saveColumnPreferences() }
function startResize(event, column) { event.preventDefault(); event.stopPropagation(); resizeCleanup?.(); const startX = event.clientX; const startWidth = widths.value[column.key]; const move = (moveEvent) => { widths.value[column.key] = Math.max(76, Math.min(320, Math.round(startWidth + moveEvent.clientX - startX))) }; const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); resizeCleanup = null; saveColumnPreferences() }; resizeCleanup = stop; window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop, { once: true }) }
function startNameResize(event) { event.preventDefault(); event.stopPropagation(); resizeCleanup?.(); const startX = event.clientX; const startWidth = nameColumnWidth.value; const move = (moveEvent) => { nameColumnWidth.value = Math.max(260, Math.min(720, Math.round(startWidth + moveEvent.clientX - startX))) }; const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); resizeCleanup = null; saveColumnPreferences() }; resizeCleanup = stop; window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop, { once: true }) }
function startNoteResize(event) { event.preventDefault(); event.stopPropagation(); resizeCleanup?.(); const startX = event.clientX; const startWidth = noteColumnWidth.value; const move = (moveEvent) => { noteColumnWidth.value = Math.max(220, Math.min(560, Math.round(startWidth + moveEvent.clientX - startX))) }; const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); resizeCleanup = null; saveColumnPreferences() }; resizeCleanup = stop; window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop, { once: true }) }
function startTableResize(event) { event.preventDefault(); event.stopPropagation(); resizeCleanup?.(); const wrap = event.currentTarget?.previousElementSibling; const startY = event.clientY; const startHeight = tableHeight.value || Math.max(280, wrap?.getBoundingClientRect().height || 520); const move = (moveEvent) => { tableHeight.value = Math.max(240, Math.min(window.innerHeight - 180, Math.round(startHeight + moveEvent.clientY - startY))) }; const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); resizeCleanup = null }; resizeCleanup = stop; window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop, { once: true }) }
function normalizedWheelDelta(delta, deltaMode) { return deltaMode === WheelEvent.DOM_DELTA_LINE ? delta * 16 : deltaMode === WheelEvent.DOM_DELTA_PAGE ? delta * window.innerHeight : delta }
function handleTableWheel(event) {
  const wrapper = event.currentTarget
  if (!(wrapper instanceof HTMLElement)) return
  const deltaX = normalizedWheelDelta(event.deltaX, event.deltaMode)
  const deltaY = normalizedWheelDelta(event.deltaY, event.deltaMode)
  const horizontalIntent = event.shiftKey || Math.abs(deltaX) > Math.abs(deltaY) * 1.5
  if (horizontalIntent || deltaY === 0 || !event.cancelable) return
  const canConsumeVertical = deltaY < 0 ? wrapper.scrollTop > 0 : wrapper.scrollTop + wrapper.clientHeight < wrapper.scrollHeight - 1
  if (!canConsumeVertical) {
    if (deltaX === 0) return
    event.preventDefault()
    window.scrollBy({ top: deltaY })
    return
  }
  if (deltaX === 0) return
  event.preventDefault()
  wrapper.scrollTop += deltaY
}
function updateConfigPosition() { const rect = configButton.value?.getBoundingClientRect(); if (rect) configPosition.value = { top: rect.bottom + 8, left: Math.max(8, rect.right - 190) } }
function toggleConfig() { configOpen.value = !configOpen.value; if (configOpen.value) requestAnimationFrame(updateConfigPosition) }
function showToast(message) { toast.value = message; if (toastTimer) window.clearTimeout(toastTimer); toastTimer = window.setTimeout(() => { toast.value = ""; toastTimer = null }, 1600) }
function fitNoteEditor(textarea) {
  textarea.style.height = "auto"
  const style = getComputedStyle(textarea)
  const borders = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth)
  textarea.style.height = `${Math.max(30, textarea.scrollHeight + borders)}px`
}
function resizeNote(event) { fitNoteEditor(event.target) }
function resizeAllNotes() { document.querySelectorAll(".strategy-note-cell textarea").forEach(fitNoteEditor) }
async function copyCampaignId(campaign) { try { if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(String(campaign.campaign_id)); else { const input = document.createElement("textarea"); input.value = String(campaign.campaign_id); input.style.position = "fixed"; input.style.opacity = "0"; document.body.appendChild(input); input.select(); document.execCommand("copy"); input.remove() }; showToast(`Campaign ID ${campaign.campaign_id} 已复制`) } catch { showToast("复制失败，请检查浏览器剪贴板权限") } }
function campaignKey(campaign) { return `${campaign.site_code}:${campaign.store_sid}:${campaign.campaign_id}` }
function campaignDraftFor(row, campaign) { return campaignDrafts.value[campaignKey(campaign)] || { strategy: campaign.strategy || row.strategy || "/", series: campaign.series || row.series || "" } }
function setCampaignDraft(campaign, field, value) {
  const key = campaignKey(campaign)
  const draft = campaignDrafts.value[key] || { strategy: "/", series: "" }
  campaignDrafts.value = { ...campaignDrafts.value, [key]: draft }
  draft[field] = value || ""
  if (field === "strategy" && draft.strategy === "/") draft.series = ""
  saveError.value = ""
}
function classificationError(strategy, series) {
  if (strategy === "/" && series) return "策略为“/”时不能选择系列"
  if (strategy !== "/" && !series) return "已选择策略时必须同时选择系列"
  return ""
}
function invalidCampaignEntries() {
  const invalid = []
  rows.value.forEach((row) => (row.campaigns || []).forEach((campaign) => {
    const draft = campaignDraftFor(row, campaign)
    if (classificationError(draft.strategy, draft.series)) invalid.push({ row, campaign, draft, message: classificationError(draft.strategy, draft.series) })
  }))
  return invalid
}
function showSaveError(message) {
  saveError.value = message
  showToast("保存未执行，请查看弹窗提示")
}
function noteValue(row) { return noteDrafts.value[noteKey(row)] ?? row.note ?? "" }
async function saveAllNotes() {
  const items = rows.value.map((row) => ({ week_start: strategyWeekStart.value, site_code: row.site_code, series: row.series || "", strategy: row.strategy, note: noteValue(row) }))
  if (!items.length) { showToast("当前筛选范围没有策略"); return }
  saving.value = "notes"; saveError.value = ""
  try {
    const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/strategy-board/notes/batch`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ week_start: strategyWeekStart.value, items }) })
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
    rows.value.forEach((row) => { row.note = noteValue(row) }); showToast(`已保存 ${data.saved || items.length} 条优化方向`)
  } catch (e) { saveError.value = e.message || "优化方向保存失败" } finally { saving.value = "" }
}
async function saveAllCampaigns() {
  const campaigns = new Map()
  rows.value.forEach((row) => (row.campaigns || []).forEach((campaign) => campaigns.set(campaignKey(campaign), campaign)))
  const items = [...campaigns.values()].map((campaign) => {
    const draft = campaignDraftFor({}, campaign)
    return {
      site_code: campaign.site_code,
      store_sid: campaign.store_sid,
      store_name: campaign.store_name,
      campaign_id: campaign.campaign_id,
      campaign_name: campaign.campaign_name,
      strategy: draft.strategy || "/",
      series: draft.series || "",
      product: "",
    }
  })
  if (!items.length) { showToast("当前筛选范围没有广告活动"); return }
  const invalid = invalidCampaignEntries()
  if (invalid.length) {
    const nextExpanded = new Set(expanded.value)
    invalid.forEach((item) => nextExpanded.add(groupKey(item.row)))
    expanded.value = nextExpanded
    const names = invalid.slice(0, 3).map((item) => `“${item.campaign.campaign_name || item.campaign.campaign_id}”：${item.message}`)
    showSaveError(`${names.join("；")}${invalid.length > 3 ? `；另有 ${invalid.length - 3} 条同类问题` : ""}`)
    return
  }
  saving.value = "campaigns"
  saveError.value = ""
  try {
    const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/strategy-board/campaign-strategy/batch`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
    showToast(`已保存 ${data.saved || items.length} 条广告策略分类`)
    await load()
  } catch (e) { saveError.value = e.message || "广告活动归类保存失败" } finally { saving.value = "" }
}
async function loadStores() { try { const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/stores`); const data = await response.json(); stores.value = data.stores || [] } catch {} }
async function fetchStrategyBoard(weekStart) {
  const query = new URLSearchParams({ start_date: weekStart, end_date: weekEndFrom(weekStart), site: strategySite.value })
  if (strategyStore.value) query.append("store_sid", strategyStore.value)
  if (strategySeries.value) query.append("series", strategySeries.value)
  const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/strategy-board?${query}`)
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
  return data
}
async function load() {
  if (!props.apiBase || !strategyWeekStart.value) return
  const requestSeq = ++strategyRequestSeq
  loading.value = true; error.value = ""; saveError.value = ""
  try {
    const previousWeek = formatLocalDate(new Date(new Date(`${strategyWeekStart.value}T00:00:00`).getTime() - 7 * 86400000))
    const [data, previousData] = await Promise.all([
      fetchStrategyBoard(strategyWeekStart.value),
      showComparison.value ? fetchStrategyBoard(previousWeek) : Promise.resolve(null),
    ])
    if (requestSeq !== strategyRequestSeq) return
    rows.value = data.strategies || []; previousRows.value = previousData?.strategies || []
    seriesOptions.value = data.series_options || DEFAULT_SERIES
    const nextDrafts = {}; const nextCampaignDrafts = {}
    rows.value.forEach((row) => {
      nextDrafts[noteKey(row)] = row.note || ""
      ;(row.campaigns || []).forEach((campaign) => { nextCampaignDrafts[campaignKey(campaign)] = { strategy: campaign.strategy || row.strategy || "/", series: campaign.series || row.series || "" } })
    })
    noteDrafts.value = nextDrafts; campaignDrafts.value = nextCampaignDrafts
  } catch (e) {
    if (requestSeq === strategyRequestSeq) error.value = e.message || "广告策略看板加载失败"
  } finally {
    if (requestSeq === strategyRequestSeq) {
      loading.value = false
      await nextTick()
      resizeAllNotes()
    }
  }
}
function onStrategySiteChange() { if (!availableStores.value.some((item) => String(item.sid) === strategyStore.value)) strategyStore.value = "" }
function onWeekChange() { strategyWeekStart.value = monday(strategyWeekStart.value) }
watch(() => [props.apiBase, strategyWeekStart.value, strategySite.value, strategyStore.value, strategySeries.value, showComparison.value], load)
onMounted(async () => {
  loadColumnPreferences()
  strategyWeekStart.value = previousWeekStart()
  window.addEventListener("scroll", updateConfigPosition, true)
  window.addEventListener("resize", updateConfigPosition)
  await nextTick()
  await loadStores()
})
onBeforeUnmount(() => { resizeCleanup?.(); window.removeEventListener("scroll", updateConfigPosition, true); window.removeEventListener("resize", updateConfigPosition); if (toastTimer) window.clearTimeout(toastTimer) })
</script>

<template>
  <el-config-provider :locale="zhCn"><section class="strategy-board-panel">
    <div class="strategy-board-head"><div><span class="section-label">广告后台数据</span><h2>广告策略看板</h2><p>按周、站点、店铺和系列筛选；展开后可给每条广告活动设置策略和系列。</p></div><div class="strategy-board-actions"><label class="comparison-toggle" title="对比上一周"><input v-model="showComparison" type="checkbox"><span>查看环比</span></label><button ref="configButton" class="strategy-config-button" type="button" @click="toggleConfig">列配置</button><button class="strategy-refresh-button" type="button" :disabled="loading" @click="load">刷新</button></div></div>
    <div class="strategy-filter-bar"><label class="week-filter"><span>周 <b class="week-filter-code">{{ strategyWeekRangeLabel }}</b></span><div class="week-picker-control"><WeekPicker v-model="strategyWeekStart" @change="onWeekChange"/></div></label><label><span>站点</span><el-select v-model="strategySite" @change="onStrategySiteChange"><el-option v-for="item in orderedSites" :key="item" :label="item" :value="item" /></el-select><small class="filter-meta-spacer" aria-hidden="true"></small></label><label><span>店铺（可不选）</span><el-select v-model="strategyStore" clearable placeholder="全部店铺"><el-option v-for="item in availableStores" :key="item.sid" :label="`${item.name}（${item.sid}）`" :value="String(item.sid)" /></el-select><small class="filter-meta-spacer" aria-hidden="true"></small></label><label><span>系列</span><el-select v-model="strategySeries" clearable placeholder="全部系列"><el-option v-for="item in seriesOptions" :key="item" :label="displaySeries(item)" :value="item" /></el-select><small class="filter-meta-spacer" aria-hidden="true"></small></label><button class="campaign-batch-save-button" type="button" :disabled="loading || saving === 'campaigns' || !campaignCount" @click="saveAllCampaigns">{{ saving === "campaigns" ? "保存中" : "保存所有广告策略分类" }}</button></div>
    <Teleport to="body"><div v-if="configOpen" class="strategy-config-panel strategy-config-panel-floating" role="dialog" aria-label="广告策略列配置" :style="{ top: `${configPosition.top}px`, left: `${configPosition.left}px` }"><button v-for="column in columns" :key="column.key" type="button" @click="toggleColumn(column)"><span>{{ column.label }}</span><span>{{ visible[column.key] ? "◉" : "○" }}</span></button></div></Teleport>
    <div v-if="error" class="strategy-board-error">{{ error }}</div><div v-else-if="loading" class="strategy-board-loading">正在同步广告后台数据…</div><div v-else-if="!groups.length" class="strategy-board-empty">暂无广告活动数据</div><div v-else class="strategy-table-wrap" :style="tableHeight ? { height: `${tableHeight}px`, maxHeight: `${tableHeight}px` } : undefined" @wheel="handleTableWheel"><table class="strategy-table" :class="{ 'comparison-enabled': showComparison }" :style="tableStyle"><colgroup><col :style="{ width: `${nameColumnWidth}px` }"><col v-for="column in visibleColumns" :key="column.key" :style="{ width: `${widths[column.key]}px` }"><col :style="{ width: `${noteColumnWidth}px` }"></colgroup><thead><tr><th class="strategy-name-column">策略 / 系列<i class="strategy-resize-handle" title="拖动调整策略 / 系列列宽" @pointerdown="startNameResize"></i></th><th v-for="column in visibleColumns" :key="column.key" draggable="true" @dragstart="startDrag(column)" @dragover.prevent @drop="dropColumn(column)"><span class="strategy-column-drag-label">{{ column.label }}</span><button type="button" class="strategy-sort-button" :class="{ active: sort.key === column.key }" @click.stop="cycleSort(column)">{{ sort.key === column.key && sort.direction === "asc" ? "↑" : "↓" }}</button><i class="strategy-resize-handle" @pointerdown="startResize($event, column)"></i></th><th class="strategy-note-column"><div class="strategy-note-header"><span>优化方向</span><button class="strategy-note-save strategy-save-all-notes" type="button" :disabled="loading || saving === 'notes' || !groups.length" @click="saveAllNotes">{{ saving === "notes" ? "保存中" : "保存所有优化方向" }}</button></div><i class="strategy-resize-handle" title="拖动调整优化方向列宽" @pointerdown="startNoteResize"></i></th></tr></thead><tbody><template v-for="row in groups" :key="groupKey(row)"><tr class="strategy-group-row"><td class="strategy-name-column"><button type="button" class="strategy-expand-button" @click="toggle(row)">{{ expanded.has(groupKey(row)) ? "−" : "+" }}</button><strong>{{ row.strategy }}</strong><small>{{ groupMeta(row) }}</small></td><td v-for="column in visibleColumns" :key="column.key"><div class="metric-cell-stack"><span>{{ display(row.metrics?.[column.key], column, row.currency) }}</span><span v-if="showComparison" :class="['period-comparison-delta', rowComparison(row, column)?.status]">{{ rowComparison(row, column)?.text }}</span></div></td><td class="strategy-note-cell"><div class="strategy-note-editor"><textarea v-model="noteDrafts[noteKey(row)]" rows="1" placeholder="填写优化方向…" @input="resizeNote"></textarea></div></td></tr><template v-if="expanded.has(groupKey(row))"><tr v-for="campaign in row.campaigns" :key="`${campaign.site_code}:${campaign.store_sid}:${campaign.campaign_id}`" :class="['strategy-campaign-row', { invalid: classificationError(campaignDraftFor(row, campaign).strategy, campaignDraftFor(row, campaign).series) }]"><td class="strategy-name-column campaign-assignment-cell"><div class="campaign-selectors"><el-select :model-value="campaignDraftFor(row, campaign).strategy" size="small" @change="setCampaignDraft(campaign, 'strategy', $event)"><el-option v-for="option in STRATEGIES" :key="option" :label="option" :value="option" /></el-select><el-select :model-value="campaignDraftFor(row, campaign).series" size="small" clearable placeholder="系列" @change="setCampaignDraft(campaign, 'series', $event)"><el-option v-for="option in seriesOptions" :key="option" :label="displaySeries(option)" :value="option" /></el-select></div><button type="button" class="campaign-name" :title="`Campaign ID：${campaign.campaign_id}，点击复制`" @click="copyCampaignId(campaign)">{{ campaign.campaign_name || `未命名广告活动 · ${campaign.campaign_id}` }}</button><small class="campaign-meta">{{ campaign.store_name || (campaign.store_sid ? `店铺 ${campaign.store_sid}` : "店铺信息缺失") }} · {{ campaign.ad_type || "广告活动" }} · ID {{ campaign.campaign_id }}</small></td><td v-for="column in visibleColumns" :key="column.key"><div class="metric-cell-stack"><span>{{ display(campaign[column.key], column, campaign.currency) }}</span><span v-if="showComparison" :class="['period-comparison-delta', campaignComparison(campaign, column)?.status]">{{ campaignComparison(campaign, column)?.text }}</span></div></td><td></td></tr></template></template></tbody>
      <tfoot><tr class="strategy-summary-row"><td class="strategy-name-column strategy-summary-label"><strong>汇总</strong><small>当前筛选范围</small></td><td v-for="column in visibleColumns" :key="column.key"><div class="metric-cell-stack"><span>{{ display(summaryMetrics[column.key], column, summaryCurrency) }}</span><span v-if="showComparison" :class="['period-comparison-delta', summaryComparison(column)?.status]">{{ summaryComparison(column)?.text }}</span></div></td><td></td></tr></tfoot>
    </table></div><i class="table-height-resize-handle" role="separator" aria-orientation="horizontal" title="拖动调整表格高度" @pointerdown="startTableResize"></i>
    <div v-if="toast" class="amazon-copy-toast" role="status" aria-live="polite">{{ toast }}</div>
    <Teleport to="body">
      <div v-if="saveError" class="strategy-save-error-popover" role="alertdialog" aria-modal="false" aria-label="保存失败提示">
        <header><strong>任务保存未完成</strong><button type="button" @click="saveError = ''">×</button></header>
        <p>{{ saveError }}</p>
        <small>表格内容已保留；请补齐红色提示行后再保存。</small>
      </div>
    </Teleport>
  </section></el-config-provider>
</template>
