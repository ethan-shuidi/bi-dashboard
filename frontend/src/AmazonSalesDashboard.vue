<script setup>
import { defineAsyncComponent } from "vue"

const SalesTargetModule = defineAsyncComponent(() => import("./SalesTargetModule.vue"))
const AmazonPlanBoard = defineAsyncComponent(() => import("./AmazonPlanBoard.vue"))

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

    <section class="sales-target-grid" aria-label="周度与月度销售目标看板">
      <SalesTargetModule :api-base="apiBase" variant="month" />
      <SalesTargetModule :api-base="apiBase" variant="week" />
    </section>
    <AmazonPlanBoard :api-base="apiBase" plan-type="operation" />
  </section>
</template>
