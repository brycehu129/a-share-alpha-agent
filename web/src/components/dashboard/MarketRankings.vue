<script setup>
import { computed, ref } from 'vue'
import { fmtFlow, fmtNum, fmtPrice, fmtTs } from '../../format'
import { openStock } from '../../composables/useStockDetail'
import RiseFall from '../RiseFall.vue'

const props = defineProps({
  data: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  stale: { type: Boolean, default: false },
  error: { type: String, default: '' },
})
const sectorKind = ref('industry')
const sectorSide = ref('strong')
const stockSide = ref('inflow')
const sector = computed(() => props.data && props.data.sectors && props.data.sectors[sectorKind.value])
const sectorRows = computed(() => (sector.value && sector.value[sectorSide.value]) || [])
const stockRows = computed(() => (props.data && props.data.stocks && props.data.stocks[stockSide.value]) || [])
const errors = computed(() => (props.data && props.data.errors) || {})
const sectorError = computed(() => errors.value[sectorKind.value] || '')
const stockError = computed(() => errors.value[stockSide.value] || '')
</script>

<template>
  <div v-loading="loading" class="ranking-grid">
    <el-card shadow="never">
      <template #header>
        <div class="card-title">
          <span>今日板块强度</span>
          <span class="sub">综合涨幅 40% · 上涨占比 30% · 主力净流入占比 30%</span>
        </div>
      </template>
      <div class="filters">
        <el-radio-group v-model="sectorKind" size="small">
          <el-radio-button value="industry">行业</el-radio-button>
          <el-radio-button value="concept">概念</el-radio-button>
        </el-radio-group>
        <el-radio-group v-model="sectorSide" size="small">
          <el-radio-button value="strong">最强 10</el-radio-button>
          <el-radio-button value="weak">最弱 10</el-radio-button>
        </el-radio-group>
      </div>
      <p v-if="sectorError" class="muted error">该榜单暂不可用：{{ sectorError }}</p>
      <el-table v-else :data="sectorRows" size="small" stripe>
        <el-table-column type="index" label="#" width="42" />
        <el-table-column label="板块" min-width="105" prop="name" show-overflow-tooltip />
        <el-table-column label="强度" width="64" align="right"><template #default="{ row }"><b class="num">{{ fmtNum(row.strength, 1) }}</b></template></el-table-column>
        <el-table-column label="涨幅" width="75" align="right"><template #default="{ row }"><RiseFall :value="row.change_pct" bare /></template></el-table-column>
        <el-table-column label="上涨占比" width="82" align="right"><template #default="{ row }"><span class="num">{{ fmtNum(row.up_pct, 1) }}%</span></template></el-table-column>
        <el-table-column label="主力净额" width="92" align="right"><template #default="{ row }"><span :class="row.main_net >= 0 ? 'rise' : 'fall'">{{ fmtFlow(row.main_net) }}</span></template></el-table-column>
        <el-table-column label="净流入占比" width="88" align="right"><template #default="{ row }"><RiseFall :value="row.main_net_pct" bare /></template></el-table-column>
      </el-table>
      <p v-if="!sectorError && !sectorRows.length" class="muted">暂无板块排行。</p>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-title">
          <span>个股主力资金排行</span>
          <span class="sub">沪深 A 股</span>
        </div>
      </template>
      <div class="filters">
        <el-radio-group v-model="stockSide" size="small">
          <el-radio-button value="inflow">净流入前 10</el-radio-button>
          <el-radio-button value="outflow">净流出前 10</el-radio-button>
        </el-radio-group>
      </div>
      <p v-if="stockError" class="muted error">该榜单暂不可用：{{ stockError }}</p>
      <el-table v-else :data="stockRows" size="small" stripe @row-click="(row) => openStock(row.symbol, row.name)">
        <el-table-column type="index" label="#" width="42" />
        <el-table-column label="股票" min-width="105"><template #default="{ row }"><span class="stock-name">{{ row.name }}</span><span class="stock-code num">{{ row.code }}</span></template></el-table-column>
        <el-table-column label="现价" width="74" align="right"><template #default="{ row }"><span class="num">{{ fmtPrice(row.price) }}</span></template></el-table-column>
        <el-table-column label="涨跌幅" width="78" align="right"><template #default="{ row }"><RiseFall :value="row.change_pct" bare /></template></el-table-column>
        <el-table-column label="主力净额" width="98" align="right"><template #default="{ row }"><span :class="row.main_net >= 0 ? 'rise' : 'fall'">{{ fmtFlow(row.main_net) }}</span></template></el-table-column>
        <el-table-column label="净流入占比" width="88" align="right"><template #default="{ row }"><RiseFall :value="row.main_net_pct" bare /></template></el-table-column>
      </el-table>
      <p v-if="!stockError && !stockRows.length" class="muted">暂无个股资金排行。</p>
    </el-card>

    <p class="source muted">
      数据时间 <span class="num">{{ data ? fmtTs(data.fetched_at) : '—' }}</span>
      <template v-if="stale"> · <b class="warn">本次刷新失败，正在显示上一次成功数据</b></template>
      <template v-if="error"> · {{ error }}</template>
      <br>{{ (data && data.source_note) || '东方财富按成交额分档估算；主力＝超大单＋大单，非交易所披露。' }}仅作行情事实展示，不构成投资建议。
    </p>
  </div>
</template>

<style scoped>
.ranking-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.filters { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.error { margin: 8px 0; }
.source { grid-column: 1 / -1; margin: -6px 0 0; }
.warn { color: var(--el-color-warning); }
:deep(.el-table__row) { cursor: default; }
.ranking-grid > :nth-child(2) :deep(.el-table__row) { cursor: pointer; }
@media (max-width: 900px) { .ranking-grid { grid-template-columns: minmax(0, 1fr); } .source { grid-column: auto; } }
</style>
