<script setup>
import { computed } from 'vue'
import { fmtWan } from '../../format'
import RiseFall from '../RiseFall.vue'

const props = defineProps({ board: { type: Object, default: null } })

const LIMIT_LABEL = { U: '涨停', D: '跌停', Z: '炸板' }
const LIMIT_TYPE = { U: 'danger', D: 'success', Z: 'warning' } // 涨停红、跌停绿、炸板橙

const empty = computed(() => !props.board || (!props.board.hm_date && !props.board.limit_date))
const hmRows = computed(() => (props.board && props.board.hm_rows) || [])
const limitRows = computed(() => (props.board && props.board.limit_rows) || [])
const summary = computed(() => (empty.value ? '暂无数据' : `游资龙虎榜 ${props.board.hm_date || '—'} · 涨跌停池 ${props.board.limit_date || '—'}（两者可能不是同一交易日，取各自最近一次有数据的交易日）`))
const limitText = (r) => (r.limit === 'U' && r.limit_times ? `${r.limit_times}连板` : LIMIT_LABEL[r.limit] || r.limit || '—')
</script>

<template>
  <el-card shadow="never">
    <template #header><div class="card-title"><span>游资龙虎榜 / 涨跌停池</span><span class="sub">{{ summary }}</span></div></template>
    <p v-if="empty" class="muted" style="margin-top: 0"><b>游资明细（hm_detail）/涨跌停（limit_list_d）接口目前只回补了近期少数交易日，尚未接入每日自动同步，定时任务还没写入过这部分数据。</b></p>
    <template v-else>
      <p class="muted" style="margin-top: 0"><b>这两组数据独立于上方候选股票，展示整个交易日的榜单。</b>接口覆盖有限，某天缺失不代表当天无游资活动或无涨跌停，只是还没同步到；游资龙虎榜按净买卖额绝对值取前30笔（同一股票当天可能有多笔独立交易，不做合并）。</p>
      <div class="board-grid">
        <div>
          <h3>游资龙虎榜（按净买卖额排序）<span class="sub" v-if="hmRows.length"> · 共{{ board.hm_total_rows }}笔，展示前{{ hmRows.length }}</span></h3>
          <el-table :data="hmRows" size="small" empty-text="当日暂无数据">
            <el-table-column label="股票" min-width="120"><template #default="{ row }"><span class="stock-name">{{ row.name || '—' }}</span><span class="stock-code num">{{ row.symbol }}</span></template></el-table-column>
            <el-table-column label="游资/席位" min-width="120"><template #default="{ row }"><span class="small">{{ row.hm_name || '—' }}</span></template></el-table-column>
            <el-table-column label="净买卖" width="120">
              <template #default="{ row }"><el-tag :type="row.net_amount > 0 ? 'warning' : 'success'" size="small" round>{{ row.net_amount > 0 ? '净买' : '净卖' }}{{ fmtWan(Math.abs(row.net_amount)) }}</el-tag></template>
            </el-table-column>
          </el-table>
        </div>
        <div>
          <h3>涨跌停池<span class="sub" v-if="limitRows.length"> · 共{{ board.limit_total_rows }}只，展示前{{ limitRows.length }}</span></h3>
          <el-table :data="limitRows" size="small" empty-text="当日暂无数据">
            <el-table-column label="股票" min-width="120"><template #default="{ row }"><span class="stock-name">{{ row.name || '—' }}</span><span class="stock-code num">{{ row.symbol }}</span></template></el-table-column>
            <el-table-column label="涨跌幅" width="100" align="right"><template #default="{ row }"><RiseFall :value="row.pct_chg" /></template></el-table-column>
            <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="LIMIT_TYPE[row.limit] || 'info'" size="small" round>{{ limitText(row) }}</el-tag></template></el-table-column>
            <el-table-column label="封单" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.fd_amount != null ? fmtWan(row.fd_amount) : '—' }}</span></template></el-table-column>
          </el-table>
        </div>
      </div>
    </template>
  </el-card>
</template>

<style scoped>
.board-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: start; }
h3 { font-size: 14px; margin: 0 0 8px; }
h3 .sub { font-weight: 400; font-size: 12px; color: var(--as-muted); }
.small { font-size: 12.5px; color: var(--el-text-color-regular); }
@media (max-width: 860px) { .board-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
