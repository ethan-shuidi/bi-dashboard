<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue"
import { fetchWithDashboardAuth } from "./dashboardAuth"

const props = defineProps({
  apiBase: { type: String, default: "" },
  startDate: { type: String, default: "" },
  endDate: { type: String, default: "" },
  sites: { type: Array, default: () => [] },
})

const rows = ref([])
const loading = ref(false)
const error = ref("")
const expanded = ref(new Set())
const saving = ref("")
const noteDrafts = ref({})
const columns = [
  { key: "clicks", label: "点击量", width: 100 },
  { key: "cpc", label: "CPC", width: 110, money: true },
  { key: "ad_cost", label: "广告花费", width: 130, money: true },
  { key: "ad_sales", label: "广告销售额", width: 135, money: true },
  { key: "acos", label: "ACOS", width: 105, percent: true },
  { key: "roas", label: "ROAS", width: 105 },
  { key: "ad_cvr", label: "广告CVR", width: 115, percent: true },
]
const widths = ref(Object.fromEntries(columns.map((column) => [column.key, column.width])))
const visible = ref(Object.fromEntries(columns.map((column) => [column.key, true])))
const sort = ref({ key: "clicks", direction: "desc" })
const configOpen = ref(false)
const currencySymbols = { USD: "$", JPY: "¥", EUR: "€", GBP: "£", CAD: "CA$", AUD: "A$", SEK: "kr" }
const columnOrder = ref(columns.map((column) => column.key))
const visibleColumns = computed(() => columnOrder.value.map((key) => columns.find((column) => column.key === key)).filter((column) => column && visible.value[column.key]))
const tableHeight = ref(null)
const groups = computed(() => rows.value.map((row) => ({ ...row, campaigns: [...(row.campaigns || [])].sort((a, b) => compare(a[sort.value.key], b[sort.value.key], sort.value.direction)) })))

function compare(left, right, direction) {
  const a = Number(left)
  const b = Number(right)
  if (Number.isNaN(a) && Number.isNaN(b)) return 0
  if (Number.isNaN(a)) return 1
  if (Number.isNaN(b)) return -1
  return direction === "asc" ? a - b : b - a
}
function display(value, column, currency) {
  if (value == null) return "—"
  if (column.percent) return `${(Number(value) * 100).toFixed(2)}%`
  if (column.money) return `${currencySymbols[currency] || currency || ""} ${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })
}
function groupKey(row) { return `${row.site_code}:${row.strategy}` }
function toggle(row) {
  const key = groupKey(row)
  const next = new Set(expanded.value)
  next.has(key) ? next.delete(key) : next.add(key)
  expanded.value = next
}
function cycleSort(column) {
  if (sort.value.key !== column.key) sort.value = { key: column.key, direction: "desc" }
  else sort.value.direction = sort.value.direction === "desc" ? "asc" : "desc"
}
function toggleColumn(column) { visible.value[column.key] = !visible.value[column.key] }
const draggingColumn = ref("")
function startDrag(column) { draggingColumn.value = column.key }
function dropColumn(column) {
  if (!draggingColumn.value || draggingColumn.value === column.key) return
  const sourceIndex = columnOrder.value.indexOf(draggingColumn.value)
  const targetIndex = columnOrder.value.indexOf(column.key)
  if (sourceIndex < 0 || targetIndex < 0) return
  const [moved] = columnOrder.value.splice(sourceIndex, 1)
  columnOrder.value.splice(targetIndex, 0, moved)
  draggingColumn.value = ""
}
let resizeCleanup = null
function startResize(event, column) {
  event.preventDefault(); event.stopPropagation(); resizeCleanup?.()
  const startX = event.clientX; const startWidth = widths.value[column.key]
  const move = (moveEvent) => { widths.value[column.key] = Math.max(76, Math.min(300, Math.round(startWidth + moveEvent.clientX - startX))) }
  const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); resizeCleanup = null }
  resizeCleanup = stop; window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop, { once: true })
}
function startTableResize(event) {
  event.preventDefault(); event.stopPropagation(); resizeCleanup?.()
  const wrap = event.currentTarget?.previousElementSibling
  const startY = event.clientY
  const startHeight = tableHeight.value || Math.max(280, wrap?.getBoundingClientRect().height || 520)
  const move = (moveEvent) => { tableHeight.value = Math.max(220, Math.min(window.innerHeight - 180, Math.round(startHeight + moveEvent.clientY - startY))) }
  const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); document.body.classList.remove("resizing-amazon-table"); resizeCleanup = null }
  resizeCleanup = stop; document.body.classList.add("resizing-amazon-table")
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop, { once: true })
}
function noteKey(row) { return groupKey(row) }
function noteValue(row) { return noteDrafts.value[noteKey(row)] ?? row.note ?? "" }
async function saveNote(row) {
  const key = noteKey(row)
  saving.value = `note:${key}`
  try {
    const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/strategy-board/note`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ site_code: row.site_code, strategy: row.strategy, note: noteValue(row) }) })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
    row.note = noteValue(row)
  } catch (e) { error.value = e.message || "备注保存失败" }
  finally { saving.value = "" }
}
async function saveCampaign(row, campaign, strategy) {
  saving.value = `campaign:${campaign.campaign_id}`
  try {
    const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/strategy-board/campaign-strategy`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ site_code: row.site_code, campaign_id: campaign.campaign_id, campaign_name: campaign.campaign_name, strategy }) })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
    await load()
  } catch (e) { error.value = e.message || "策略保存失败" }
  finally { saving.value = "" }
}
async function load() {
  if (!props.apiBase || !props.startDate || !props.endDate || !props.sites.length) return
  loading.value = true; error.value = ""
  try {
    const query = new URLSearchParams({ start_date: props.startDate, end_date: props.endDate })
    props.sites.forEach((site) => query.append("site", site))
    const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/strategy-board?${query}`)
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
    rows.value = data.strategies || []
    const nextDrafts = {}
    rows.value.forEach((row) => { nextDrafts[noteKey(row)] = row.note || "" })
    noteDrafts.value = nextDrafts
  } catch (e) { error.value = e.message || "广告策略看板加载失败" }
  finally { loading.value = false }
}
watch(() => [props.apiBase, props.startDate, props.endDate, props.sites.join(",")], load, { immediate: true })
onBeforeUnmount(() => resizeCleanup?.())
</script>

<template>
  <section class="strategy-board-panel">
    <div class="strategy-board-head">
      <div><span class="section-label">广告后台数据</span><h2>广告策略看板</h2><p>按策略汇总广告活动；仅展示统计期间点击量大于 0 的活动。</p></div>
      <div class="strategy-board-actions"><button class="strategy-config-button" type="button" @click="configOpen = !configOpen">列配置</button><button class="strategy-refresh-button" type="button" :disabled="loading" @click="load">刷新</button><div v-if="configOpen" class="strategy-config-panel"><button v-for="column in columns" :key="column.key" type="button" @click="toggleColumn(column)"><span>{{ column.label }}</span><span>{{ visible[column.key] ? "◉" : "○" }}</span></button></div></div>
    </div>
    <div v-if="error" class="strategy-board-error">{{ error }}</div>
    <div v-else-if="loading" class="strategy-board-loading">正在同步广告后台数据…</div>
    <div v-else class="strategy-table-wrap" :style="tableHeight ? { height: `${tableHeight}px`, maxHeight: `${tableHeight}px` } : undefined">
      <table class="strategy-table"><thead><tr><th class="strategy-name-column">策略</th><th v-for="column in visibleColumns" :key="column.key" draggable="true" @dragstart="startDrag(column)" @dragover.prevent @drop="dropColumn(column)" :style="{ width: `${widths[column.key]}px` }"><span class="strategy-column-drag-label">{{ column.label }}</span><button type="button" class="strategy-sort-button" :class="{ active: sort.key === column.key }" @click.stop="cycleSort(column)" :aria-label="`${column.label}排序`">{{ sort.key === column.key && sort.direction === "asc" ? "↑" : "↓" }}</button><i class="strategy-resize-handle" @pointerdown="startResize($event, column)"></i></th><th class="strategy-note-column">优化方向</th></tr></thead>
        <tbody><template v-for="row in groups" :key="groupKey(row)"><tr class="strategy-group-row"><td class="strategy-name-column"><button type="button" class="strategy-expand-button" @click="toggle(row)">{{ expanded.has(groupKey(row)) ? "−" : "+" }}</button><strong>{{ row.strategy }}</strong><small>{{ row.site }} · {{ row.campaigns.length }} 个活动</small></td><td v-for="column in visibleColumns" :key="column.key">{{ display(row.metrics?.[column.key], column, row.currency) }}</td><td class="strategy-note-cell"><textarea v-model="noteDrafts[noteKey(row)]" rows="1" placeholder="填写优化方向…"></textarea><button type="button" class="strategy-note-save" :disabled="saving === `note:${noteKey(row)}`" @click="saveNote(row)">保存</button></td></tr><template v-if="expanded.has(groupKey(row))"><tr v-for="campaign in row.campaigns" :key="`${groupKey(row)}:${campaign.campaign_id}`" class="strategy-campaign-row"><td class="strategy-name-column"><el-select :model-value="row.strategy" size="small" @change="saveCampaign(row, campaign, $event)"><el-option v-for="option in ['品类词', '品牌防御', '竞品词', '自动', 'SB/SBV', 'SD', 'B2B', '/']" :key="option" :label="option" :value="option" /></el-select><span class="campaign-name" :title="campaign.campaign_id">{{ campaign.campaign_name }}</span></td><td v-for="column in visibleColumns" :key="column.key">{{ display(campaign[column.key], column, campaign.currency) }}</td><td></td></tr></template></template></tbody>
      </table>
    </div><i class="table-height-resize-handle" role="separator" aria-orientation="horizontal" title="拖动调整表格高度" @pointerdown="startTableResize"></i>
  </section>
</template>
