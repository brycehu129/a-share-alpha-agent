<script setup>
import { fmtDateTime } from '../../format'
import RiseFall from '../RiseFall.vue'

defineProps({ quotes: { type: Array, default: () => [] } })
</script>

<template>
  <div class="index-grid">
    <div v-for="q in quotes" :key="q.name" class="index-tile">
      <div class="index-name">{{ q.name }}</div>
      <div class="index-last num">{{ q.last }}</div>
      <RiseFall class="index-chg" :value="parseFloat(q.change_pct)" />
      <div class="index-time num">{{ fmtDateTime(q.quote_at).slice(11) }}</div>
    </div>
  </div>
</template>

<style scoped>
.index-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.index-tile { background: var(--el-bg-color); border: 1px solid var(--el-border-color-light); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 3px; }
.index-name { font-size: 13px; color: var(--el-text-color-regular); font-weight: 600; }
.index-last { font-size: 20px; font-weight: 600; }
.index-chg { font-size: 13px; font-weight: 600; }
.index-time { font-size: 12px; color: var(--as-muted); }
@media (max-width: 860px) { .index-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
