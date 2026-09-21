<script setup>
import { computed } from 'vue'
import { fmtNum } from '../../format'

const props = defineProps({ decisions: { type: Array, default: () => [] } })

const STATE_LABEL = { waiting_buy: '待入场', observing: '观察中', holding: '持有中', exited: '已退出', skipped: '已跳过', pending_sell: '待卖出' }
const NEWS_LABEL = { clear: '无风险公告', flag: '有需留意的公告', unverified: '未核验' }

// 待入场的排最前，其余保持原顺序（sort 是稳定的）
const rows = computed(() => props.decisions.slice().sort((a, b) => (a.state === 'waiting_buy' ? 0 : 1) - (b.state === 'waiting_buy' ? 0 : 1)))

function entryRange(d) {
  if (d.entry_low && d.entry_high) return `${fmtNum(d.entry_low, 3)}–${fmtNum(d.entry_high, 3)}`
  if (d.entry_high) return `≤ ${fmtNum(d.entry_high, 3)}`
  return '—'
}
function levels(d) {
  const lv = d.plan_levels
  if (!lv) return null
  const sz = lv.sizing || {}
  return {
    text: `止损 −${(lv.stop_pct * 100).toFixed(1)}%（≈${lv.stop_price_at_reference}） / 目标 +${(lv.target_pct * 100).toFixed(1)}%（≈${lv.target_price_at_reference}） / ${lv.hold_sessions} 个交易日 / 仓位约 ${Number(sz.position_pct).toFixed(0)}% 净值，最大亏约 ${Number(sz.max_loss_pct).toFixed(2)}%${sz.binding === 'weight_cap' ? '（单只上限）' : '（风险预算）'}`,
    archiveOnly: !!d.archive_only,
  }
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-title"><span>预测与待执行计划</span><span class="sub">冻结计划当前状态 · 共 <span class="num">{{ decisions.length }}</span> 条</span></div>
    </template>
    <el-table :data="rows" empty-text="当前没有冻结的计划。">
      <el-table-column label="股票" min-width="120">
        <template #default="{ row }"><span class="stock-name">{{ row.name }}</span><span class="stock-code num">{{ row.symbol }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }"><el-tag :type="row.state === 'waiting_buy' ? 'primary' : 'info'" round size="small">{{ STATE_LABEL[row.state] || row.state }}</el-tag></template>
      </el-table-column>
      <el-table-column label="最早允许日期" width="120"><template #default="{ row }"><span class="num">{{ row.eligible_from || '—' }}</span></template></el-table-column>
      <el-table-column label="参考价" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.reference_price ?? '—' }}</span></template></el-table-column>
      <el-table-column label="入场区间" width="130" align="right"><template #default="{ row }"><span class="num">{{ entryRange(row) }}</span></template></el-table-column>
      <el-table-column label="止损 / 目标 / 最长持有 / 仓位" min-width="260">
        <template #default="{ row }">
          <template v-if="levels(row)">
            <span class="small">{{ levels(row).text }}</span>
            <div v-if="levels(row).archiveOnly" class="muted">仅研究留档，不成交</div>
          </template>
          <template v-else>—</template>
        </template>
      </el-table-column>
      <el-table-column label="夜间公告" min-width="160">
        <template #default="{ row }">
          <span v-if="!NEWS_LABEL[row.news]">—</span>
          <template v-else>
            <span class="small">{{ NEWS_LABEL[row.news] }}</span>
            <div v-for="(i, k) in row.news_items || []" :key="k" class="muted">{{ String(i.time).slice(11, 16) }} {{ i.title }}</div>
          </template>
        </template>
      </el-table-column>
      <el-table-column label="说明" min-width="180"><template #default="{ row }"><span class="small muted">{{ (row.reasons && row.reasons[0]) || '' }}</span></template></el-table-column>
    </el-table>
  </el-card>
</template>

<style scoped>
.small { font-size: 12.5px; }
</style>
