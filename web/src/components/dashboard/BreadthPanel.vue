<script setup>
import { computed } from 'vue'
import { fmtNum } from '../../format'
import StatTile from '../StatTile.vue'

// 全市场涨跌家数（market.universe.breadth，导出里早就有）和涨跌停家数。
// 涨跌停优先用 limit_list_d 的精确计数（limit_counts）；没有就退回按涨跌幅近似的数（limit_approx），并明确标「近似」。
const props = defineProps({
  universe: { type: Object, default: null },
  limitCounts: { type: Object, default: null },
  limitApprox: { type: Object, default: null },
})

const breadth = computed(() => (props.universe && props.universe.breadth) || null)
const total = computed(() => (breadth.value ? breadth.value.up + breadth.value.down + breadth.value.flat : 0))
const upPct = computed(() => (total.value ? (breadth.value.up / total.value) * 100 : null))
const dates = computed(() => Object.keys((props.universe && props.universe.dates) || {}).join('、'))
// 开盘前的快照里绝大多数股票还没有成交价变动，「平盘」会虚高：提示一句，别让人误读成市场平淡。
const mostlyFlat = computed(() => total.value > 0 && breadth.value.flat / total.value > 0.4)

const limits = computed(() => {
  if (props.limitCounts) return { ...props.limitCounts, exact: true }
  if (props.limitApprox) return { up: props.limitApprox.up, down: props.limitApprox.down, exact: false }
  return null
})
const int = (v) => (v === null || v === undefined ? '—' : String(v))
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-title">
        <span>市场宽度与涨跌停</span>
        <span v-if="dates" class="sub">全市场报价日期 {{ dates }}</span>
      </div>
    </template>

    <div class="tiles">
      <StatTile label="上涨家数" :value="breadth ? int(breadth.up) : '—'" :tone="breadth ? 'rise' : ''" />
      <StatTile label="下跌家数" :value="breadth ? int(breadth.down) : '—'" :tone="breadth ? 'fall' : ''" />
      <StatTile label="平盘家数" :value="breadth ? int(breadth.flat) : '—'" />
      <StatTile label="上涨占比" :value="upPct === null ? '—' : fmtNum(upPct, 1) + '%'" />
      <StatTile :label="limits && !limits.exact ? '涨停（近似）' : '涨停'" :value="limits ? int(limits.up) : '—'" :tone="limits ? 'rise' : ''" />
      <StatTile :label="limits && !limits.exact ? '跌停（近似）' : '跌停'" :value="limits ? int(limits.down) : '—'" :tone="limits ? 'fall' : ''" />
    </div>

    <p v-if="!breadth" class="muted note">全市场涨跌家数暂无：各只股票的报价日期不一致（例如盘中跨日），或全市场行情还没有归档。</p>
    <p v-else-if="mostlyFlat" class="note muted">平盘家数占比很高，这多半是开盘前的快照（大部分股票还没有价格变动），不代表市场平淡。</p>
    <p v-if="limits && limits.exact" class="muted note">
      涨跌停家数取自 {{ limits.date }} 的涨跌停池（交易所口径）<template v-if="limits.broken !== undefined">，当日炸板 {{ limits.broken }} 只</template>。
    </p>
    <p v-else-if="limits" class="muted note">
      涨跌停家数是近似值：按涨跌幅判定（主板 ≥9.8%、创业板/科创板 ≥19.5%），ST 股 5% 的限制识别不了，不是交易所口径。
    </p>
    <p v-else class="muted note">涨跌停家数暂无：涨跌停池还没有同步到，也没有可用的全市场行情归档。</p>
  </el-card>
</template>

<style scoped>
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 1px;
  background: var(--el-border-color-light);
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  overflow: hidden;
}
.note { margin: 10px 0 0; }
</style>
