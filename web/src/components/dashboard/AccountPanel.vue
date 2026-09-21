<script setup>
import { computed } from 'vue'
import { fmtMoney, fmtNum } from '../../format'
import StatGrid from '../StatGrid.vue'
import StatTile from '../StatTile.vue'
import RiseFall from '../RiseFall.vue'
import NavChart from './NavChart.vue'

const props = defineProps({ portfolio: { type: Object, required: true } })
const p = computed(() => props.portfolio)
const SIDE = { buy: '买入', sell: '卖出', skipped: '未成交' }
const trades = computed(() => p.value.trades.slice(-6).reverse())
// 最大回撤：本身就是「亏损幅度」，不是涨跌，用中性色（绿色会被读成「好」）；为 0 时不显示负号。
const drawdown = computed(() => {
  const v = Math.abs(p.value.max_drawdown_pct)
  return v < 0.005 ? '0.00%' : `−${v.toFixed(2)}%`
})
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-title">
        <span>虚拟账户</span>
        <span class="sub">估值日 {{ p.last_date }} · {{ p.valuation_status === 'current' ? '估值正常' : '估值阻塞' }}</span>
      </div>
    </template>
    <div class="account-body">
      <div class="col">
        <StatGrid>
          <StatTile label="总资产" :value="fmtMoney(p.equity)" />
          <StatTile label="现金" :value="fmtMoney(p.cash)" />
          <StatTile label="相对沪深300超额"><RiseFall :value="p.excess_pp" /></StatTile>
          <StatTile label="最大回撤" :value="drawdown" />
          <StatTile label="持仓数量" :value="`${p.positions.length} 只`" />
          <StatTile label="已平仓胜率" :value="p.trade_win_rate !== null ? `${p.trade_win_rate}%` : '暂无样本'" />
        </StatGrid>
        <div class="chart">
          <NavChart :points="p.curve" benchmark label="虚拟账户净值与沪深300基准" />
          <div class="legend">
            <span><i class="dot" style="background: var(--el-color-primary)" />虚拟账户净值</span>
            <span><i class="dot" style="background: var(--as-flat)" />沪深300基准</span>
          </div>
        </div>
      </div>

      <div class="col">
        <h3>当前持仓</h3>
        <p v-if="!p.positions.length" class="empty">当前无持仓；已生成的入场计划尚未在允许窗口内成交。</p>
        <ul v-else class="rows">
          <li v-for="pos in p.positions" :key="pos.symbol"><span class="num">{{ pos.symbol }}</span> · {{ pos.shares }}股 · 成本 <span class="num">{{ fmtNum(pos.entry_price) }}</span></li>
        </ul>

        <h3>近期成交 / 尝试记录</h3>
        <p v-if="!trades.length" class="empty">暂无成交或尝试记录。</p>
        <ul v-else class="rows">
          <li v-for="(t, i) in trades" :key="i">
            <div class="row-head"><span><span class="num">{{ t.symbol }}</span> · {{ SIDE[t.side] || t.side }}</span><span class="num muted">{{ t.date }}</span></div>
            <div v-if="t.reason" class="muted">{{ t.reason }}</div>
          </li>
        </ul>
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.account-body { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px 28px; align-items: start; }
.col { min-width: 0; }
.chart { margin-top: 14px; }
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--el-text-color-regular); margin-top: 6px; }
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.dot { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }
h3 { font-size: 14px; margin: 0 0 8px; }
h3:not(:first-child) { margin-top: 18px; }
.empty { margin: 0; font-size: 13px; color: var(--as-muted); background: var(--el-fill-color-light); border-radius: 8px; padding: 10px 12px; }
.rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.rows li { font-size: 13px; line-height: 1.6; padding: 8px 10px; background: var(--el-fill-color-light); border-radius: 8px; }
.row-head { display: flex; justify-content: space-between; gap: 8px; font-weight: 600; }
@media (max-width: 860px) { .account-body { grid-template-columns: minmax(0, 1fr); } }
</style>
