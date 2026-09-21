<script setup>
import { computed } from 'vue'

// 采集健康：让你一眼看出"今天到底抓没抓到"。数据来自 /api/sentinel(/symbol) 的 collection。
const props = defineProps({
  collection: { type: Object, default: null },
  symbol: { type: Boolean, default: false }, // true = 个股抽屉（多显示该股观测数与资金流采集数）
})
const c = computed(() => props.collection)
const low = computed(() => !!c.value && c.value.expected > 0 && c.value.ticks < c.value.expected * 0.9)
const gapMinutes = computed(() => (c.value ? Math.round(c.value.gap_seconds / 60) : 0))
</script>

<template>
  <el-alert v-if="c" :type="low ? 'warning' : 'info'" :closable="false" show-icon>
    <template #title>
      盘中轮询 <b class="num">{{ c.ticks }}/{{ c.expected }}</b> 轮
      <template v-if="c.gap_count"> · 中断 {{ c.gap_count }} 次（合计约 {{ gapMinutes }} 分钟）</template>
      <template v-if="c.first_tick"> · {{ c.first_tick }} – {{ c.last_tick }}</template>
      <template v-if="symbol"> · 该股观测 <b class="num">{{ c.observations }}</b> 次 · 资金流采集 <b class="num">{{ c.flow_ok }}</b> 次<template v-if="c.flow_error">（失败 {{ c.flow_error }} 次）</template></template>
      <template v-else> · 资金流采集 {{ c.flow_runs }} 次</template>
    </template>
    <div v-if="low" class="detail">
      采集明显偏少：一个完整交易日约 243 轮（每分钟一轮）。可能是定时任务没有每分钟触发，或被日历状态、上一轮未结束挡掉。
    </div>
    <div v-if="c.skips && c.skips.length" class="detail">
      时段内被跳过：<span v-for="(s, i) in c.skips" :key="s.reason">{{ i ? '；' : '' }}{{ s.reason }} × {{ s.count }}</span>
    </div>
    <div v-if="symbol && c.flow_last_error" class="detail">最近一次资金流失败原因：{{ c.flow_last_error }}</div>
  </el-alert>
</template>

<style scoped>
.detail { margin-top: 4px; font-size: 12.5px; line-height: 1.6; }
</style>
