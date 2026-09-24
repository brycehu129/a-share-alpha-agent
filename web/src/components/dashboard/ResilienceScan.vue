<script setup>
import { computed, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { fmtFlow, fmtNum, fmtPrice, fmtTs } from '../../format'
import { openStock } from '../../composables/useStockDetail'
import RiseFall from '../RiseFall.vue'

const props = defineProps({
  scan: { type: Object, default: null },
  timeline: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
})
const emit = defineEmits(['rescan', 'thresholds'])

// 阈值是**显示过滤器**，不是硬门槛：留档永远是全量，改这里只影响看到什么，
// 也会让时间线按新阈值重算。当前默认值未经任何验证，先收集分布再定。
const excessMin = ref(2.0)
const flowMin = ref(3.0)
const requireSector = ref(true)
const requireHoldsUp = ref(false)
const view = ref('hits')

function apply() {
  emit('thresholds', {
    excess_min_pp: excessMin.value,
    main_net_pct_min: flowMin.value,
    require_sector: requireSector.value ? '1' : '0',
    require_holds_up: requireHoldsUp.value ? '1' : '0',
  })
}

const market = computed(() => (props.scan && props.scan.market) || null)
const universe = computed(() => (props.scan && props.scan.universe) || null)
const rows = computed(() => {
  const s = props.scan
  if (!s) return []
  return view.value === 'hits' ? s.hits || [] : s.rows || []
})
const errors = computed(() => (props.scan && props.scan.errors) || {})
const errorList = computed(() => Object.entries(errors.value).map(([k, v]) => `${k}：${v}`))
const isStale = computed(() => ((props.scan && props.scan.stale) || []).length > 0)
// 板块那次请求挂了 → 所有行 sector_matched 都是 false → 命中必然 0。
// 那是「判断不了」，不是「没有符合条件的票」，必须分开说清楚。
const sectorBlind = computed(() =>
  !!(props.scan && universe.value && universe.value.sector_data_available === false && requireSector.value))
const fromRecord = computed(() => !!(props.scan && props.scan.from_record))
const marketTag = computed(() => {
  const m = market.value
  if (!m || m.market_down === null || m.market_down === undefined) return null
  return m.market_down
    ? { type: 'danger', text: `${m.primary_label} ${fmtNum(m.changes[m.primary], 2)}% · 大盘在跌` }
    : { type: 'info', text: `${m.primary_label} +${fmtNum(m.changes[m.primary], 2)}% · 大盘没跌` }
})
</script>

<template>
  <el-card v-loading="loading" shadow="never" class="resilience">
    <template #header>
      <div class="card-title">
        <span>抗跌扫描</span>
        <span class="sub">大盘往下时横住或向上 · 主力在买 · 所属板块也在吸金</span>
        <el-button :icon="Refresh" size="small" round :loading="loading" @click="emit('rescan')">重新扫描</el-button>
      </div>
    </template>

    <div class="head">
      <el-tag v-if="marketTag" :type="marketTag.type" round>{{ marketTag.text }}</el-tag>
      <el-tag v-if="isStale" type="warning" effect="plain" round>本轮取数失败，显示上一次成功数据</el-tag>
      <el-tag v-if="fromRecord" type="info" effect="plain" round>来自最近一次留档，未现扫</el-tag>
      <span v-if="scan" class="muted">扫描于 <span class="num">{{ fmtTs(scan.generated_at) }}</span></span>
    </div>

    <el-alert
      v-if="error" :title="`扫描失败：${error}`" type="error" show-icon :closable="false" style="margin-bottom: 12px" />
    <el-alert
      v-else-if="sectorBlind"
      title="板块数据本轮没取到，「板块也在吸金」这个条件无法判断"
      type="warning" show-icon :closable="false" style="margin-bottom: 12px">
      下面的命中数是 0，意思是<b>判断不了</b>，不是<b>没有符合条件的票</b>。要看抗跌+资金流两个条件的结果，
      先取消勾选「板块也在吸金且在涨」。
    </el-alert>
    <el-alert
      v-else-if="errorList.length" :title="`部分数据未取到：${errorList.join('；')}`"
      type="warning" show-icon :closable="false" style="margin-bottom: 12px" />

    <div class="filters">
      <span class="fl"><span class="lbl">抗跌度 ≥</span>
        <el-input-number v-model="excessMin" :min="-10" :max="15" :step="0.5" size="small" controls-position="right" @change="apply" /><span class="lbl">pp</span>
      </span>
      <span class="fl"><span class="lbl">主力净流入占比 ≥</span>
        <el-input-number v-model="flowMin" :min="-20" :max="50" :step="1" size="small" controls-position="right" @change="apply" /><span class="lbl">%</span>
      </span>
      <el-checkbox v-model="requireSector" size="small" @change="apply">板块也在吸金且在涨</el-checkbox>
      <el-checkbox v-model="requireHoldsUp" size="small" @change="apply">个股本身不跌</el-checkbox>
      <el-radio-group v-model="view" size="small">
        <el-radio-button value="hits">命中 {{ (scan && scan.hits && scan.hits.length) || 0 }}</el-radio-button>
        <el-radio-button value="all">全部 {{ (scan && scan.rows && scan.rows.length) || 0 }}</el-radio-button>
      </el-radio-group>
    </div>
    <p class="muted caveat">阈值未经任何验证，只是显示过滤器 —— 留档永远保存全量原始值，改阈值不影响留档，历史时间线会按新阈值重算。这是观察工具，不是买入信号。</p>

    <el-table :data="rows" size="small" stripe @row-click="(row) => openStock(row.symbol, row.name)">
      <el-table-column type="index" label="#" width="42" />
      <el-table-column label="股票" min-width="140">
        <template #default="{ row }">
          <span class="stock-name">{{ row.name }}</span><span class="stock-code num">{{ row.code }}</span>
          <div class="tags">
            <el-tag v-if="row.industry" size="small" type="info" effect="plain">{{ row.industry }}</el-tag>
            <el-tag v-if="!row.sector_matched" size="small" type="warning" effect="plain">板块未匹配</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="现价" width="72" align="right"><template #default="{ row }"><span class="num">{{ fmtPrice(row.price) }}</span></template></el-table-column>
      <el-table-column label="涨跌幅" width="78" align="right"><template #default="{ row }"><RiseFall :value="row.change_pct" bare /></template></el-table-column>
      <el-table-column label="抗跌度" width="92" align="right">
        <template #default="{ row }">
          <el-tooltip v-if="row.benchmark_label" :content="`对比 ${row.benchmark_label}${row.benchmark_size_matched ? '（按流通市值分档）' : '（市值缺失，退回主基准）'}`" placement="top">
            <span><RiseFall :value="row.excess_pp" bare /></span>
          </el-tooltip>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="主力净额" width="94" align="right"><template #default="{ row }"><span :class="row.main_net >= 0 ? 'rise' : 'fall'">{{ fmtFlow(row.main_net) }}</span></template></el-table-column>
      <el-table-column label="净流入占比" width="90" align="right"><template #default="{ row }"><RiseFall :value="row.main_net_pct" bare /></template></el-table-column>
      <el-table-column label="板块涨幅" width="86" align="right">
        <template #default="{ row }">
          <RiseFall v-if="row.sector_change_pct !== null && row.sector_change_pct !== undefined" :value="row.sector_change_pct" bare />
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="分数" width="66" align="right"><template #default="{ row }"><b class="num">{{ fmtNum(row.score, 1) }}</b></template></el-table-column>
    </el-table>
    <p v-if="!rows.length && !loading" class="muted">{{ view === 'hits' ? '当前阈值下没有命中的股票。' : '暂无扫描数据。' }}</p>

    <template v-if="timeline.length">
      <h4 class="tl-title">今日首次命中时间线<span class="sub">没有推送，这里回答「几点出现过谁」</span></h4>
      <el-table :data="timeline" size="small" stripe max-height="260" @row-click="(row) => openStock(row.symbol, row.name)">
        <el-table-column label="时刻" width="78"><template #default="{ row }"><span class="num">{{ fmtTs(row.at) }}</span></template></el-table-column>
        <el-table-column label="股票" min-width="120"><template #default="{ row }"><span class="stock-name">{{ row.name }}</span><span class="stock-code num">{{ row.code }}</span></template></el-table-column>
        <el-table-column label="板块" min-width="90" prop="industry" show-overflow-tooltip />
        <el-table-column label="当时涨跌" width="88" align="right"><template #default="{ row }"><RiseFall :value="row.change_pct" bare /></template></el-table-column>
        <el-table-column label="当时抗跌" width="88" align="right"><template #default="{ row }"><RiseFall :value="row.excess_pp" bare /></template></el-table-column>
        <el-table-column label="主力净额" width="94" align="right"><template #default="{ row }"><span :class="row.main_net >= 0 ? 'rise' : 'fall'">{{ fmtFlow(row.main_net) }}</span></template></el-table-column>
      </el-table>
    </template>

    <p class="source muted">
      <template v-if="universe">
        取样：主力净流入额前 {{ universe.from_amount }} 只 ∪ 净流入占比前 {{ universe.from_ratio }} 只，去重后 {{ universe.stocks_scanned }} 只；
        板块 {{ universe.sector_scope }}<template v-if="universe.sectors_total">（全市场共 {{ universe.sectors_total }} 个）</template>。<br>
      </template>
      {{ (scan && scan.source_note) || '' }}
    </p>
  </el-card>
</template>

<style scoped>
.resilience { margin-top: 16px; }
.card-title { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.card-title .sub { flex: 1; }
.head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 6px; }
.fl { display: inline-flex; align-items: center; gap: 5px; }
.lbl { font-size: 12px; color: var(--as-muted); }
.fl :deep(.el-input-number) { width: 96px; }
.caveat { margin: 0 0 10px; font-size: 11px; }
.tags { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 3px; }
.tl-title { margin: 18px 0 8px; font-size: 13px; }
.tl-title .sub { margin-left: 8px; font-weight: normal; color: var(--as-muted); font-size: 11px; }
.source { margin: 10px 0 0; font-size: 11px; }
:deep(.el-table__row) { cursor: pointer; }
</style>
