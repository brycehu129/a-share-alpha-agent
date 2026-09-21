<script setup>
import { computed } from 'vue'
import { direction, fmtFlow, fmtNum } from '../../format'
import StatTile from '../StatTile.vue'

// 此刻的资金流与盘口事实。全是事实，不是买卖建议：净流入红、净流出绿（A 股惯例，和涨跌一致）。
const props = defineProps({
  flow: { type: Object, default: null },
  book: { type: Object, default: null },
})
const tone = (v) => direction(v, 0)
const outer = computed(() => (props.book && props.book.outer_pct !== undefined ? fmtNum(props.book.outer_pct, 1) + '%' : '—'))
const ratio = computed(() => {
  const r = props.book && props.book.bid_ask_ratio
  return r === undefined || r === null ? '—' : (r > 0 ? '+' : '') + Number(r).toFixed(1) + '%'
})
const peak = computed(() => (props.flow && props.flow.peak_main !== undefined ? `峰值 ${fmtFlow(props.flow.peak_main)}（${props.flow.peak_time.slice(0, 2)}:${props.flow.peak_time.slice(2)}）` : ''))
const flip = computed(() => {
  const f = props.flow && props.flow.flip
  return f ? `${f.at.slice(0, 2)}:${f.at.slice(2)} 起由${f.from}转${f.to}` : ''
})
</script>

<template>
  <div class="grid">
    <template v-if="flow">
      <StatTile label="主力净流入（累计）" :value="fmtFlow(flow.main)" :tone="tone(flow.main)" />
      <StatTile label="近30分钟主力" :value="fmtFlow(flow.main_30m)" :tone="tone(flow.main_30m)" />
      <StatTile label="超大单" :value="fmtFlow(flow.xlarge)" :tone="tone(flow.xlarge)" />
      <StatTile label="大单" :value="fmtFlow(flow.large)" :tone="tone(flow.large)" />
      <StatTile label="中单" :value="fmtFlow(flow.mid)" :tone="tone(flow.mid)" />
      <StatTile label="小单" :value="fmtFlow(flow.small)" :tone="tone(flow.small)" />
    </template>
    <StatTile label="外盘占比" :value="outer" />
    <StatTile label="委比" :value="ratio" :tone="book && book.bid_ask_ratio !== undefined ? tone(book.bid_ask_ratio) : ''" />
  </div>
  <p v-if="flow && (peak || flip)" class="muted extra">{{ peak }}<template v-if="peak && flip"> · </template>{{ flip }}</p>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1px;
  background: var(--el-border-color-light);
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  overflow: hidden;
}
.extra { margin: 6px 0 0; }
</style>
