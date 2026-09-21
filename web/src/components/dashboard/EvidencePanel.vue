<script setup>
import { computed } from 'vue'
import { fmtPct } from '../../format'

const props = defineProps({ evidence: { type: Object, default: null } })

const EXIT_LABEL = { stop: '止损', breakeven_stop: '保本止损', target: '止盈', time_stop_day1: '首日收盘不及入场价', hold_expiry: '持有到期', drawdown_pause: '回撤暂停' }
const TRACK = { breakout: '突破', pullback: '回调反弹' }
const TIER = { top3: '前3（可成交）', rank4_10: '4–10名（仅留档）' }
const NOT_TRIGGERED = {
  never_confirmed: '始终没上穿确认线', chase: '开盘高于追高上限且全天没回落', void_at_open: '开盘即在作废线下',
  void_before_confirm: '先跌破作废线', ma20_unavailable: 'MA20无法核验', zone_empty: '入场区间为空', crossed_both_confirm_and_void: '先后不明',
}

// 「悲观 ~ 乐观」区间：日线分不清同一根 K 线里先止损还是先止盈，所以退出结果给一个区间。
function band(b) {
  if (!b) return '样本不足'
  return `${fmtPct(b.mean_pess_pct, 2)} ~ ${fmtPct(b.mean_opt_pct, 2)}（胜率 ${b.win_pess_pct}%~${b.win_opt_pct}%）`
}

const sub = computed(() => {
  const ev = props.evidence
  if (!ev) return ''
  return `选股 ${ev.selection_version}${ev.execution_version ? ' · 执行 ' + ev.execution_version : ''} · 共 ${ev.total_records} 条记录`
})

const groups = computed(() =>
  Object.entries((props.evidence && props.evidence.groups) || {}).map(([key, g]) => {
    const [track, tier] = key.split('/')
    return {
      key, track: TRACK[track] || track, tier: TIER[tier] || tier, plans: g.plans,
      fixed: `${g.fixed.n} / ${g.fixed.win_rate_pct === null ? '样本不足' : g.fixed.win_rate_pct + '%'}`,
      trig: `${g.contract.filled} / ${g.contract.not_triggered} / ${g.contract.ambiguous_entry}`,
      contract: band(g.contract.result),
      cf: band(g.counterfactual.result),
      untriggered: g.counterfactual.untriggered_n ? `${g.counterfactual.untriggered_n} 条：${band(g.counterfactual.untriggered_result)}` : '—',
    }
  }),
)

const reasonText = computed(() => {
  const reasons = {}
  Object.values((props.evidence && props.evidence.groups) || {}).forEach((g) =>
    Object.entries(g.contract.not_triggered_reasons || {}).forEach(([r, n]) => { reasons[r] = (reasons[r] || 0) + n }),
  )
  return Object.entries(reasons).sort((a, b) => b[1] - a[1]).map(([r, n]) => `${NOT_TRIGGERED[r] || r} ${n} 条`).join('；')
})

function entryText(r) {
  if (r.entry_state === 'filled') return '已触发'
  if (r.entry_state === 'ambiguous') return '先后不明'
  return `未触发：${NOT_TRIGGERED[r.entry_reason] || r.entry_reason}${r.near_miss_pct != null ? `（差 ${Number(r.near_miss_pct).toFixed(2)}%）` : ''}`
}
const contractText = (r) => (r.entry_state === 'filled' ? `${fmtPct(r.net_pess, 2)} ~ ${fmtPct(r.net_opt, 2)}（${EXIT_LABEL[r.exit_pess] || r.exit_pess}）` : '—')
</script>

<template>
  <el-card shadow="never">
    <template #header><div class="card-title"><span>候选池证据三层（研究标签）</span><span class="sub">{{ sub }}</span></div></template>
    <el-empty v-if="!evidence" :image-size="60" description="尚无证据标签：计划冻结后要等入场日和持有期走完才出，第一批预计在 exec-0.2 计划冻结后的第 4 个交易日。" />
    <template v-else>
      <p class="muted" style="margin-top: 0">
        <b>固定标签</b>=选股对不对（次日开盘→第3个收盘）；<b>合约模拟</b>=用计划自己冻结的入场条件与退出规则跑一遍，条件设得对不对；<b>反事实</b>=无视条件、次日开盘直接买、套同一套退出，与合约对比看入场条件是帮忙还是添乱，未触发者的反事实看条件是不是太严。<b>研究标签，不是实际成交。</b>日线分不清同一根K线里先止损还是先止盈，所以退出结果给「悲观~乐观」区间。比率和均值只在样本≥{{ evidence.gate.min_n }} 且日期组≥{{ evidence.gate.min_cohorts }} 时显示，未达标只列计数（不代表零）。
        <template v-if="evidence.unusable || evidence.other_versions"> 另有 {{ evidence.unusable }} 条因除权无法使用、{{ evidence.other_versions }} 条属于其他版本（不并入）。</template>
      </p>

      <el-empty v-if="!groups.length" :image-size="50" description="当前版本还没有到期的标签。" />
      <template v-else>
        <el-table :data="groups" size="small">
          <el-table-column prop="track" label="track" width="100" />
          <el-table-column prop="tier" label="档位" width="140" />
          <el-table-column prop="plans" label="计划数" width="80" align="right" />
          <el-table-column prop="fixed" label="固定标签 n / 胜率" min-width="130" />
          <el-table-column prop="trig" label="触发/未触发/先后不明" min-width="140" />
          <el-table-column prop="contract" label="合约净收益" min-width="200" />
          <el-table-column prop="cf" label="反事实净收益" min-width="200" />
          <el-table-column prop="untriggered" label="未触发者的反事实" min-width="200" />
        </el-table>
        <p v-if="reasonText" class="muted">未触发原因：{{ reasonText }}。</p>
      </template>

      <template v-if="(evidence.recent || []).length">
        <h3>最近的标签</h3>
        <el-table :data="evidence.recent" size="small">
          <el-table-column label="股票" min-width="110"><template #default="{ row }"><span class="num">{{ row.symbol }}{{ row.rank ? ' · #' + row.rank : '' }}</span></template></el-table-column>
          <el-table-column label="track" width="100"><template #default="{ row }">{{ TRACK[row.track] || row.track }}</template></el-table-column>
          <el-table-column label="入场日" width="110"><template #default="{ row }"><span class="num">{{ row.entry_day }}</span></template></el-table-column>
          <el-table-column label="入场" min-width="200"><template #default="{ row }"><span class="small">{{ entryText(row) }}</span></template></el-table-column>
          <el-table-column label="合约净收益" min-width="220"><template #default="{ row }"><span class="num small">{{ contractText(row) }}</span></template></el-table-column>
          <el-table-column label="反事实净收益" min-width="160"><template #default="{ row }"><span class="num small">{{ fmtPct(row.cf_net_pess, 2) }} ~ {{ fmtPct(row.cf_net_opt, 2) }}</span></template></el-table-column>
        </el-table>
      </template>
      <ul v-if="(evidence.limitations || []).length" class="limits"><li v-for="(l, i) in evidence.limitations" :key="i">{{ l }}</li></ul>
    </template>
  </el-card>
</template>

<style scoped>
h3 { font-size: 14px; margin: 18px 0 8px; }
.small { font-size: 12.5px; }
.limits { color: var(--as-muted); font-size: 12.5px; line-height: 1.7; }
</style>
