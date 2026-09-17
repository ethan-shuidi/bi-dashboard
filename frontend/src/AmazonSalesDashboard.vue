<script setup>
import SalesTargetModule from "./SalesTargetModule.vue"

defineProps({
  apiBase: { type: String, required: true },
})
</script>

<template>
  <section class="sales-dashboard content-shell">
    <section class="page-header">
      <div>
        <div class="breadcrumb"><span>数据中心</span><i>/</i><strong>销售分析</strong></div>
        <h1>Amazon-销售看板</h1>
        <p>按型号合并主链接与小链接，跟踪月度与周度目标完成情况。</p>
      </div>
      <div class="page-header-actions">
        <div class="topbar-center"><span class="live-dot"></span><span>领星数据在线</span></div>
      </div>
    </section>

    <SalesTargetModule :api-base="apiBase" variant="month" />
    <SalesTargetModule :api-base="apiBase" variant="week" />
  </section>
</template>
