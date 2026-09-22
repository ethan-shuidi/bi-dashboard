<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { apiWithDashboardAuth, chooseEditConflictAction, formatDashboardEditMetadata } from "./dashboardAuth"

const props = defineProps({ apiBase: { type: String, required: true } })

const all = "全部"
const siteFilter = ref(all)
const modelFilter = ref(all)
const seriesFilter = ref(all)
const productFilter = ref(all)
const loading = ref(false)
const saving = ref(false)
const error = ref("")
const notice = ref("")
const rows = ref([])
const drafts = ref({})
const editMetadata = ref({ updated_at: null, updated_by: null })
const baseUpdatedAt = ref(null)
const dataQuality = ref(null)
let requestSeq = 0

const siteOptions = computed(() => [...new Set(rows.value.map((row) => row.site))])
const modelOptions = computed(() => [...new Set(rows.value.map((row) => row.model))])
const seriesOptions = computed(() => {
  const scoped = rows.value.filter((row) => modelFilter.value === all || row.model === modelFilter.value)
  return [...new Set(scoped.map((row) => row.series_key || row.series))]
})
const productOptions = computed(() => {
  const scoped = rows.value.filter((row) => (
    (modelFilter.value === all || row.model === modelFilter.value) &&
    (seriesFilter.value === all || (row.series_key || row.series) === seriesFilter.value)
  ))
  return [...new Set(scoped.map((row) => row.product_key || row.product))]
})

const rowKey = (row) => `${row.site_code}:${row.product_key}`
const normalizedAsin = (value) => String(value ?? "").trim().toUpperCase()

const effectiveRows = computed(() => rows.value.map((row) => {
  const key = rowKey(row)
  return { ...row, asin: normalizedAsin(drafts.value[key] ?? row.asin) }
}))

const visibleRows = computed(() => effectiveRows.value.filter((row) => (
  (siteFilter.value === all || row.site === siteFilter.value) &&
  (modelFilter.value === all || row.model === modelFilter.value) &&
  (seriesFilter.value === all || (row.series_key || row.series) === seriesFilter.value) &&
  (productFilter.value === all || (row.product_key || row.product) === productFilter.value)
)))

const dirtyRows = computed(() => effectiveRows.value.filter((row) => (
  normalizedAsin(row.asin) !== normalizedAsin(rows.value.find((source) => rowKey(source) === rowKey(row))?.asin)
)))

const duplicateGroups = computed(() => {
  const groups = new Map()
  for (const row of effectiveRows.value) {
    const asin = normalizedAsin(row.asin)
    if (!asin) continue
    const key = `${row.site_code}/${asin}`
    groups.set(key, [...(groups.get(key) || []), row.product])
  }
  return [...groups.entries()]
    .filter(([, products]) => new Set(products).size > 1)
    .map(([key, products]) => ({ key, products: [...new Set(products)] }))
})

const invalidRows = computed(() => effectiveRows.value.filter((row) => {
  const asin = normalizedAsin(row.asin)
  return asin && !/^B[A-Z0-9]{9}$/.test(asin)
}))
const dirtyCount = computed(() => dirtyRows.value.length)
const dirty = computed(() => dirtyCount.value > 0)

async function api(path, options = {}) {
  return apiWithDashboardAuth(`${props.apiBase}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  })
}

function applyPayload(payload) {
  rows.value = (payload.rows || []).map((row) => ({ ...row, asin: normalizedAsin(row.asin) }))
  drafts.value = {}
  editMetadata.value = payload.edit || { updated_at: null, updated_by: null }
  baseUpdatedAt.value = editMetadata.value.updated_at || null
  dataQuality.value = payload.data_quality || null
}

async function loadDashboard() {
  const currentRequest = ++requestSeq
  loading.value = true
  error.value = ""
  try {
    const payload = await api("/api/amazon/data-source/mappings")
    if (currentRequest !== requestSeq) return
    applyPayload(payload)
    notice.value = ""
  } catch (exception) {
    if (currentRequest === requestSeq) error.value = exception.message || "数据源加载失败"
  } finally {
    if (currentRequest === requestSeq) loading.value = false
  }
}

function validateBeforeSave() {
  if (invalidRows.value.length) {
    error.value = `ASIN 格式无效：${invalidRows.value[0].asin || "空"}（应为 B + 9 位数字/大写字母）`
    return false
  }
  if (duplicateGroups.value.length) {
    const item = duplicateGroups.value[0]
    error.value = `同一站点内 ASIN 重复：${item.key} → ${item.products.join(" / ")}`
    return false
  }
  return true
}

async function saveMappings({ force = false } = {}) {
  if (!dirty.value) {
    notice.value = "当前没有未保存的 ASIN"
    return
  }
  if (!validateBeforeSave()) return
  const items = dirtyRows.value.map((row) => ({
    site_code: row.site_code,
    product_key: row.product_key,
    asin: normalizedAsin(row.asin),
  }))
  saving.value = true
  error.value = ""
  notice.value = ""
  try {
    const saved = await api("/api/amazon/data-source/mappings", {
      method: "POST",
      body: JSON.stringify({ items, base_updated_at: baseUpdatedAt.value, force }),
    })
    if (!Array.isArray(saved.rows) || saved.rows.length !== rows.value.length) {
      throw new Error("云端保存后回读的数据不完整，请重新加载后再保存")
    }
    applyPayload(saved)
    notice.value = `数据源已保存（${saved.saved ?? items.length} 行），广告与销售看板已使用新映射`
  } catch (exception) {
    const action = await chooseEditConflictAction(exception)
    if (action === "overwrite") {
      try {
        await saveMappings({ force: true })
      } catch {}
      return
    }
    if (action === "reload") {
      await loadDashboard()
      notice.value = "已加载云端数据源"
      return
    }
    error.value = exception.message || "数据源保存失败"
  } finally {
    saving.value = false
  }
}

function updateAsin(row, value) {
  drafts.value[rowKey(row)] = String(value ?? "").trim().toUpperCase()
  notice.value = ""
}

function warnUnsavedBeforeUnload(event) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ""
}

function normalizeDependentFilters() {
  if (modelFilter.value !== all && !modelOptions.value.includes(modelFilter.value)) modelFilter.value = all
  if (seriesFilter.value !== all && !seriesOptions.value.includes(seriesFilter.value)) seriesFilter.value = all
  if (productFilter.value !== all && !productOptions.value.includes(productFilter.value)) productFilter.value = all
}

watch(modelFilter, () => {
  seriesFilter.value = all
  productFilter.value = all
})
watch(seriesFilter, () => {
  productFilter.value = all
})
watch(() => props.apiBase, (value) => {
  if (value) loadDashboard()
})
watch([siteFilter, modelFilter, seriesFilter, productFilter], normalizeDependentFilters)

onMounted(() => {
  window.addEventListener("beforeunload", warnUnsavedBeforeUnload)
  if (props.apiBase) loadDashboard()
})
onBeforeUnmount(() => window.removeEventListener("beforeunload", warnUnsavedBeforeUnload))
</script>

<template>
  <section class="data-source-page" aria-label="数据源">
    <header class="data-source-head">
      <div>
        <span>Amazon 基础数据映射</span>
        <h1>数据源</h1>
        <p>维护各站点产品 ASIN，保存后直接影响 Amazon 广告数据与销售看板的抓取与归类。</p>
      </div>
      <div class="data-source-head-meta">
        <span>{{ formatDashboardEditMetadata(editMetadata) }}</span>
        <span :class="['data-source-quality', { warning: dataQuality && !dataQuality.consistent }]">
          {{ dataQuality?.message || "ASIN 校验将在保存前执行" }}
        </span>
      </div>
    </header>

    <section class="data-source-filters" aria-label="数据源筛选">
      <label>
        <span>站点</span>
        <select v-model="siteFilter">
          <option :value="all">全部站点</option>
          <option v-for="item in siteOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label>
        <span>型号</span>
        <select v-model="modelFilter">
          <option :value="all">全部型号</option>
          <option v-for="item in modelOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label>
        <span>系列</span>
        <select v-model="seriesFilter">
          <option :value="all">全部系列</option>
          <option v-for="item in seriesOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label>
        <span>产品</span>
        <select v-model="productFilter">
          <option :value="all">全部产品</option>
          <option v-for="item in productOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button
        class="data-source-save"
        type="button"
        :disabled="saving || loading || !rows.length"
        @click="saveMappings"
      >
        {{ saving ? "保存中…" : dirty ? `保存（${dirtyCount} 行*）` : "保存" }}
      </button>
    </section>

    <section class="data-source-panel" aria-label="数据源映射表">
      <div class="data-source-toolbar">
        <strong>ASIN 映射表</strong>
        <span>已筛选 {{ visibleRows.length }} / {{ rows.length }} 行；未保存 {{ dirtyCount }} 行；同一站点内 ASIN 不能重复，跨站点允许相同 ASIN。</span>
      </div>

      <div v-if="error" class="data-source-error" role="alert">{{ error }}</div>
      <div v-else-if="notice" class="data-source-notice" role="status">{{ notice }}</div>

      <div class="data-source-table-wrap">
        <table class="data-source-table">
          <thead>
            <tr>
              <th>站点</th>
              <th>型号</th>
              <th>系列</th>
              <th>产品</th>
              <th>ASIN</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading && !rows.length">
              <td colspan="5">正在加载数据源…</td>
            </tr>
            <tr v-else-if="!visibleRows.length">
              <td colspan="5">当前筛选没有匹配行</td>
            </tr>
            <tr v-for="row in visibleRows" :key="rowKey(row)">
              <td>{{ row.site }}</td>
              <td>{{ row.model }}</td>
              <td>{{ row.series }}</td>
              <td>{{ row.product }}</td>
              <td :title="row.row_updated_at ? `${row.row_updated_by || '未知编辑者'} · ${row.row_updated_at}` : undefined">
                <input
                  :value="row.asin"
                  :class="{ dirty: normalizedAsin(row.asin) !== normalizedAsin(rows.find((source) => rowKey(source) === rowKey(row))?.asin) }"
                  type="text"
                  spellcheck="false"
                  autocomplete="off"
                  maxlength="10"
                  placeholder="可留空"
                  @input="updateAsin(row, $event.target.value)"
                >
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>

<style scoped>
.data-source-page{min-width:0;padding:24px;color:#17324d}
.data-source-head{display:flex;align-items:flex-end;justify-content:space-between;gap:22px}
.data-source-head span{color:#63788f;font-size:12px;font-weight:750}
.data-source-head h1{margin:4px 0 0;color:#102a56;font-size:28px;font-weight:850;letter-spacing:-.03em}
.data-source-head p{margin:6px 0 0;color:#687b92;font-size:13px}
.data-source-head-meta{display:grid;gap:6px;justify-items:end;color:#63788f;font-size:12px;font-weight:700;text-align:right}
.data-source-quality{padding:5px 9px;border:1px solid #cbe2d7;border-radius:8px;color:#16845b;background:#f2fbf6}
.data-source-quality.warning{border-color:#f0c5cd;color:#b52e45;background:#fff4f6}
.data-source-filters{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr)) auto;gap:12px;align-items:end;margin-top:20px;padding:15px;border:1px solid #e2ebf6;border-radius:14px;background:#f8fbff}
.data-source-filters label{display:grid;gap:6px;min-width:0}
.data-source-filters span{color:#5f7188;font-size:12px;font-weight:750}
.data-source-filters select{width:100%;height:42px;padding:0 10px;border:1px solid #d5e2f1;border-radius:9px;background:#fff;color:#26466d;font:inherit;font-size:13px;font-weight:650}
.data-source-save{height:42px;min-width:118px;padding:0 16px;border:1px solid #1d4ed8;border-radius:9px;color:#fff;background:#2563eb;font-size:13px;font-weight:780;cursor:pointer}
.data-source-save:hover:not(:disabled){background:#1d4ed8}
.data-source-save:disabled{opacity:.55;cursor:not-allowed}
.data-source-panel{overflow:hidden;margin-top:16px;border:1px solid #e2ebf6;border-radius:16px;background:#fff;box-shadow:0 12px 28px rgba(28,63,111,.07)}
.data-source-toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:15px 18px;border-bottom:1px solid #eef3f9;background:#fbfdff}
.data-source-toolbar strong{color:#132f5b;font-size:16px}
.data-source-toolbar span{color:#73849a;font-size:12px;font-weight:650;text-align:right}
.data-source-error{margin:12px 18px 0;padding:10px 12px;border:1px solid #f1c3cc;border-radius:9px;background:#fff4f6;color:#b52e45;font-size:12px;font-weight:750}
.data-source-notice{margin:12px 18px 0;padding:10px 12px;border:1px solid #cbe2d7;border-radius:9px;background:#f2fbf6;color:#16845b;font-size:12px;font-weight:750}
.data-source-table-wrap{max-height:calc(100vh - 330px);overflow:auto;overscroll-behavior:contain}
.data-source-table{width:100%;min-width:960px;border-collapse:separate;border-spacing:0;table-layout:fixed}
.data-source-table th,.data-source-table td{height:44px;padding:0 14px;border-bottom:1px solid #edf2f7;text-align:center;vertical-align:middle;white-space:nowrap;color:#385575}
.data-source-table th{position:sticky;top:0;z-index:2;background:#f8fbff;color:#5e728b;font-size:12px;font-weight:780}
.data-source-table th:first-child,.data-source-table td:first-child{width:130px;text-align:left}
.data-source-table th:nth-child(2),.data-source-table td:nth-child(2){width:100px}
.data-source-table th:nth-child(3),.data-source-table td:nth-child(3){width:150px}
.data-source-table th:nth-child(4),.data-source-table td:nth-child(4){width:180px}
.data-source-table th:last-child,.data-source-table td:last-child{width:220px}
.data-source-table input{width:100%;height:34px;padding:0 10px;border:1px solid #d5e2f1;border-radius:8px;background:#fff;color:#26466d;font:inherit;font-size:13px;font-weight:700;text-align:center;text-transform:uppercase}
.data-source-table input:focus{border-color:#6ea8dd;outline:2px solid rgba(46,120,193,.15)}
.data-source-table input.dirty{border-color:#e4b342;background:#fffbeb}
.data-source-table tbody tr:last-child td{border-bottom:0}
.data-source-table tbody td[colspan="5"]{height:100px;color:#71839a}
@media (max-width:1100px){.data-source-head{align-items:start;flex-direction:column}.data-source-head-meta{justify-items:start;text-align:left}.data-source-filters{grid-template-columns:1fr 1fr}.data-source-save{grid-column:1/-1}}
@media (max-width:720px){.data-source-page{padding:16px}.data-source-filters{grid-template-columns:1fr}.data-source-toolbar{align-items:start;flex-direction:column}.data-source-toolbar span{text-align:left}.data-source-panel{margin-inline:-16px;border-radius:0}}
</style>
