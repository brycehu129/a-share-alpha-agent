<script setup>
import { computed } from 'vue'
import { fmtMoney, fmtPct, fmtDateTime, direction } from '../../format'
import StatGrid from '../StatGrid.vue'
import StatTile from '../StatTile.vue'
import RiseFall from '../RiseFall.vue'
import NavChart from './NavChart.vue'

const props = defineProps({ account: { type: Object, default: null } })

const EXIT_LABEL = { stop: '止损', breakeven_stop: '保本止损', target: '止盈', time_stop_day1: '首日收盘不及入场价', hold_expiry: '持有到期', drawdown_pause: '回撤暂停' }
const PLAN_LABEL = { watching: '观察中', filled: '已成交', expired: '已过期', voided: '已作废', skipped: '已跳过' }
const OP_TYPE = { buy: '买入', sell: '卖出', breakeven_armed: '保本止损启动', plan_voided: '计划作废', plan_expired: '计划过期', plan_skipped: '计划跳过' }

const acc = computed(() => props.account)
const tradeStats = computed(() => (acc.value && acc.value.trade_stats) || { closed: 0, wins: 0, win_rate_pct: null })
const dd = computed(() => Math.abs((acc.value && acc.value.drawdown_pct) || 0))
const navTone = computed(() => (acc.value.nav > 1 ? 'rise' : acc.value.nav < 1 ? 'fall' : ''))
const sells = computed(() => ((acc.value && acc.value.trades) || []).filter((t) => t.side === 'sell').slice(-6).reverse())
const ops = computed(() => (acc.value && acc.value.operations) || [])
const planCounts = computed(() => Object.entries((acc.value && acc.value.plan_counts) || {}).map(([k, v]) => `${PLAN_LABEL[k] || k} ${v}`).join(' · '))
const sub = computed(() => (acc.value ? `快照 ${fmtDateTime(acc.value.snapshot_at)}${acc.value.paused ? ' · 风控暂停中' : ''}` : ''))
const num = (v, d = 2) => Number(v).toFixed(d)
</script>

<template>
  <el-card shadow="never">
    <template #header><div class="card-title"><span>盘中条件执行虚拟账户</span><span class="sub">{{ sub }}</span></div></template>
    <p class="muted" style="margin-top: 0">新计划由盘中引擎按条件触发执行，独立的 10 万虚拟本金；与上面的虚拟账户（exec-0.1）互不相干，<b>两者的成交与胜率不得混算</b>。这是虚拟账户，不是你的真实持仓。</p>

    <el-empty v-if="!acc" :image-size="60" description="账本尚未并入：盘中引擎第一次运行、且日线流程跑过之后这里才会有内容。" />
    <template v-else>
      <StatGrid>
        <StatTile label="总资产" :value="fmtMoney(acc.equity)" />
        <StatTile label="现金" :value="fmtMoney(acc.cash)" />
        <StatTile label="净值" :value="num(acc.nav, 4)" :tone="navTone" />
        <StatTile label="回撤" :value="dd < 0.005 ? '0.00%' : `−${dd.toFixed(2)}%`" />
        <StatTile label="持仓" :value="`${(acc.positions || []).length} 只`" />
        <StatTile label="已平仓" :value="`${tradeStats.closed} 笔 / 盈利 ${tradeStats.wins}`" />
      </StatGrid>
      <p class="muted">{{ tradeStats.win_rate_pct !== null && tradeStats.win_rate_pct !== undefined ? `已平仓胜率 ${tradeStats.win_rate_pct}%。` : '胜率暂不显示：已平仓不足 30 笔，几笔的比例没有含义。' }}</p>

      <NavChart v-if="(acc.curve || []).length >= 2" :points="acc.curve" :baseline="1" :height="130" label="盘中条件执行账户净值" />

      <h3>当前持仓</h3>
      <el-table v-if="(acc.positions || []).length" :data="acc.positions" size="small">
        <el-table-column label="股票" min-width="120"><template #default="{ row }"><span class="stock-name">{{ row.name || row.symbol }}</span><span class="stock-code num">{{ row.symbol }}</span></template></el-table-column>
        <el-table-column label="入场" min-width="140"><template #default="{ row }"><span class="num">{{ row.entry_day }} @ {{ num(row.entry_price) }}</span></template></el-table-column>
        <el-table-column label="现价" width="80" align="right"><template #default="{ row }"><span class="num">{{ num(row.mark) }}</span></template></el-table-column>
        <el-table-column label="浮盈" width="110" align="right"><template #default="{ row }"><RiseFall :value="row.unrealized_pct" /></template></el-table-column>
        <el-table-column label="止损" width="80" align="right"><template #default="{ row }"><span class="num">{{ num(row.stop) }}</span></template></el-table-column>
        <el-table-column label="目标" width="80" align="right"><template #default="{ row }"><span class="num">{{ num(row.target) }}</span></template></el-table-column>
        <el-table-column label="保本" width="80"><template #default="{ row }">{{ row.breakeven_armed ? '已武装' : '—' }}</template></el-table-column>
      </el-table>
      <p v-else class="empty">当前无持仓。</p>

      <h3>近期平仓</h3>
      <ul v-if="sells.length" class="rows">
        <li v-for="(t, i) in sells" :key="i">
          <div class="row-head"><span>{{ t.symbol }} · {{ EXIT_LABEL[t.reason] || t.reason }}</span><span class="num">{{ t.date }}</span></div>
          <div class="muted">@ {{ num(t.price) }} · 盈亏 <span class="num" :class="direction(t.pnl)">{{ num(t.pnl) }}</span>（<RiseFall :value="t.return_pct" bare />）</div>
        </li>
      </ul>
      <p v-else class="empty">暂无平仓记录。</p>

      <h3>操作记录（每一笔，最新在前）</h3>
      <el-table v-if="ops.length" :data="ops" size="small">
        <el-table-column label="时间" width="140"><template #default="{ row }"><span class="num small">{{ fmtDateTime(row.at) }}</span></template></el-table-column>
        <el-table-column label="操作" width="120"><template #default="{ row }">{{ OP_TYPE[row.type] || row.type }}</template></el-table-column>
        <el-table-column label="股票" width="110"><template #default="{ row }"><span class="num">{{ row.symbol }}</span></template></el-table-column>
        <el-table-column label="价格 / 股数" width="130"><template #default="{ row }"><span class="num">{{ row.price != null ? `${num(row.price)} / ${row.shares}` : '—' }}</span></template></el-table-column>
        <el-table-column label="说明" min-width="220"><template #default="{ row }"><span class="small">{{ row.detail }}</span></template></el-table-column>
      </el-table>
      <p v-else class="empty">暂无操作：盘中引擎按条件买入、止损、止盈、计划作废/过期时，每一次都会记在这里。</p>

      <p v-if="planCounts" class="muted">计划状态：{{ planCounts }}</p>
      <ul v-if="(acc.issues || []).length" class="issues"><li v-for="(i, k) in acc.issues" :key="k">{{ i }}</li></ul>
    </template>
  </el-card>
</template>

<style scoped>
h3 { font-size: 14px; margin: 18px 0 8px; }
.small { font-size: 12.5px; }
.empty { margin: 0; font-size: 13px; color: var(--as-muted); background: var(--el-fill-color-light); border-radius: 8px; padding: 10px 12px; }
.rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.rows li { font-size: 13px; line-height: 1.6; padding: 8px 10px; background: var(--el-fill-color-light); border-radius: 8px; }
.row-head { display: flex; justify-content: space-between; gap: 8px; font-weight: 600; }
.issues { color: var(--as-muted); font-size: 12.5px; }
</style>
