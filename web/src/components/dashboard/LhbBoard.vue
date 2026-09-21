<script setup>
import { computed, ref } from 'vue'
import { fmtAmount, fmtPrice, fmtTs } from '../../format'
import { openStock } from '../../composables/useStockDetail'
import RiseFall from '../RiseFall.vue'

// 龙虎榜：独立数据区。个股汇总列表（买入/卖出/净额），点击任一股票打开详情抽屉（营业部席位在抽屉里）。
// 数据来自东方财富，交易日 16:30 起陆续发布、17:30 前补全；页面上必须标出数据日期，不拿旧日子冒充今天。
const props = defineProps({ lhb: { type: Object, default: null }, todayLabel: { type: String, default: '' } })

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'buy', label: '净买入' },
  { key: 'sell', label: '净卖出' },
]
const filter = ref('buy')
const rows = computed(() => (props.lhb && props.lhb.rows) || [])
const shown = computed(() => {
  if (filter.value === 'buy') return rows.value.filter((r) => r.net > 0)
  if (filter.value === 'sell') return rows.value.filter((r) => r.net < 0).sort((a, b) => a.net - b.net)
  return rows.value
})
const count = (k) => (k === 'all' ? rows.value.length : rows.value.filter((r) => (k === 'buy' ? r.net > 0 : r.net < 0)).length)
const stale = computed(() => !!(props.lhb && props.todayLabel && props.lhb.date !== props.todayLabel))
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-title">
        <span>龙虎榜复盘 <span v-if="lhb" class="date num">{{ lhb.date }}</span></span>
        <span class="sub">数据时间 <span class="num">{{ lhb ? fmtTs(lhb.fetched_at) : '—' }}</span> · 每个交易日 16:30 / 17:30 更新</span>
      </div>
    </template>

    <p v-if="!lhb || !rows.length" class="muted" style="margin: 0">
      <template v-if="!lhb">龙虎榜暂无：还没有落盘过复盘数据（定时任务每个交易日 16:30 / 17:30 抓取）。</template>
      <template v-else>{{ lhb.date }} 龙虎榜为空：东方财富通常在 16:30 之后才开始发布，17:30 前补全。</template>
    </p>
    <template v-else>
      <el-alert v-if="stale" type="warning" show-icon :closable="false" style="margin-bottom: 10px"
        :title="`当前展示的是 ${lhb.date} 的龙虎榜，${todayLabel} 的龙虎榜还没有发布（16:30 之后陆续更新）。`" />
      <div class="toolbar">
        <span class="total num">{{ count('all') }} 只上榜</span>
        <el-radio-group v-model="filter" size="small">
          <el-radio-button v-for="f in FILTERS" :key="f.key" :value="f.key">{{ f.label }} {{ count(f.key) }}</el-radio-button>
        </el-radio-group>
      </div>
      <div class="grid">
        <button v-for="r in shown" :key="r.symbol" type="button" class="cell" @click="openStock(r.symbol, r.name)">
          <span class="line1">
            <span class="name">{{ r.name }}</span>
            <RiseFall :value="r.pct" bare class="pct" />
          </span>
          <span class="line2 num"><span>{{ r.code }}</span><span>{{ fmtPrice(r.price) }}</span></span>
          <span class="line3">
            <span :class="r.net > 0 ? 'rise' : 'fall'">净{{ r.net > 0 ? '买入' : '卖出' }} {{ fmtAmount(r.net) }}</span>
            <span class="muted">买 {{ fmtAmount(r.buy) }} · 卖 {{ fmtAmount(r.sell) }}</span>
          </span>
          <span v-if="r.reasons && r.reasons.length" class="reason" :title="r.reasons.join('；')">{{ r.reasons[0] }}</span>
        </button>
      </div>
      <p v-if="!shown.length" class="muted">这一类当日没有个股。</p>
      <p class="muted note">点击个股查看营业部买卖前五席位。金额为龙虎榜当日口径（不含「连续三个交易日」类的累计口径）；同一只股票有多个上榜原因时合并为一行，原因悬停可看全部。</p>
    </template>
  </el-card>
</template>

<style scoped>
.date { font-size: 13px; font-weight: 600; color: var(--as-muted); margin-left: 6px; }
.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.total { font-size: 13px; color: var(--as-muted); }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0; border: 1px solid var(--el-border-color-light); border-radius: 8px; overflow: hidden; }
.cell { display: flex; flex-direction: column; gap: 3px; padding: 9px 12px; text-align: left; background: var(--el-bg-color); border: 0; border-bottom: 1px solid var(--el-border-color-lighter); font: inherit; color: inherit; cursor: pointer; min-width: 0; }
.cell:nth-child(odd) { border-right: 1px solid var(--el-border-color-lighter); }
.cell:hover, .cell:focus-visible { background: var(--el-fill-color-light); outline: none; }
.line1, .line2, .line3 { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.name { font-weight: 650; font-size: 14px; }
.pct { font-weight: 700; font-size: 14px; }
.line2 { font-size: 12px; color: var(--as-muted); }
.line3 { font-size: 12.5px; font-weight: 600; flex-wrap: wrap; }
.line3 .muted { font-weight: 400; font-size: 12px; }
.reason { font-size: 11.5px; color: var(--as-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.note { margin: 10px 0 0; }
@media (max-width: 720px) { .grid { grid-template-columns: minmax(0, 1fr); } .cell:nth-child(odd) { border-right: 0; } }
</style>
