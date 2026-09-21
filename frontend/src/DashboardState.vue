<script setup>
defineProps({
  state: { type: String, default: "loading" },
  title: { type: String, default: "" },
  message: { type: String, required: true },
})
</script>

<template>
  <section
    :class="['dashboard-state', state]"
    :role="state === 'error' ? 'alert' : 'status'"
    :aria-live="state === 'error' ? 'assertive' : 'polite'"
  >
    <span class="dashboard-state-icon" aria-hidden="true">{{ state === "error" ? "!" : state === "warning" ? "!" : state === "empty" ? "○" : "…" }}</span>
    <div>
      <strong v-if="title">{{ title }}</strong>
      <p>{{ message }}</p>
    </div>
  </section>
</template>

<style scoped>
.dashboard-state{display:flex;align-items:center;gap:12px;min-height:58px;padding:12px 16px;border-radius:12px;border:1px solid #dbe5f2;background:#f8fbff;color:#334e73;font-size:13px}
.dashboard-state-icon{display:grid;place-items:center;flex:0 0 26px;width:26px;height:26px;border-radius:50%;background:#e8f1ff;color:#3b82f6;font-weight:700}
.dashboard-state p{margin:0;line-height:1.55}
.dashboard-state strong{display:block;margin-bottom:2px;color:#173c6b}
.dashboard-state.error{border-color:#fecdd3;background:#fff1f2;color:#991b1b}
.dashboard-state.error .dashboard-state-icon{background:#fee2e2;color:#dc2626}
.dashboard-state.warning{border-color:#fed7aa;background:#fff7ed;color:#9a3412}
.dashboard-state.warning .dashboard-state-icon{background:#ffedd5;color:#ea580c}
.dashboard-state.empty{background:#f8fafc;color:#64748b}
.dashboard-state.empty .dashboard-state-icon{background:#e2e8f0;color:#64748b}
</style>
