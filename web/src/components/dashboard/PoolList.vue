<script setup>
import { fmtAmount, fmtBoards, fmtPrice } from '../../format'
import { openStock } from '../../composables/useStockDetail'
import RiseFall from '../RiseFall.vue'

// 涨停池/跌停池/炸板池/昨日涨停/强势股 的一列：每只股票三行——名称+行业+涨幅 / 代码+现价 / 池子特有的细节。
// kind: zt | dt | zb | yzt | qs。点击整行打开个股详情抽屉。
defineProps({
  rows: { type: Array, default: () => [] },
  kind: { type: String, required: true },
})

const t = (v) => (v ? v : '—')
function detail(r, kind) {
  if (kind === 'zt') return [`首次封板 ${t(r.first_seal)}`, `最终封板 ${t(r.last_seal)}`, `封板资金 ${fmtAmount(r.seal_fund)}`, `炸板 ${r.open_times || 0} 次`]
  if (kind === 'dt') return [`最后封板 ${t(r.last_seal)}`, `封单资金 ${fmtAmount(r.seal_fund)}`, `连续跌停 ${r.days || 1} 天`, `开板 ${r.open_times || 0} 次`]
  if (kind === 'zb') return [`首次封板 ${t(r.first_seal)}`, `炸板 ${r.open_times || 0} 次`, `振幅 ${r.amplitude ?? '—'}%`, `换手 ${r.turnover ?? '—'}%`]
  if (kind === 'yzt') return [`昨日${fmtBoards(r.boards)}`, `昨日首封 ${t(r.first_seal)}`, `振幅 ${r.amplitude ?? '—'}%`, `换手 ${r.turnover ?? '—'}%`]
  return [`换手 ${r.turnover ?? '—'}%`, `量比 ${r.volume_ratio ?? '—'}`, r.new_high ? '创新高' : '未创新高', `成交额 ${fmtAmount(r.amount)}`]
}
</script>

<template>
  <ul class="pool">
    <li v-for="r in rows" :key="r.symbol">
      <button type="button" class="row" @click="openStock(r.symbol, r.name)">
        <span class="l1">
          <span class="name">{{ r.name }}<span v-if="r.industry" class="ind">{{ r.industry }}</span></span>
          <span class="changes">
            <RiseFall :value="r.current_pct" bare class="pct" />
            <span class="review-pct">复盘 {{ r.pct == null ? '—' : `${r.pct}%` }}</span>
          </span>
        </span>
        <span class="l2 num"><span>{{ r.code }}</span><span>现 {{ fmtPrice(r.current_price) }} · 复盘 {{ fmtPrice(r.price) }}</span></span>
        <span class="l3"><span v-for="(d, i) in detail(r, kind)" :key="i">{{ d }}</span></span>
      </button>
    </li>
    <li v-if="!rows.length" class="empty muted">暂无</li>
  </ul>
</template>

<style scoped>
.pool { list-style: none; margin: 0; padding: 0; }
.row { display: flex; flex-direction: column; gap: 2px; width: 100%; padding: 8px 12px; text-align: left; background: transparent; border: 0; border-bottom: 1px solid var(--el-border-color-lighter); font: inherit; color: inherit; cursor: pointer; }
.row:hover, .row:focus-visible { background: var(--el-fill-color-light); outline: none; }
.l1, .l2 { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.name { font-weight: 650; font-size: 13.5px; min-width: 0; }
.ind { margin-left: 6px; padding: 0 5px; font-size: 11px; font-weight: 500; color: var(--el-color-primary); background: var(--el-color-primary-light-9); border-radius: 3px; white-space: nowrap; }
.pct { font-weight: 700; font-size: 13.5px; }
.changes { display: flex; flex-direction: column; align-items: flex-end; line-height: 1.2; }
.review-pct { font-size: 10.5px; color: var(--as-muted); font-weight: 400; }
.l2 { font-size: 12px; color: var(--as-muted); }
.l3 { display: flex; flex-wrap: wrap; gap: 0 12px; font-size: 11.5px; color: var(--as-muted); }
.empty { padding: 16px 12px; }
</style>
