<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { fetchWithDashboardAuth } from "./dashboardAuth"
import zhCn from "element-plus/es/locale/lang/zh-cn"
import WeekPicker from "./WeekPicker.vue"

const props = defineProps({
  apiBase: { type: String, required: true },
  planType: {
    type: String,
    required: true,
    validator(value) {
      return ["ad", "operation"].includes(value)
    },
  },
})

const SITE_ORDER = ["美国", "日本", "德国", "英国", "法国", "加拿大", "澳洲", "西班牙", "意大利", "荷兰", "比利时", "墨西哥", "爱尔兰", "波兰", "瑞典"]
const DEFAULT_SERIES = ["TN10系列（主链接）汇总", "TN10系列（小链接）汇总", "TN20系列（主链接）汇总", "TN20系列（小链接）汇总"]
const isOperationPlan = computed(() => props.planType === "operation")
const planTitle = computed(() => isOperationPlan.value ? "运营计划" : "广告计划")
const planDescription = computed(() => `${planTitle.value}内容按周、站点和系列保存，所有用户共享同一份云端内容。`)
const planEndpoint = computed(() => `/api/amazon/${isOperationPlan.value ? "operation-plan" : "ad-plan"}`)

const stores = ref([])
const seriesOptions = ref([...DEFAULT_SERIES])
const weekStart = ref("")
const site = ref("美国")
const series = ref(DEFAULT_SERIES[0])
const draft = ref({ review: "", plan: "" })
const loading = ref(false)
const saving = ref(false)
const error = ref("")
const toast = ref("")
const reviewEditor = ref(null)
const planEditor = ref(null)
const editorHeight = ref(172)
let toastTimer = null
let resizeObserver = null

const orderedSites = computed(() => [...new Set([...SITE_ORDER, ...stores.value.map((item) => item.country).filter(Boolean)])].sort((a, b) => (SITE_ORDER.indexOf(a) < 0 ? 999 : SITE_ORDER.indexOf(a)) - (SITE_ORDER.indexOf(b) < 0 ? 999 : SITE_ORDER.indexOf(b)) || a.localeCompare(b, "zh-CN")))
const weekEnd = computed(() => {
  const date = new Date(`${monday(weekStart.value)}T00:00:00`)
  date.setDate(date.getDate() + 6)
  return formatLocalDate(date)
})
const weekRangeLabel = computed(() => `${formatDotDate(weekStart.value)}~${formatDotDate(weekEnd.value)}`)

function formatLocalDate(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`
}

function formatDotDate(value) {
  return String(value || "").replaceAll("-", ".")
}

function previousWeekStart() {
  const date = new Date()
  date.setHours(0, 0, 0, 0)
  date.setDate(date.getDate() - ((date.getDay() + 6) % 7) - 7)
  return formatLocalDate(date)
}

function monday(value) {
  const date = new Date(`${value}T00:00:00`)
  if (Number.isNaN(date.getTime())) return previousWeekStart()
  date.setDate(date.getDate() - ((date.getDay() + 6) % 7))
  return formatLocalDate(date)
}

function displaySeries(value) {
  return ({
    "TN10系列（主链接）汇总": "TN10（主）",
    "TN10系列（小链接）汇总": "TN10（小）",
    "TN20系列（主链接）汇总": "TN20（主）",
    "TN20系列（小链接）汇总": "TN20（小）",
  }[value] || value || "")
}

function showToast(message) {
  toast.value = message
  if (toastTimer) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => {
    toast.value = ""
    toastTimer = null
  }, 1600)
}

async function loadStores() {
  try {
    const response = await fetchWithDashboardAuth(`${props.apiBase}/api/amazon/stores`)
    const data = await response.json()
    stores.value = data.stores || []
  } catch {}
}

async function load() {
  if (!props.apiBase || !weekStart.value || !site.value || !series.value) return
  loading.value = true
  try {
    const query = new URLSearchParams({ week_start: weekStart.value, site: site.value, series: series.value })
    const response = await fetchWithDashboardAuth(`${props.apiBase}${planEndpoint.value}?${query}`)
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
    draft.value = { review: data.review || "", plan: data.plan || "" }
  } catch (exception) {
    error.value = exception.message || `${planTitle.value}加载失败`
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  error.value = ""
  try {
    const response = await fetchWithDashboardAuth(`${props.apiBase}${planEndpoint.value}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ week_start: weekStart.value, site: site.value, series: series.value, ...draft.value }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`)
    showToast(`${planTitle.value}已保存到云端`)
  } catch (exception) {
    error.value = exception.message || `${planTitle.value}保存失败`
  } finally {
    saving.value = false
  }
}

function onWeekChange() {
  weekStart.value = monday(weekStart.value)
  load()
}

watch(() => props.apiBase, () => {
  if (!props.apiBase) return
  loadStores()
  load()
})
watch(() => [weekStart.value, site.value, series.value], load)

onMounted(async () => {
  weekStart.value = previousWeekStart()
  await nextTick()
  if (typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver((entries) => {
      const changed = entries.find((entry) => Math.abs(entry.target.getBoundingClientRect().height - editorHeight.value) > 1)
      if (changed) editorHeight.value = Math.max(150, Math.round(changed.target.getBoundingClientRect().height))
    })
    ;[reviewEditor.value, planEditor.value].filter(Boolean).forEach((editor) => resizeObserver.observe(editor, { box: "border-box" }))
  }
  await loadStores()
  await load()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  if (toastTimer) window.clearTimeout(toastTimer)
})
</script>

<template>
  <el-config-provider :locale="zhCn">
    <section class="ad-plan-panel" :aria-label="planTitle">
      <div class="ad-plan-head">
        <div>
          <span class="section-label">共享编辑</span>
          <h2>{{ planTitle }}</h2>
          <p>{{ planDescription }}</p>
        </div>
        <button class="strategy-note-save" type="button" :disabled="saving || loading" @click="save">{{ saving ? "保存中" : "保存到云端" }}</button>
      </div>
      <div class="ad-plan-filters">
        <label class="week-filter">
          <span>周 <b class="week-filter-code">{{ weekRangeLabel }}</b></span>
          <div class="week-picker-control"><WeekPicker v-model="weekStart" @change="onWeekChange" /></div>
        </label>
        <label>
          <span>站点</span>
          <el-select v-model="site">
            <el-option v-for="item in orderedSites" :key="item" :label="item" :value="item" />
          </el-select>
          <small class="filter-meta-spacer" aria-hidden="true"></small>
        </label>
        <label>
          <span>系列</span>
          <el-select v-model="series">
            <el-option v-for="item in seriesOptions" :key="item" :label="displaySeries(item)" :value="item" />
          </el-select>
          <small class="filter-meta-spacer" aria-hidden="true"></small>
        </label>
      </div>
      <div v-if="error" class="strategy-board-error">{{ error }}</div>
      <div class="ad-plan-grid">
        <label>
          <span>上周复盘</span>
          <textarea ref="reviewEditor" v-model="draft.review" :style="{ height: `${editorHeight}px` }" :disabled="loading" placeholder="填写上周复盘…"></textarea>
        </label>
        <label>
          <span>本周计划</span>
          <textarea ref="planEditor" v-model="draft.plan" :style="{ height: `${editorHeight}px` }" :disabled="loading" placeholder="填写本周计划…"></textarea>
        </label>
      </div>
      <div v-if="toast" class="amazon-copy-toast" role="status" aria-live="polite">{{ toast }}</div>
    </section>
  </el-config-provider>
</template>
