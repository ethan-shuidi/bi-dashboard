<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { ElMessageBox } from "element-plus"
import { fetchWithDashboardAuth } from "./dashboardAuth"
import WeekPicker from "./WeekPicker.vue"

const props = defineProps({ apiBase: { type: String, required: true } })

const siteOptions = ["美国", "日本", "德国", "英国", "法国", "加拿大", "澳洲", "西班牙", "意大利", "荷兰", "比利时", "墨西哥", "爱尔兰", "波兰", "瑞典"]
const categoryOptions = ["comu品牌词", "AI核心词", "类目词", "Plaud品牌词", "Pocket品牌词", "其他品牌词"]
const quickRangeOptions = [
  { label: "前5周", value: 5 },
  { label: "前10周", value: 10 },
  { label: "前15周", value: 15 },
]
const abaDataOptions = [
  { label: "全部", value: "ALL" },
  { label: "仅周搜索排名", value: "RANK" },
  { label: "仅周搜索量", value: "VOLUME" },
]
const site = ref("美国")
const quickRange = ref(10)
const abaData = ref("ALL")
const loading = ref(false)
const saving = ref(false)
const error = ref("")
const notice = ref("")
const data = ref(null)
const terms = ref([])
const savedTerms = ref([])
const endWeek = ref("")
const startWeek = ref("")
const addRowCount = ref(10)
const rowGap = ref(10)
const columnWidths = ref({})
const contextMenu = ref({ visible: false, sourceIndex: -1, x: 0, y: 0 })
const trendTooltip = ref(null)
const rowGapStorageKey = "ideadock.keyword-dashboard.row-gap.v1"
const columnWidthStorageKey = "ideadock.keyword-dashboard.column-widths.v1"
const columnWidthDefaults = {
  category: 150,
  keyword: 240,
  rankTrend: 150,
  volumeTrend: 150,
  weeklyMetric: 90,
}
const columnWidthBounds = {
  category: [110, 260],
  keyword: [180, 460],
  rankTrend: [110, 280],
  volumeTrend: [110, 280],
  weeklyMetric: [70, 190],
}
let resizeCleanup = null
let dashboardRequestSeq = 0

function parseDate(value) {
  const parsed = new Date(`${value}T00:00:00`)
  return Number.isNaN(parsed.getTime()) ? new Date() : parsed
}

function isoDate(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`
}

function keywordWeekStart(value) {
  const date = parseDate(value)
  date.setHours(0, 0, 0, 0)
  date.setDate(date.getDate() - date.getDay())
  return date
}

function addDays(value, amount) {
  const date = new Date(value)
  date.setDate(date.getDate() + amount)
  return date
}

function isoWeek(value) {
  const anchor = addDays(keywordWeekStart(value), 4)
  const firstThursday = new Date(anchor.getFullYear(), 0, 4)
  const firstMonday = new Date(firstThursday)
  firstMonday.setDate(firstThursday.getDate() - ((firstThursday.getDay() + 6) % 7))
  return Math.floor((anchor - firstMonday) / 604800000) + 1
}

function latestCompletedWeek() {
  return addDays(keywordWeekStart(new Date()), -7)
}

function applyQuickRange(weekCount) {
  const end = latestCompletedWeek()
  endWeek.value = isoDate(end)
  startWeek.value = isoDate(addDays(end, -(Number(weekCount) - 1) * 7))
}

function syncQuickRange() {
  const currentWeek = latestCompletedWeek()
  const matched = quickRangeOptions.find(({ value }) => (
    startWeek.value === isoDate(addDays(currentWeek, -(value - 1) * 7)) &&
    endWeek.value === isoDate(currentWeek)
  ))
  quickRange.value = matched?.value ?? null
}

async function api(path, options = {}) {
  const response = await fetchWithDashboardAuth(`${props.apiBase}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = body?.detail
    if (typeof detail === "string" && detail) throw new Error(detail)
    if (Array.isArray(detail) && detail.length) throw new Error(detail[0]?.msg || `请求失败：HTTP ${response.status}`)
    throw new Error(`请求失败：HTTP ${response.status}`)
  }
  return body
}

const weeks = computed(() => data.value?.weeks || [])
const dirty = computed(() => JSON.stringify(termPayload()) !== JSON.stringify(savedTerms.value))
const dataRows = computed(() => data.value?.rows || [])
const dataRowMap = computed(() => new Map(dataRows.value.map((row) => [String(row.keyword || "").trim().toLowerCase(), row])))
const showRank = computed(() => abaData.value === "ALL" || abaData.value === "RANK")
const showVolume = computed(() => abaData.value === "ALL" || abaData.value === "VOLUME")
const visibleMetricCount = computed(() => Number(showRank.value) + Number(showVolume.value))
const latestDataWeekIndex = computed(() => {
  for (let index = weeks.value.length - 1; index >= 0; index -= 1) {
    const hasData = dataRows.value.some((row) => {
      const item = row.weekly?.[index]
      const hasRank = showRank.value && item?.search_rank !== null && item?.search_rank !== undefined
      const hasVolume = showVolume.value && item?.search_volume !== null && item?.search_volume !== undefined
      return hasRank || hasVolume
    })
    if (hasData) return index
  }
  return weeks.value.length ? weeks.value.length - 1 : 0
})
const visibleWeekIndices = computed(() => weeks.value.slice(0, latestDataWeekIndex.value + 1).map((_, index) => index))
const displayWeeks = computed(() => visibleWeekIndices.value.slice().reverse().map((index) => weeks.value[index]))
const weekIndexByStart = computed(() => new Map(weeks.value.map((week, index) => [week.start, index])))
const tableMinWidth = computed(() => {
  const widths = [
    columnWidth("category"),
    columnWidth("keyword"),
    ...(showRank.value ? [columnWidth("rankTrend")] : []),
    ...(showVolume.value ? [columnWidth("volumeTrend")] : []),
  ]
  for (const week of displayWeeks.value) {
    if (showRank.value) widths.push(weekColumnWidth(week, "rank"))
    if (showVolume.value) widths.push(weekColumnWidth(week, "volume"))
  }
  return widths.reduce((total, width) => total + width, 0)
})

function columnWidth(key) {
  const bounds = columnWidthBounds[key]
  const fallback = columnWidthDefaults[key]
  const value = Number(columnWidths.value[key])
  if (!bounds || !Number.isFinite(value)) return fallback
  return Math.round(Math.max(bounds[0], Math.min(bounds[1], value)))
}

function weeklyColumnKey(week, field) {
  return `week:${week.start}:${field}`
}

function weekColumnWidth(week, field) {
  const key = weeklyColumnKey(week, field)
  const value = Number(columnWidths.value[key])
  const fallback = columnWidthDefaults.weeklyMetric
  if (!Number.isFinite(value)) return fallback
  return Math.round(Math.max(columnWidthBounds.weeklyMetric[0], Math.min(columnWidthBounds.weeklyMetric[1], value)))
}

function saveColumnWidths() {
  window.localStorage.setItem(columnWidthStorageKey, JSON.stringify(columnWidths.value))
}

function startColumnResize(event, key, getWidth) {
  event.preventDefault()
  event.stopPropagation()
  resizeCleanup?.()
  const startX = event.clientX
  const startWidth = getWidth()
  const previousUserSelect = document.body.style.userSelect
  const previousCursor = document.body.style.cursor
  const move = (moveEvent) => {
    const bounds = columnWidthBounds[key] || columnWidthBounds.weeklyMetric
    columnWidths.value = {
      ...columnWidths.value,
      [key]: Math.max(bounds[0], Math.min(bounds[1], Math.round(startWidth + moveEvent.clientX - startX))),
    }
  }
  const stop = () => {
    window.removeEventListener("pointermove", move)
    window.removeEventListener("pointerup", stop)
    document.body.style.userSelect = previousUserSelect
    document.body.style.cursor = previousCursor
    resizeCleanup = null
    saveColumnWidths()
  }
  resizeCleanup = stop
  document.body.style.userSelect = "none"
  document.body.style.cursor = "col-resize"
  window.addEventListener("pointermove", move)
  window.addEventListener("pointerup", stop, { once: true })
}

function resetColumnWidths() {
  columnWidths.value = {}
  window.localStorage.removeItem(columnWidthStorageKey)
  notice.value = "Amazon-ABA排名列宽已恢复默认"
  error.value = ""
}

function normalizeCategory(value) {
  const category = String(value || "").trim()
  return categoryOptions.includes(category) ? category : "其他品牌词"
}

function normalizeTerms(payload) {
  return (payload?.terms || []).map((item) => ({
    category: normalizeCategory(item.category),
    keyword: String(item.keyword || "").trim(),
  }))
}

function termPayload() {
  return terms.value.map((item, index) => ({
    category: normalizeCategory(item.category),
    keyword: String(item.keyword || "").trim(),
    sort_order: index,
    enabled: true,
  }))
}

async function loadTerms(nextSite = site.value) {
  const payload = await api(`/api/keyword-dashboard/terms?site=${encodeURIComponent(nextSite)}`)
  terms.value = normalizeTerms(payload)
  savedTerms.value = termPayload()
}

async function loadDashboard() {
  if (!props.apiBase || !startWeek.value || !endWeek.value) return
  const requestSeq = ++dashboardRequestSeq
  loading.value = true
  error.value = ""
  try {
    const query = new URLSearchParams({
      site: site.value,
      start_week: startWeek.value,
      end_week: endWeek.value,
    })
    data.value = await api(`/api/keyword-dashboard?${query}`)
    if (requestSeq !== dashboardRequestSeq) return
    if (data.value?.terms?.length && !terms.value.length) {
      terms.value = normalizeTerms(data.value)
      savedTerms.value = termPayload()
    }
    if (!dirty.value) applySavedDashboardOrder()
    const warnings = data.value?.warnings || []
    if (warnings.length) error.value = warnings.join("；")
  } catch (exception) {
    if (requestSeq !== dashboardRequestSeq) return
    error.value = exception.message || "关键词数据加载失败"
  } finally {
    if (requestSeq === dashboardRequestSeq) loading.value = false
  }
}

async function loadAll() {
  const requestSeq = ++dashboardRequestSeq
  loading.value = true
  error.value = ""
  try {
    await loadTerms()
    if (requestSeq !== dashboardRequestSeq) return
    await loadDashboard()
  } catch (exception) {
    if (requestSeq !== dashboardRequestSeq) return
    error.value = exception.message || "关键词配置加载失败"
  } finally {
    if (requestSeq === dashboardRequestSeq) loading.value = false
  }
}

function changeSite(nextSite) {
  if (nextSite === site.value) return
  if (dirty.value && !window.confirm("当前站点关键词尚未保存，切换后将丢失未保存的修改。是否继续？")) return
  site.value = nextSite
  notice.value = ""
}

function addTerms() {
  const count = Number(addRowCount.value)
  if (!Number.isInteger(count) || count < 1) {
    error.value = "新增行数必须为正整数"
    return
  }
  if (terms.value.length + count > 100) {
    error.value = `每个站点最多支持 100 个关键词，当前还可新增 ${Math.max(100 - terms.value.length, 0)} 行`
    return
  }
  const category = terms.value[terms.value.length - 1]?.category || categoryOptions[0]
  for (let index = 0; index < count; index += 1) {
    terms.value.push({ category, keyword: "" })
  }
  error.value = ""
  notice.value = ""
}

function removeTerm(index) {
  terms.value.splice(index, 1)
  contextMenu.value.visible = false
  notice.value = ""
}

function openKeywordMenu(event, sourceIndex) {
  contextMenu.value = {
    visible: true,
    sourceIndex,
    x: Math.min(event.clientX, window.innerWidth - 130),
    y: Math.min(event.clientY, window.innerHeight - 90),
  }
}

function closeContextMenu() {
  contextMenu.value.visible = false
}

function onKeywordPaste(event, sourceIndex) {
  const text = event.clipboardData?.getData("text") || ""
  const values = text.split(/[\r\n,;\t]+/).map((item) => item.trim()).filter(Boolean)
  if (values.length <= 1) return
  event.preventDefault()
  const category = terms.value[sourceIndex]?.category || categoryOptions[0]
  terms.value.splice(sourceIndex, 1, ...values.map((keyword) => ({ category, keyword })))
  notice.value = ""
}

function applySavedDashboardOrder() {
  const orderedRows = dataRows.value
  if (!orderedRows.length || !terms.value.length) return
  const rankByKey = new Map(orderedRows.map((row, index) => [String(row.keyword || "").trim().toLowerCase(), index]))
  const originalIndexByKey = new Map(terms.value.map((item, index) => [String(item.keyword || "").trim().toLowerCase(), index]))
  terms.value = [...terms.value].sort((left, right) => (
    (rankByKey.get(String(left.keyword || "").trim().toLowerCase()) ?? orderedRows.length + (
      originalIndexByKey.get(String(left.keyword || "").trim().toLowerCase()) ?? 0
    )) -
    (rankByKey.get(String(right.keyword || "").trim().toLowerCase()) ?? orderedRows.length + (
      originalIndexByKey.get(String(right.keyword || "").trim().toLowerCase()) ?? 0
    ))
  ))
  savedTerms.value = termPayload()
}

async function saveTerms() {
  const payload = termPayload()
  if (payload.some((item) => !item.keyword)) {
    error.value = "关键词不能为空"
    return
  }
  if (new Set(payload.map((item) => item.keyword.toLowerCase())).size !== payload.length) {
    error.value = "关键词不能重复"
    return
  }
  saving.value = true
  error.value = ""
  notice.value = ""
  try {
    const saved = await api("/api/keyword-dashboard/terms", {
      method: "POST",
      body: JSON.stringify({ site: site.value, terms: payload }),
    })
    terms.value = normalizeTerms(saved)
    savedTerms.value = termPayload()
    await loadDashboard()
    notice.value = error.value ? "关键词配置已保存，部分 ABA 数据未拉取" : "关键词配置已保存，排序与历史数据已更新"
  } catch (exception) {
    error.value = exception.message || "关键词配置保存失败"
  } finally {
    saving.value = false
  }
}

async function refreshDashboard() {
  if (!terms.value.length) {
    error.value = "请先添加并保存关键词"
    return
  }
  if (dirty.value) {
    error.value = "关键词配置有未保存修改，请先保存后再刷新"
    return
  }
  try {
    await ElMessageBox.confirm("数据已经抓取完成，抓取需要消耗Credit，是否继续？", "刷新确认", {
      confirmButtonText: "继续",
      cancelButtonText: "取消",
      type: "warning",
    })
  } catch {
    return
  }
  loading.value = true
  error.value = ""
  notice.value = ""
  try {
    const query = new URLSearchParams({
      site: site.value,
      start_week: startWeek.value,
      end_week: endWeek.value,
      refresh: "true",
    })
    data.value = await api(`/api/keyword-dashboard?${query}`)
    if (!dirty.value) applySavedDashboardOrder()
    const warnings = data.value?.warnings || []
    if (warnings.length) error.value = warnings.join("；")
    else notice.value = "ABA 数据已重新抓取并写入历史库"
  } catch (exception) {
    error.value = exception.message || "ABA 数据刷新失败"
  } finally {
    loading.value = false
  }
}

const tableRows = computed(() => {
  return terms.value.map((item, sourceIndex) => {
    const source = dataRowMap.value.get(String(item.keyword || "").trim().toLowerCase())
    const weekly = source?.weekly || weeks.value.map((week) => ({
      week_start: week.start,
      week_end: week.end,
      label: week.label,
      search_rank: null,
      search_volume: null,
    }))
    return {
      sourceIndex,
      category: item.category,
      keyword: item.keyword,
      weekly,
      latest_search_rank: weekly.reduce((result, item) => item.search_rank ?? result, null),
    }
  })
})

function formatNumber(value) {
  if (value === null || value === undefined) return "—"
  return new Intl.NumberFormat("zh-CN").format(Number(value))
}

function metricClass(row, index, field) {
  const current = row.weekly[index]?.[field]
  const previous = row.weekly[index - 1]?.[field]
  if (current === null || current === undefined || previous === null || previous === undefined) return "flat"
  if (field === "search_rank") {
    if (current < previous) return "up"
    if (current > previous) return "down"
  } else {
    if (current > previous) return "up"
    if (current < previous) return "down"
  }
  return "flat"
}

function sparkline(row, field) {
  const values = row.weekly.map((item) => item[field])
  const visibleIndexes = [...visibleWeekIndices.value].sort((left, right) => left - right)
  const firstVisibleIndex = visibleIndexes[0] ?? 0
  const lastVisibleIndex = visibleIndexes[visibleIndexes.length - 1] ?? Math.max(values.length - 1, 0)
  const points = values
    .map((value, index) => ({ value, index }))
    .filter((item) => visibleWeekIndices.value.includes(item.index))
    .filter((item) => item.value !== null && item.value !== undefined)
  if (points.length < 2) return null
  const min = Math.min(...points.map((item) => item.value))
  const max = Math.max(...points.map((item) => item.value))
  const hasExtreme = min !== max
  const span = max - min || 1
  const coordinates = points.map((item) => ({
    ...item,
    x: (item.index - firstVisibleIndex) / Math.max(lastVisibleIndex - firstVisibleIndex, 1) * 100,
    y: 5 + (item.value - min) / span * 26,
    is_min: hasExtreme && item.value === min,
    is_max: hasExtreme && item.value === max,
  }))
  return {
    path: coordinates.map((item, index) => `${index ? "L" : "M"}${item.x.toFixed(2)},${item.y.toFixed(2)}`).join(" "),
    points: coordinates,
  }
}

function weekIndex(start) {
  return weekIndexByStart.value.get(start) ?? -1
}

function sparklineTitle(index, field, value) {
  const week = weeks.value[index]
  const label = week?.label || `W${String(isoWeek(week?.start || new Date())).padStart(2, "0")}`
  return `${label} · ${field === "search_rank" ? "周搜索排名" : "周搜索量"} ${formatNumber(value)}`
}

function positionTrendTooltip(event) {
  if (!trendTooltip.value) return
  trendTooltip.value = {
    ...trendTooltip.value,
    x: Math.max(12, Math.min(event.clientX + 12, window.innerWidth - 196)),
    y: Math.max(12, Math.min(event.clientY - 40, window.innerHeight - 66)),
  }
}

function showTrendTooltip(event, point, field) {
  trendTooltip.value = {
    text: sparklineTitle(point.index, field, point.value),
    x: 0,
    y: 0,
  }
  positionTrendTooltip(event)
}

function moveTrendTooltip(event) {
  positionTrendTooltip(event)
}

function hideTrendTooltip() {
  trendTooltip.value = null
}

watch(startWeek, (value) => {
  if (value && endWeek.value && value > endWeek.value) startWeek.value = endWeek.value
  syncQuickRange()
})
watch(endWeek, (value) => {
  const latest = isoDate(latestCompletedWeek())
  if (value && value > latest) {
    endWeek.value = latest
    notice.value = "西柚 ABA 仅支持已完成周，已自动切换到上一个已完成周"
    return
  }
  if (value && startWeek.value && value < startWeek.value) startWeek.value = value
  syncQuickRange()
})
watch(() => [props.apiBase, site.value], async ([nextApiBase], oldValues) => {
  if (!nextApiBase) return
  if (oldValues && oldValues[1] === site.value) return
  await loadAll()
})
watch(() => [props.apiBase, startWeek.value, endWeek.value], ([nextApiBase], oldValues) => {
  if (!nextApiBase) return
  if (oldValues && oldValues[0] === props.apiBase) loadDashboard()
})

applyQuickRange(10)
onMounted(() => {
  const savedRowGap = Number(window.localStorage.getItem(rowGapStorageKey))
  if (Number.isFinite(savedRowGap) && savedRowGap >= 4 && savedRowGap <= 18) rowGap.value = savedRowGap
  try {
    const savedWidths = JSON.parse(window.localStorage.getItem(columnWidthStorageKey) || "{}")
    if (savedWidths && typeof savedWidths === "object" && !Array.isArray(savedWidths)) columnWidths.value = savedWidths
  } catch {
    columnWidths.value = {}
  }
  document.addEventListener("click", closeContextMenu)
  document.addEventListener("scroll", closeContextMenu, true)
  document.addEventListener("scroll", hideTrendTooltip, true)
  loadAll()
})
watch(rowGap, (value) => {
  window.localStorage.setItem(rowGapStorageKey, String(value))
})
onBeforeUnmount(() => {
  resizeCleanup?.()
  document.removeEventListener("click", closeContextMenu)
  document.removeEventListener("scroll", closeContextMenu, true)
  document.removeEventListener("scroll", hideTrendTooltip, true)
})
</script>

<template>
  <section class="keyword-dashboard" aria-label="Amazon-ABA排名">
    <header class="keyword-head">
      <div>
        <span>西柚 ABA 数据</span>
        <h1>Amazon-ABA排名</h1>
        <p>按分类与最新排名自动排序，历史 ABA 数据入云端后不再重复抓取。</p>
      </div>
      <div class="keyword-head-actions">
        <span v-if="data" :class="['keyword-cache', { cached: data.cached }]">{{ data.cached ? "历史数据" : "新抓取" }}</span>
        <button type="button" :disabled="loading" @click="refreshDashboard">{{ loading ? "同步中…" : "刷新" }}</button>
        <button class="primary" type="button" :disabled="saving || loading" @click="saveTerms">{{ saving ? "保存中…" : dirty ? "保存*" : "保存" }}</button>
      </div>
    </header>

    <section class="keyword-filters" aria-label="搜索词筛选">
      <label>
        <span>快速选择</span>
        <select v-model="quickRange" @change="applyQuickRange(quickRange)">
          <option v-for="item in quickRangeOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <label><span>开始周</span><WeekPicker v-model="startWeek" :week-starts-on="0" /></label>
      <label><span>结束周</span><WeekPicker v-model="endWeek" :week-starts-on="0" /></label>
      <label>
        <span>站点</span>
        <select :value="site" @change="changeSite($event.target.value)">
          <option v-for="item in siteOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label>
        <span>ABA数据</span>
        <select v-model="abaData">
          <option v-for="item in abaDataOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <label class="row-gap-control" aria-label="表格行距">
        <span>行距</span>
        <input v-model.number="rowGap" type="range" min="4" max="18" step="1">
        <small>{{ rowGap }}px</small>
      </label>
      <div class="column-width-control">
        <button type="button" @click="resetColumnWidths">恢复默认列宽</button>
      </div>
    </section>

    <section v-if="error" class="keyword-message error" role="alert">{{ error }}</section>
    <section v-if="notice" class="keyword-message success" role="status">{{ notice }}</section>
    <section v-if="loading && !tableRows.length" class="keyword-message muted">正在获取西柚 ABA 数据…</section>

    <section class="keyword-table-panel" aria-label="搜索词周度数据">
      <div class="keyword-table-wrap">
        <table :style="{ width: `${tableMinWidth}px`, minWidth: `${tableMinWidth}px`, '--keyword-row-gap': `${rowGap}px` }">
          <colgroup>
            <col :style="{ width: `${columnWidth('category')}px` }">
            <col :style="{ width: `${columnWidth('keyword')}px` }">
            <col v-if="showRank" :style="{ width: `${columnWidth('rankTrend')}px` }">
            <col v-if="showVolume" :style="{ width: `${columnWidth('volumeTrend')}px` }">
            <template v-for="week in displayWeeks" :key="`${week.start}-columns`">
              <col v-if="showRank" :style="{ width: `${weekColumnWidth(week, 'rank')}px` }">
              <col v-if="showVolume" :style="{ width: `${weekColumnWidth(week, 'volume')}px` }">
            </template>
          </colgroup>
          <thead>
            <tr>
              <th class="category" rowspan="2">
                <span>分类</span>
                <i
                  class="column-resize-handle"
                  role="separator"
                  aria-orientation="vertical"
                  title="拖动调整分类列宽"
                  @pointerdown="startColumnResize($event, 'category', () => columnWidth('category'))"
                ></i>
              </th>
              <th class="keyword" rowspan="2">
                <span>关键词</span>
                <i
                  class="column-resize-handle"
                  role="separator"
                  aria-orientation="vertical"
                  title="拖动调整关键词列宽"
                  @pointerdown="startColumnResize($event, 'keyword', () => columnWidth('keyword'))"
                ></i>
              </th>
              <th v-if="showRank" class="trend" rowspan="2">
                <span>搜索排名趋势</span>
                <i
                  class="column-resize-handle"
                  role="separator"
                  aria-orientation="vertical"
                  title="拖动调整搜索排名趋势列宽"
                  @pointerdown="startColumnResize($event, 'rankTrend', () => columnWidth('rankTrend'))"
                ></i>
              </th>
              <th v-if="showVolume" class="trend" rowspan="2">
                <span>搜索量趋势</span>
                <i
                  class="column-resize-handle"
                  role="separator"
                  aria-orientation="vertical"
                  title="拖动调整搜索量趋势列宽"
                  @pointerdown="startColumnResize($event, 'volumeTrend', () => columnWidth('volumeTrend'))"
                ></i>
              </th>
              <th
                v-for="week in displayWeeks"
                :key="week.start"
                :class="['week-group', { single: visibleMetricCount === 1 }]"
                :colspan="visibleMetricCount"
              >
                <strong>{{ week.label || `W${String(isoWeek(week.start)).padStart(2, "0")}` }}</strong>
                <small>{{ week.start.slice(5) }}~{{ week.end.slice(5) }}</small>
              </th>
            </tr>
            <tr>
              <template v-for="week in displayWeeks" :key="`${week.start}-metrics`">
                <th v-if="showRank" class="metric-head rank">
                  <span>周搜索排名</span>
                  <i
                    class="column-resize-handle"
                    role="separator"
                    aria-orientation="vertical"
                    title="拖动调整周搜索排名列宽"
                    @pointerdown="startColumnResize($event, weeklyColumnKey(week, 'rank'), () => weekColumnWidth(week, 'rank'))"
                  ></i>
                </th>
                <th v-if="showVolume" class="metric-head volume">
                  <span>周搜索量</span>
                  <i
                    class="column-resize-handle"
                    role="separator"
                    aria-orientation="vertical"
                    title="拖动调整周搜索量列宽"
                    @pointerdown="startColumnResize($event, weeklyColumnKey(week, 'volume'), () => weekColumnWidth(week, 'volume'))"
                  ></i>
                </th>
              </template>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in tableRows" :key="row.sourceIndex">
              <td class="category-cell">
                <select v-model="terms[row.sourceIndex].category" class="category-select" aria-label="关键词分类">
                  <option v-for="item in categoryOptions" :key="item" :value="item">{{ item }}</option>
                </select>
              </td>
              <th scope="row">
                <input
                  v-model="terms[row.sourceIndex].keyword"
                  type="text"
                  maxlength="255"
                  placeholder="关键词"
                  aria-label="关键词"
                  @paste="onKeywordPaste($event, row.sourceIndex)"
                  @contextmenu.prevent="openKeywordMenu($event, row.sourceIndex)"
                >
              </th>
              <td v-if="showRank" class="trend">
                <svg v-if="sparkline(row, 'search_rank')" viewBox="0 0 100 36" preserveAspectRatio="none" role="img" :aria-label="`${row.keyword} ABA 搜索排名趋势`">
                  <path :d="sparkline(row, 'search_rank').path" />
                  <circle
                    v-for="point in sparkline(row, 'search_rank').points"
                    :key="point.index"
                    :cx="point.x"
                    :cy="point.y"
                    r="1.8"
                    :class="{ extreme: point.is_min || point.is_max }"
                    @mouseenter="showTrendTooltip($event, point, 'search_rank')"
                    @mousemove="moveTrendTooltip"
                    @mouseleave="hideTrendTooltip"
                  />
                </svg>
                <span v-else class="no-trend">数据不足</span>
              </td>
              <td v-if="showVolume" class="trend volume-trend">
                <svg v-if="sparkline(row, 'search_volume')" viewBox="0 0 100 36" preserveAspectRatio="none" role="img" :aria-label="`${row.keyword} ABA 搜索量趋势`">
                  <path :d="sparkline(row, 'search_volume').path" />
                  <circle
                    v-for="point in sparkline(row, 'search_volume').points"
                    :key="point.index"
                    :cx="point.x"
                    :cy="point.y"
                    r="1.8"
                    :class="{ extreme: point.is_min || point.is_max }"
                    @mouseenter="showTrendTooltip($event, point, 'search_volume')"
                    @mousemove="moveTrendTooltip"
                    @mouseleave="hideTrendTooltip"
                  />
                </svg>
                <span v-else class="no-trend">数据不足</span>
              </td>
              <template v-for="week in displayWeeks" :key="week.start">
                <td
                  v-if="showRank"
                  :class="['metric', metricClass(row, weekIndex(week.start), 'search_rank')]"
                >
                  {{ formatNumber(row.weekly[weekIndex(week.start)]?.search_rank) }}
                </td>
                <td
                  v-if="showVolume"
                  :class="['metric', metricClass(row, weekIndex(week.start), 'search_volume')]"
                >
                  {{ formatNumber(row.weekly[weekIndex(week.start)]?.search_volume) }}
                </td>
              </template>
            </tr>
          </tbody>
        </table>
        <div class="table-footer">
          <div class="add-row-control">
            <input
              v-model.number="addRowCount"
              type="number"
              min="1"
              max="100"
              step="1"
              aria-label="新增行数"
            >
            <button type="button" class="add-row" title="新增关键词" aria-label="新增关键词" @click="addTerms">+</button>
          </div>
          <span>输入行数后点击 + 新增；新增/删除仅保存在当前表格，点击右上角“保存”后才写入云端。</span>
        </div>
        <div v-if="tableRows.length && !weeks.length" class="keyword-empty">当前周范围没有数据</div>
        <div v-else-if="!tableRows.length && !loading" class="keyword-empty">点击左下角 + 添加关键词，选择分类后保存。</div>
      </div>

      <div
        v-if="contextMenu.visible"
        class="keyword-context-menu"
        :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }"
        @click.stop
      >
        <button type="button" @click="removeTerm(contextMenu.sourceIndex)">删除</button>
      </div>

      <div
        v-if="trendTooltip"
        class="trend-tooltip"
        :style="{ left: `${trendTooltip.x}px`, top: `${trendTooltip.y}px` }"
      >
        {{ trendTooltip.text }}
      </div>
    </section>
  </section>
</template>

<style scoped>
.keyword-dashboard{min-width:0;padding:22px;color:#17324d}
.keyword-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.keyword-head span{color:#6d7f95;font-size:12px;font-weight:750}.keyword-head h1{margin:3px 0 0;color:#12315d;font-size:24px}.keyword-head p{margin:6px 0 0;color:#6d7f95;font-size:13px}.keyword-head-actions{display:flex;align-items:center;gap:8px}.keyword-head-actions button{height:36px;padding:0 14px;border:1px solid #c9dbf0;border-radius:9px;background:#fff;color:#24589d;font:inherit;font-size:13px;font-weight:700;cursor:pointer}.keyword-head-actions .primary{border-color:#1e57c8;background:#1e57c8;color:#fff}.keyword-head-actions button:disabled{opacity:.6;cursor:not-allowed}.keyword-cache{height:28px;display:inline-flex;align-items:center;padding:0 10px;border-radius:999px;background:#eef3f9;color:#65778c;font-size:12px;font-weight:750}.keyword-cache.cached{background:#e8f7ef;color:#19704b}
.keyword-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:18px;padding:14px;border:1px solid #e2ebf6;border-radius:14px;background:#f8fbff}.keyword-filters label,.column-width-control{display:grid;gap:6px;min-width:0}.keyword-filters span{color:#5f7188;font-size:12px;font-weight:750}.keyword-filters select{width:100%;height:42px;padding:0 10px;border:1px solid #d5e2f1;border-radius:9px;background:#fff;color:#26466d;font:inherit}.row-gap-control{align-content:center}.row-gap-control input{width:100%;height:24px;margin:5px 0;accent-color:#1e57c8}.row-gap-control small{color:#526b88;font-size:12px;font-weight:750;text-align:right}.column-width-control{align-content:end}.column-width-control button{height:42px;padding:0 12px;border:1px solid #bfd7f1;border-radius:9px;background:#fff;color:#24589d;font:inherit;font-size:13px;font-weight:750;cursor:pointer}.column-width-control button:hover{background:#f2f8ff}
.keyword-message{margin-top:16px;padding:12px 14px;border-radius:10px;font-size:13px}.keyword-message.error{background:#fff2f4;color:#ad2745}.keyword-message.success{background:#edfaf3;color:#17724c}.keyword-message.muted{background:#f7fafd;color:#6d7f95}
.keyword-table-panel{position:relative;z-index:1;margin-top:16px;border:1px solid #e2ebf6;border-radius:16px;background:#fff;box-shadow:0 12px 28px rgba(28,63,111,.07)}.keyword-table-wrap{overflow:auto;overscroll-behavior-x:contain;border-radius:16px}table{border-collapse:collapse;table-layout:fixed}th,td{padding:10px;border-bottom:1px solid #e9f0f8;text-align:left;vertical-align:middle;overflow:hidden}thead tr:nth-child(1) th{position:sticky;top:0;z-index:4;background:#f4f8fd;color:#455f7c;font-size:12px}thead tr:nth-child(2) th{position:sticky;top:40px;z-index:4;background:#eaf1f9;color:#526b88;font-size:11px;font-weight:750}thead th>span{display:inline-block;max-width:calc(100% - 12px);vertical-align:middle}.keyword input,.category-select{width:100%;height:34px;padding:0 9px;text-align:center;border:1px solid #d5e2f1;border-radius:8px;background:#fff;color:#26466d;font:inherit;font-size:13px}.keyword input:focus,.category-select:focus{border-color:#6ea8dd;outline:2px solid rgba(46,120,193,.14)}.category,.keyword,.category-cell{text-align:center}.week-group{text-align:center}.week-group small{display:block;margin-top:2px;color:#7f90a5;font-weight:500}.metric-head{text-align:center;padding-right:10px}.metric-head.rank{background:#eef4fb}.metric-head.volume{background:#f6f9fd}.trend svg{display:block;width:100%;height:36px}.trend path{fill:none;stroke:#2f80ed;stroke-width:2;vector-effect:non-scaling-stroke}.trend circle{cursor:pointer;fill:#fff;stroke:#2f80ed;stroke-width:1;vector-effect:non-scaling-stroke}.trend circle.extreme{fill:#d93025;stroke:#d93025}.volume-trend path{stroke:#12a05f}.volume-trend circle{stroke:#12a05f}.no-trend{color:#93a2b4;font-size:12px}.column-resize-handle{position:absolute;top:0;right:0;bottom:0;z-index:6;width:9px;margin-right:-4.5px;cursor:col-resize;touch-action:none;background:linear-gradient(90deg,transparent 3px,rgba(30,87,200,.28) 3px,rgba(30,87,200,.28) 5px,transparent 5px)}.column-resize-handle:hover,.column-resize-handle:active{background:linear-gradient(90deg,transparent 2px,#1e57c8 2px,#1e57c8 6px,transparent 6px)}tbody th,tbody td{padding-top:var(--keyword-row-gap,10px);padding-bottom:var(--keyword-row-gap,10px)}tbody td{color:#26466d;font-size:13px}tbody td.metric{text-align:center;font-variant-numeric:tabular-nums}tbody td.metric.up{color:#c62838;font-weight:800}tbody td.metric.down{color:#16845b;font-weight:800}tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
.table-footer{position:sticky;left:0;display:flex;align-items:center;gap:10px;padding:12px 14px;background:#fff}.add-row-control{display:flex;align-items:center;gap:8px}.add-row-control input{width:76px;height:34px;padding:0 9px;border:1px solid #d5e2f1;border-radius:9px;background:#fff;color:#26466d;font:inherit;font-size:13px}.add-row-control input:focus{border-color:#6ea8dd;outline:2px solid rgba(46,120,193,.14)}.add-row{width:34px;height:34px;border:1px dashed #9dbbe0;border-radius:50%;background:#f7fbff;color:#2567b7;font-size:20px;line-height:1;cursor:pointer}.add-row:hover{border-style:solid;background:#edf5ff}.table-footer span{color:#71819a;font-size:12px}
.keyword-context-menu{position:fixed;z-index:3000;min-width:96px;padding:6px;border:1px solid #d8e3ef;border-radius:9px;background:#fff;box-shadow:0 14px 30px rgba(25,58,102,.18)}.keyword-context-menu button{width:100%;height:30px;border:0;border-radius:6px;background:transparent;color:#26466d;font:inherit;font-size:13px;cursor:pointer}.keyword-context-menu button:hover{background:#f2f7ff;color:#b32638}
.trend-tooltip{position:fixed;z-index:4000;max-width:184px;padding:7px 9px;border:1px solid #d8e3ef;border-radius:8px;background:#fff;color:#26466d;font-size:12px;font-weight:700;line-height:1.35;box-shadow:0 12px 26px rgba(25,58,102,.18);pointer-events:none;white-space:nowrap}
.keyword-empty{padding:24px;border-radius:10px;background:#f8fbff;color:#71819a;font-size:13px;text-align:center}
@media (max-width:1100px){.keyword-filters{grid-template-columns:1fr 1fr}.keyword-head{align-items:flex-start}.keyword-head-actions{flex-wrap:wrap}}
@media (max-width:720px){.keyword-filters{grid-template-columns:1fr}.keyword-table-panel{margin-inline:-22px;border-radius:0}}
</style>
