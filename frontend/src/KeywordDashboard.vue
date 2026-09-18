<script setup>
import { computed, onMounted, ref, watch } from "vue"
import { fetchWithDashboardAuth } from "./dashboardAuth"
import WeekPicker from "./WeekPicker.vue"

const props = defineProps({ apiBase: { type: String, required: true } })

const siteOptions = ["美国", "日本", "德国", "英国", "法国", "加拿大", "澳洲", "西班牙", "意大利", "荷兰", "比利时", "墨西哥", "爱尔兰", "波兰", "瑞典"]
const quickRangeOptions = [
  { label: "近5周", value: 5 },
  { label: "近10周", value: 10 },
  { label: "近15周", value: 15 },
]
const site = ref("美国")
const quickRange = ref(5)
const loading = ref(false)
const saving = ref(false)
const error = ref("")
const notice = ref("")
const data = ref(null)
const terms = ref([])
const savedTerms = ref([])
const endWeek = ref("")
const startWeek = ref("")

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

function applyQuickRange(weekCount) {
  const end = keywordWeekStart(new Date())
  endWeek.value = isoDate(end)
  startWeek.value = isoDate(addDays(end, -(Number(weekCount) - 1) * 7))
}

function syncQuickRange() {
  const currentWeek = keywordWeekStart(new Date())
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
const rows = computed(() => data.value?.rows || [])
const dirty = computed(() => JSON.stringify(termPayload()) !== JSON.stringify(savedTerms.value))

function termPayload() {
  return terms.value.map((item, index) => ({
    category: String(item.category || "未分类").trim() || "未分类",
    keyword: String(item.keyword || "").trim(),
    sort_order: index,
    enabled: true,
  }))
}

async function loadTerms(nextSite = site.value) {
  const payload = await api(`/api/keyword-dashboard/terms?site=${encodeURIComponent(nextSite)}`)
  terms.value = (payload.terms || []).map((item) => ({
    category: item.category || "未分类",
    keyword: item.keyword || "",
  }))
  savedTerms.value = termPayload()
}

async function loadDashboard({ refresh = false } = {}) {
  if (!props.apiBase || !startWeek.value || !endWeek.value) return
  loading.value = true
  error.value = ""
  try {
    const query = new URLSearchParams({
      site: site.value,
      start_week: startWeek.value,
      end_week: endWeek.value,
    })
    if (refresh) query.set("refresh", "true")
    data.value = await api(`/api/keyword-dashboard?${query}`)
    if (data.value?.terms?.length && !terms.value.length) {
      terms.value = data.value.terms.map((item) => ({ category: item.category || "未分类", keyword: item.keyword || "" }))
      savedTerms.value = termPayload()
    }
  } catch (exception) {
    error.value = exception.message || "关键词数据加载失败"
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  loading.value = true
  error.value = ""
  try {
    await loadTerms()
    await loadDashboard()
  } catch (exception) {
    error.value = exception.message || "关键词配置加载失败"
  } finally {
    loading.value = false
  }
}

function changeSite(nextSite) {
  if (nextSite === site.value) return
  if (dirty.value && !window.confirm("当前站点关键词尚未保存，切换后将丢失未保存的修改。是否继续？")) return
  site.value = nextSite
  notice.value = ""
}

function addTerm() {
  terms.value.push({ category: terms.value[terms.value.length - 1]?.category || "未分类", keyword: "" })
  notice.value = ""
}

function removeTerm(index) {
  terms.value.splice(index, 1)
  notice.value = ""
}

function moveTerm(index, direction) {
  const target = index + direction
  if (target < 0 || target >= terms.value.length) return
  const next = [...terms.value]
  const [item] = next.splice(index, 1)
  next.splice(target, 0, item)
  terms.value = next
}

async function saveTerms() {
  const payload = termPayload()
  if (!payload.length) {
    error.value = "请至少添加一个关键词"
    return
  }
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
    terms.value = (saved.terms || []).map((item) => ({ category: item.category || "未分类", keyword: item.keyword || "" }))
    savedTerms.value = termPayload()
    await loadDashboard({ refresh: true })
    notice.value = error.value ? "关键词配置已保存，ABA 数据暂未拉取" : "关键词配置已保存"
  } catch (exception) {
    error.value = exception.message || "关键词配置保存失败"
  } finally {
    saving.value = false
  }
}

function formatNumber(value) {
  if (value === null || value === undefined) return "—"
  return new Intl.NumberFormat("zh-CN").format(Number(value))
}

function rankClass(change) {
  if (change === null || change === undefined) return "flat"
  if (change > 0) return "up"
  if (change < 0) return "down"
  return "flat"
}

function rankChangeText(change) {
  if (change === null || change === undefined) return "—"
  if (change > 0) return `排名上升 ${change}`
  if (change < 0) return `排名下降 ${Math.abs(change)}`
  return "排名持平"
}

function sparkline(row) {
  const values = row.weekly.map((item) => item.search_rank)
  const points = values
    .map((value, index) => ({ value, index }))
    .filter((item) => item.value !== null && item.value !== undefined)
  if (points.length < 2) return null
  const min = Math.min(...points.map((item) => item.value))
  const max = Math.max(...points.map((item) => item.value))
  const span = max - min || 1
  const coordinates = points.map((item) => ({
    ...item,
    x: item.index / Math.max(values.length - 1, 1) * 100,
    y: 5 + (item.value - min) / span * 26,
  }))
  return {
    path: coordinates.map((item, index) => `${index ? "L" : "M"}${item.x.toFixed(2)},${item.y.toFixed(2)}`).join(" "),
    points: coordinates,
    min,
    max,
  }
}

watch(startWeek, (value) => {
  if (value && endWeek.value && value > endWeek.value) endWeek.value = value
  syncQuickRange()
})
watch(endWeek, (value) => {
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

applyQuickRange(5)
onMounted(loadAll)
</script>

<template>
  <section class="keyword-dashboard" aria-label="关键词看板">
    <header class="keyword-head">
      <div>
        <span>西柚 ABA 数据</span>
        <h1>关键词看板</h1>
        <p>按站点维护关键词，查看 ABA 周度搜索排名与搜索量。</p>
      </div>
      <div class="keyword-head-actions">
        <span v-if="data" :class="['keyword-cache', { cached: data.cached }]">{{ data.cached ? "缓存数据" : "实时数据" }}</span>
        <button type="button" :disabled="loading" @click="loadDashboard({ refresh: true })">{{ loading ? "同步中…" : "刷新" }}</button>
        <button class="primary" type="button" :disabled="saving || loading" @click="saveTerms">{{ saving ? "保存中…" : dirty ? "保存*" : "保存" }}</button>
      </div>
    </header>

    <section class="keyword-filters" aria-label="关键词筛选">
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
    </section>

    <section class="keyword-manager" aria-label="关键词管理">
      <header>
        <div>
          <strong>{{ site }} 关键词配置</strong>
          <small>按整站保存，支持添加、删除和排序；每个站点最多 100 个。</small>
        </div>
        <div class="keyword-manager-actions">
          <button type="button" @click="addTerm">添加关键词</button>
        </div>
      </header>
      <div class="keyword-manager-list">
        <div v-for="(item, index) in terms" :key="`${index}-${item.keyword}`" class="keyword-manager-row">
          <input v-model="item.category" type="text" maxlength="80" placeholder="分类" aria-label="关键词分类">
          <input v-model="item.keyword" type="text" maxlength="255" placeholder="关键词" aria-label="关键词">
          <div class="keyword-order">
            <button type="button" :disabled="index === 0" @click="moveTerm(index, -1)">↑</button>
            <button type="button" :disabled="index === terms.length - 1" @click="moveTerm(index, 1)">↓</button>
            <button type="button" @click="removeTerm(index)">删除</button>
          </div>
        </div>
        <div v-if="!terms.length" class="keyword-empty">请先添加需要跟踪的关键词，保存后自动拉取西柚 ABA 数据。</div>
      </div>
    </section>

    <section v-if="error" class="keyword-message error" role="alert">{{ error }}</section>
    <section v-if="notice" class="keyword-message success" role="status">{{ notice }}</section>
    <section v-if="loading && !rows.length" class="keyword-message muted">正在获取西柚 ABA 数据…</section>

    <section class="keyword-table-panel" aria-label="关键词周度数据">
      <div class="keyword-table-wrap">
        <table>
          <thead>
            <tr>
              <th class="category">分类</th>
              <th class="keyword">关键词</th>
              <th class="trend">趋势</th>
              <th v-for="week in weeks" :key="week.start" class="week">
                <strong>{{ week.label || `W${String(isoWeek(week.start)).padStart(2, "0")}` }}</strong>
                <small>{{ week.start.slice(5) }}~{{ week.end.slice(5) }}</small>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id || row.keyword">
              <td>{{ row.category }}</td>
              <th scope="row">
                <span>{{ row.keyword }}</span>
                <small :class="rankClass(row.rank_change)">{{ rankChangeText(row.rank_change) }}</small>
              </th>
              <td class="trend">
                <svg v-if="sparkline(row)" viewBox="0 0 100 36" preserveAspectRatio="none" role="img" :aria-label="`${row.keyword} ABA 搜索排名趋势`">
                  <path :d="sparkline(row).path" />
                  <circle v-for="point in sparkline(row).points" :key="point.index" :cx="point.x" :cy="point.y" r="2">
                    <title>{{ weeks[point.index]?.label }} · 排名 {{ point.value }}</title>
                  </circle>
                </svg>
                <span v-else class="no-trend">数据不足</span>
              </td>
              <td v-for="item in row.weekly" :key="item.week_start" :class="{ missing: item.search_volume === null || item.search_volume === undefined }">
                <strong>{{ formatNumber(item.search_volume) }}</strong>
                <small>{{ item.search_rank === null || item.search_rank === undefined ? "排名 —" : `排名 ${formatNumber(item.search_rank)}` }}</small>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="rows.length && !weeks.length" class="keyword-empty">当前周范围没有数据</div>
        <div v-else-if="!rows.length && !loading" class="keyword-empty">暂无关键词数据，请先在上方添加并保存关键词。</div>
      </div>
    </section>
  </section>
</template>

<style scoped>
.keyword-dashboard{min-width:0;padding:22px;color:#17324d}
.keyword-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.keyword-head span{color:#6d7f95;font-size:12px;font-weight:750}.keyword-head h1{margin:3px 0 0;color:#12315d;font-size:24px}.keyword-head p{margin:6px 0 0;color:#6d7f95;font-size:13px}.keyword-head-actions{display:flex;align-items:center;gap:8px}.keyword-head-actions button,.keyword-manager-actions button{height:36px;padding:0 14px;border:1px solid #c9dbf0;border-radius:9px;background:#fff;color:#24589d;font:inherit;font-size:13px;font-weight:700;cursor:pointer}.keyword-head-actions .primary{border-color:#1e57c8;background:#1e57c8;color:#fff}.keyword-head-actions button:disabled,.keyword-manager-actions button:disabled{opacity:.6;cursor:not-allowed}.keyword-cache{height:28px;display:inline-flex;align-items:center;padding:0 10px;border-radius:999px;background:#eef3f9;color:#65778c;font-size:12px;font-weight:750}.keyword-cache.cached{background:#e8f7ef;color:#19704b}
.keyword-filters{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin-top:18px;padding:14px;border:1px solid #e2ebf6;border-radius:14px;background:#f8fbff}.keyword-filters label{display:grid;gap:6px;min-width:0}.keyword-filters span{color:#5f7188;font-size:12px;font-weight:750}.keyword-filters select{width:100%;height:42px;padding:0 10px;border:1px solid #d5e2f1;border-radius:9px;background:#fff;color:#26466d;font:inherit}
.keyword-manager{margin-top:16px;padding:16px;border:1px solid #e2ebf6;border-radius:16px;background:#fff;box-shadow:0 12px 28px rgba(28,63,111,.07)}.keyword-manager header{display:flex;align-items:center;justify-content:space-between;gap:14px}.keyword-manager strong{display:block;color:#183c6b;font-size:15px}.keyword-manager small{display:block;margin-top:3px;color:#71819a;font-size:12px}.keyword-manager-list{display:grid;max-height:270px;gap:8px;margin-top:12px;overflow:auto;padding-right:2px}.keyword-manager-row{display:grid;grid-template-columns:minmax(110px,180px) minmax(220px,1fr) auto;gap:8px}.keyword-manager-row input,.keyword-manager-row button{height:36px;border:1px solid #d5e2f1;border-radius:8px;background:#fff;color:#26466d;font:inherit}.keyword-manager-row input{padding:0 10px}.keyword-order{display:flex;gap:5px}.keyword-order button{width:38px;padding:0}.keyword-order button:last-child{width:52px}.keyword-order button:disabled{opacity:.4;cursor:not-allowed}.keyword-empty{padding:24px;border-radius:10px;background:#f8fbff;color:#71819a;font-size:13px;text-align:center}
.keyword-message{margin-top:16px;padding:12px 14px;border-radius:10px;font-size:13px}.keyword-message.error{background:#fff2f4;color:#ad2745}.keyword-message.success{background:#edfaf3;color:#17724c}.keyword-message.muted{background:#f7fafd;color:#6d7f95}
.keyword-table-panel{position:relative;z-index:1;margin-top:16px;border:1px solid #e2ebf6;border-radius:16px;background:#fff;box-shadow:0 12px 28px rgba(28,63,111,.07)}.keyword-table-wrap{overflow:auto;overscroll-behavior-x:contain;border-radius:16px}table{width:100%;min-width:1180px;border-collapse:collapse}th,td{padding:11px 10px;border-bottom:1px solid #e9f0f8;text-align:left;vertical-align:middle}thead th{position:sticky;top:0;z-index:2;background:#f4f8fd;color:#455f7c;font-size:12px}thead .week{min-width:86px;text-align:right}thead strong{display:block}thead small{display:block;margin-top:2px;color:#7f90a5;font-weight:500}.category{width:130px;color:#607289}.keyword{width:210px}.keyword th span{display:block;color:#173d70;font-size:13px;font-weight:800}.keyword th small{display:inline-block;margin-top:4px;border-radius:999px;background:#eef2f7;padding:2px 7px;color:#68798f;font-size:11px;font-weight:700}.keyword th small.up{background:#fdecef;color:#b52e45}.keyword th small.down{background:#e8f7ef;color:#19704b}.trend{width:150px}.trend svg{display:block;width:100%;height:36px}.trend path{fill:none;stroke:#2f80ed;stroke-width:2}.trend circle{fill:#fff;stroke:#2f80ed;stroke-width:1.5}.no-trend{color:#93a2b4;font-size:12px}tbody td{color:#26466d;font-size:13px}tbody td.week{text-align:right}tbody td.week strong{display:block}tbody td.week small{display:block;margin-top:2px;color:#77899e;font-size:11px}tbody td.missing strong{color:#93a2b4}tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
@media (max-width:1100px){.keyword-filters{grid-template-columns:1fr 1fr}.keyword-head{align-items:flex-start}.keyword-head-actions{flex-wrap:wrap}}
@media (max-width:720px){.keyword-filters,.keyword-manager-row{grid-template-columns:1fr}.keyword-table-panel{margin-inline:-22px;border-radius:0}}
</style>
