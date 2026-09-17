<script setup>
import AmazonPlanBoard from "./AmazonPlanBoard.vue"

defineProps({
  apiBase: { type: String, required: true },
})
</script>

<template>
  <div class="amazon-page">
    <div class="amazon-page-head">
      <div>
        <div class="breadcrumb"><span>数据中心</span><i>/</i><strong>广告分析</strong></div>
        <h1>Amazon-广告计划</h1>
        <p>按周、站点和系列维护共享广告计划。</p>
      </div>
      <span class="period-badge">共享云端编辑</span>
    </div>
    <AmazonPlanBoard :api-base="apiBase" plan-type="ad" />
  </div>
</template>
