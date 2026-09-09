<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"

const props = defineProps({ modelValue: { type: String, default: "" } })
const emit = defineEmits(["update:modelValue", "change"])
const open = ref(false)
const viewDate = ref(parseDate(props.modelValue) || new Date())
const hoverDate = ref(null)
const root = ref(null)
const weekdays = ["一", "二", "三", "四", "五", "六", "日"]

function parseDate(value) { if (!value) return null; const date = new Date(`${value}T00:00:00`); return Number.isNaN(date.getTime()) ? null : date }
function pad(value) { return String(value).padStart(2, "0") }
function formatDate(date) { return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` }
function monday(date) { const result = new Date(date); result.setHours(0, 0, 0, 0); result.setDate(result.getDate() - ((result.getDay() + 6) % 7)); return result }
function addDays(date, amount) { const result = new Date(date); result.setDate(result.getDate() + amount); return result }
function sameDay(left, right) { return left && right && formatDate(left) === formatDate(right) }
function weekStartFor(date) { return monday(date) }
function inWeek(date, start) { return date >= start && date <= addDays(start, 6) }
function isoWeek(date) { const start = monday(date); const thursday = addDays(start, 3); const firstThursday = new Date(thursday.getFullYear(), 0, 4); const firstMonday = monday(firstThursday); return Math.floor((start - firstMonday) / 604800000) + 1 }

const selectedDate = computed(() => parseDate(props.modelValue) || new Date())
const selectedWeekStart = computed(() => weekStartFor(selectedDate.value))
const hoverWeekStart = computed(() => hoverDate.value ? weekStartFor(hoverDate.value) : null)
const displayValue = computed(() => `${selectedWeekStart.value.getFullYear()}年第${pad(isoWeek(selectedWeekStart.value))}周`)
const cells = computed(() => {
  const firstOfMonth = new Date(viewDate.value.getFullYear(), viewDate.value.getMonth(), 1)
  const start = monday(firstOfMonth)
  return Array.from({ length: 42 }, (_, index) => {
    const date = addDays(start, index)
    return { date, key: formatDate(date), day: date.getDate(), outside: date.getMonth() !== viewDate.value.getMonth() }
  })
})

function toggle() { open.value = !open.value; if (open.value) { viewDate.value = new Date(selectedDate.value); hoverDate.value = null } }
function moveMonth(amount) { viewDate.value = new Date(viewDate.value.getFullYear(), viewDate.value.getMonth() + amount, 1) }
function pick(date) { const start = weekStartFor(date); const value = formatDate(start); emit("update:modelValue", value); emit("change", value); open.value = false; hoverDate.value = null }
function cellClass(cell) { const classes = ["week-picker-day"]; if (cell.outside) classes.push("is-outside"); if (inWeek(cell.date, selectedWeekStart.value)) classes.push("is-selected-week"); if (hoverWeekStart.value && inWeek(cell.date, hoverWeekStart.value)) classes.push("is-hover-week"); if (sameDay(cell.date, selectedDate.value)) classes.push("is-selected-day"); return classes }
function handleOutside(event) { if (root.value && !root.value.contains(event.target)) { open.value = false; hoverDate.value = null } }
watch(() => props.modelValue, (value) => { const date = parseDate(value); if (date && !open.value) viewDate.value = date })
onMounted(() => document.addEventListener("pointerdown", handleOutside))
onBeforeUnmount(() => document.removeEventListener("pointerdown", handleOutside))
</script>

<template>
  <div ref="root" class="week-picker" @pointerdown.stop>
    <button type="button" class="week-picker-input" @pointerdown.stop.prevent="toggle" @click.stop.prevent><span class="week-picker-calendar-icon" aria-hidden="true">▦</span><span class="week-picker-value">{{ displayValue }}</span></button>
    <div v-if="open" class="week-picker-popover">
      <div class="week-picker-header"><button type="button" @click="moveMonth(-1)">‹</button><strong>{{ viewDate.getFullYear() }} 年 {{ viewDate.getMonth() + 1 }} 月</strong><button type="button" @click="moveMonth(1)">›</button></div>
      <div class="week-picker-weekdays"><span v-for="day in weekdays" :key="day">{{ day }}</span></div>
      <div class="week-picker-grid"><button v-for="cell in cells" :key="cell.key" type="button" :class="cellClass(cell)" @mouseenter="hoverDate = cell.date" @focus="hoverDate = cell.date" @click="pick(cell.date)">{{ cell.day }}</button></div>
    </div>
  </div>
</template>

<style scoped>
.week-picker{position:relative;width:100%;font-size:13px}.week-picker-input{width:100%;height:42px;padding:0 11px;display:flex;align-items:center;gap:8px;border:1px solid #d5e2f1;border-radius:9px;color:#173f7c;background:#f8fbff;font:inherit;font-weight:650;text-align:left;cursor:pointer;box-shadow:none;transition:border-color .2s,background-color .2s}.week-picker-input:hover{border-color:#adc8e8;background:#f8fbff}.week-picker-calendar-icon{color:#77899d;font-size:14px;line-height:1}.week-picker-value{flex:1;display:flex;align-items:center;height:40px;line-height:normal;white-space:nowrap}.week-picker-popover{position:absolute;top:47px;left:0;z-index:2200;width:282px;padding:12px;border:1px solid #d8e3ef;border-radius:8px;background:#fff;box-shadow:0 12px 28px rgba(26,66,112,.18)}.week-picker-header{height:32px;display:grid;grid-template-columns:32px 1fr 32px;align-items:center;text-align:center}.week-picker-header button{height:28px;padding:0;border:0;color:#526983;background:transparent;font-size:23px;cursor:pointer}.week-picker-header button:hover{border-radius:5px;background:#eef5fd}.week-picker-header strong{color:#294b73;font-size:14px}.week-picker-weekdays,.week-picker-grid{display:grid;grid-template-columns:repeat(7,1fr);text-align:center}.week-picker-weekdays{margin:6px 0 3px;color:#6f8197;font-size:12px}.week-picker-grid{row-gap:2px}.week-picker-day{height:30px;padding:0;border:0;border-radius:0;color:#385575;background:transparent;font-size:12px;cursor:pointer}.week-picker-day.is-outside{color:#a9b5c2}.week-picker-day.is-hover-week{background:#eaf3ff}.week-picker-day.is-selected-week{background:#dcecff;color:#1e5bad;font-weight:700}.week-picker-day.is-hover-week.is-selected-week{background:#cde3ff}.week-picker-day.is-selected-day{position:relative;color:#fff;background:#3f93ee;border-radius:50%;font-weight:800}.week-picker-day:hover{background:#c8e2ff;color:#174f9d}.week-picker-day.is-selected-day:hover{background:#287fdc;color:#fff}
</style>
