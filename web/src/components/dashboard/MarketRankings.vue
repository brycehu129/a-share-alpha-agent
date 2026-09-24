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
const stockSide = ref('inflow')
const sector = computed(() => props.data && props.data.sectors && props.data.sectors[sectorKind.value])
// 只有「最强」：接口一页最多 100 行，从中挑出的「最弱」其实是「第 91–100 强」，
// 是误导而不是信息，所以后端已经不再返回 weak（见 market_rankings.rank_sectors）。
const sectorRows = computed(() => (sector.value && sector.value.strong) || [])
const sectorScope = computed(() => {
  const s = sector.value
  if (!s) return ''
  return s.total && s.fetched && s.total > s.fetched
    ? `${s.scope || ''}（全市场共 ${s.total} 个）`
    : (s.scope || '')
})
const stockRows = computed(() => (props.data && props.data.stocks && props.data.stocks[stockSide.value]) || [])
const errors = computed(() => (props.data && props.data.errors) || {})
const stale = computed(() => (props.data && props.data.stale) || [])
const sectorError = computed(() => !sector.value ? (errors.value[sectorKind.value] || '') : '')
const stockError = computed(() => !stockRows.value.length ? (errors.value[stockSide.value] || '') : '')
const sectorWarning = computed(() => stale.value.includes(sectorKind.value) ? errors.value[sectorKind.value] : '')
const stockWarning = computed(() => stale.value.includes(stockSide.value) ? errors.value[stockSide.value] : '')
</script>

<template>
  <div v-loading="loading" class="ranking-grid">
    <el-card shadow="never">
      <template #header>
        <div class="card-title">
          <span>今日最强板块</span>
          <span class="sub">综合涨幅 40% · 上涨占比 30% · 主力净流入占比 30%</span>
        </div>
      </template>
      <div class="filters">
        <el-radio-group v-model="sectorKind" size="small">
          <el-radio-button value="industry">行业</el-radio-button>
          <el-radio-button value="concept">概念</el-radio-button>
        </el-radio-group>
        <span v-if="sectorScope" class="scope muted">{{ sectorScope }}</span>
      </div>
      <p v-if="sectorError" class="muted error">该榜单暂不可用：{{ sectorError }}</p>
      <p v-else-if="sectorWarning" class="muted warning">实时取数失败，显示最近一次成功数据：{{ sectorWarning }}</p>
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
      <p v-else-if="stockWarning" class="muted warning">实时取数失败，显示最近一次成功数据：{{ stockWarning }}</p>
      <el-table v-else :data="stockRows" size="small" stripe @row-click="(row) => openStock(row.symbol, row.name)">
        <el-table-column type="index" label="#" width="42" />
        <el-table-column label="股票 / 板块概念" min-width="190">
          <template #default="{ row }">
            <span class="stock-name">{{ row.name }}</span><span class="stock-code num">{{ row.code }}</span>
            <span class="stock-tags">
              <el-tag v-if="row.industry" size="small" type="info" effect="plain">{{ row.industry }}</el-tag>
              <el-tooltip v-if="row.concepts && row.concepts.length" :content="row.concepts.join(' · ')" placement="top">
                <span class="concept-tags">
                  <el-tag v-for="concept in row.concepts.slice(0, 2)" :key="concept" size="small" effect="plain">{{ concept }}</el-tag>
                  <span v-if="row.concepts.length > 2" class="more">+{{ row.concepts.length - 2 }}</span>
                </span>
              </el-tooltip>
            </span>
          </template>
        </el-table-column>
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
.filters { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 10px; }
.scope { font-size: 11px; }
.error { margin: 8px 0; }
.warning { margin: 8px 0; color: var(--el-color-warning); }
.source { grid-column: 1 / -1; margin: -6px 0 0; }
.warn { color: var(--el-color-warning); }
.stock-tags, .concept-tags { display: flex; flex-wrap: wrap; align-items: center; gap: 3px; margin-top: 3px; }
.stock-tags :deep(.el-tag) { max-width: 82px; overflow: hidden; text-overflow: ellipsis; }
.more { color: var(--as-muted); font-size: 11px; }
:deep(.el-table__row) { cursor: default; }
.ranking-grid > :nth-child(2) :deep(.el-table__row) { cursor: pointer; }
@media (max-width: 900px) { .ranking-grid { grid-template-columns: minmax(0, 1fr); } .source { grid-column: auto; } }
</style>
