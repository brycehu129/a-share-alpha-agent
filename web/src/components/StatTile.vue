<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: String, default: '' },
  tone: { type: String, default: '' }, // rise / fall / flat：只有真正有涨跌含义的指标才着色
})
</script>

<template>
  <div class="stat-tile">
    <span class="stat-label">{{ label }}</span>
    <span class="stat-value num" :class="tone"><slot>{{ value }}</slot></span>
  </div>
</template>

<style scoped>
.stat-tile { background: var(--el-bg-color); padding: 10px 14px; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.stat-label { font-size: 12px; color: var(--as-muted); font-weight: 600; }
.stat-value { font-size: 18px; font-weight: 700; overflow-wrap: anywhere; }
</style>
