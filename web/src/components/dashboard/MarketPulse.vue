<script setup>
import { computed } from 'vue'
import { fmtAmount, fmtFlow, fmtNum, fmtTs, fmtVolumeTrend, shortDate } from '../../format'
import StatTile from '../StatTile.vue'

// 大盘脉搏：涨跌家数、涨跌停/炸板家数、两市成交额（较上一交易日放量/缩量）、大盘主力资金净流入。
// 全部是实时取数（/api/dashboard 的 live 块）+ 涨跌停池；每一组各自缺失各自显示「暂无」，不影响别的组。
const props = defineProps({
  live: { type: Object, default: null },
  pools: { type: Object, default: null }, // review.pools
  poolDate: { type: String, default: '' },
  poolTime: { type: String, default: '' }, // 涨跌停池的取数时间（精确到秒）
})

const breadth = computed(() => (props.live && props.live.breadth) || null)
const turnover = computed(() => (props.live && props.live.turnover) || null)
const flow = computed(() => (props.live && props.live.flow) || null)
const errors = computed(() => (props.live && props.live.errors) || {})
const complete = computed(() => !props.live || props.live.complete !== false)

const total = computed(() => (breadth.value ? breadth.value.total : 0))
const seg = (n) => (total.value ? (n / total.value) * 100 : 0)

const poolCount = (k) => (props.pools && props.pools[k] ? props.pools[k].total : null)
const cnt = (v) => (v === null || v === undefined ? '—' : String(v))
// 封板率 = 涨停 /（涨停 + 炸板）。两个池子有一个缺就不算。
const sealRate = computed(() => {
  const zt = poolCount('zt')
  const zb = poolCount('zb')
  return zt === null || zb === null || zt + zb === 0 ? null : (zt / (zt + zb)) * 100
})

const trend = computed(() => {
  const t = turnover.value
  return t ? fmtVolumeTrend(t.delta, t.delta_pct) : ''
})
const trendTone = computed(() => (turnover.value ? (turnover.value.delta > 0 ? 'rise' : turnover.value.delta < 0 ? 'fall' : '') : ''))
const mainTone = computed(() => (flow.value ? (flow.value.main_net > 0 ? 'rise' : flow.value.main_net < 0 ? 'fall' : '') : ''))
const tone = (v) => (v > 0 ? 'rise' : v < 0 ? 'fall' : 'flat')
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-title">
        <span>大盘脉搏</span>
        <span class="sub">沪深北全市场 · 实时取数<template v-if="poolDate">；涨跌停 {{ poolDate }}</template></span>
      </div>
    </template>

    <!-- 涨跌家数：红绿条直观看赚钱效应 -->
    <div v-if="breadth" class="breadth">
      <div class="bar" role="img" :aria-label="`上涨 ${breadth.up} 家，下跌 ${breadth.down} 家，平盘 ${breadth.flat} 家`">
        <span class="up" :style="{ width: seg(breadth.up) + '%' }" />
        <span class="flat-seg" :style="{ width: seg(breadth.flat) + '%' }" />
        <span class="down" :style="{ width: seg(breadth.down) + '%' }" />
      </div>
      <div class="bar-legend num">
        <span class="rise">上涨 {{ breadth.up }}</span>
        <span class="flat">平盘 {{ breadth.flat }}</span>
        <span class="fall">下跌 {{ breadth.down }}</span>
      </div>
    </div>
    <p v-else class="muted note">涨跌家数暂无：{{ errors.breadth || '行情源没有返回' }}</p>

    <div class="tiles">
      <StatTile label="上涨占比" :value="breadth && breadth.up_pct !== null ? fmtNum(breadth.up_pct, 1) + '%' : '—'" :tone="breadth ? (breadth.up_pct >= 50 ? 'rise' : 'fall') : ''" />
      <StatTile label="涨停" :value="cnt(poolCount('zt'))" :tone="poolCount('zt') ? 'rise' : ''" />
      <StatTile label="跌停" :value="cnt(poolCount('dt'))" :tone="poolCount('dt') ? 'fall' : ''" />
      <StatTile label="炸板" :value="cnt(poolCount('zb'))" />
      <StatTile label="封板率" :value="sealRate === null ? '—' : fmtNum(sealRate, 0) + '%'" />

      <StatTile :label="complete ? '两市成交额' : '两市成交额（盘中累计）'" :value="turnover ? fmtAmount(turnover.amount) : '—'" />
      <StatTile :label="`较上一交易日${turnover ? '（' + shortDate(turnover.prev_date) + '）' : ''}`" :tone="complete ? trendTone : ''">
        <span class="trend">{{ turnover ? (complete ? trend : '盘中不与全天比') : '—' }}</span>
      </StatTile>
      <StatTile :label="complete ? '主力净流入' : '主力净流入（盘中累计）'" :tone="mainTone" :value="flow ? fmtFlow(flow.main_net) : '—'" />
    </div>

    <div v-if="flow" class="flow-detail num">
      <span>超大单 <b :class="tone(flow.huge_net)">{{ fmtFlow(flow.huge_net) }}</b></span>
      <span>大单 <b :class="tone(flow.big_net)">{{ fmtFlow(flow.big_net) }}</b></span>
      <span>中单 <b :class="tone(flow.mid_net)">{{ fmtFlow(flow.mid_net) }}</b></span>
      <span>小单 <b :class="tone(flow.small_net)">{{ fmtFlow(flow.small_net) }}</b></span>
    </div>

    <p v-if="errors.turnover || errors.flow" class="muted note">
      <template v-if="errors.turnover">成交额暂无：{{ errors.turnover }}。</template>
      <template v-if="errors.flow">资金流向暂无：{{ errors.flow }}。</template>
    </p>
    <!-- 每一组数据各自的时间，精确到秒：行情时间是交易所给的，取数时间是我们向数据源请求完成的时刻 -->
    <ul class="times num">
      <li>涨跌家数 <b>{{ breadth ? fmtTs(breadth.fetched_at) : '—' }}</b><i v-if="breadth && breadth.stale">（取数失败，沿用上一次）</i></li>
      <li>成交额 <b>{{ turnover ? fmtTs(turnover.quote_at) : '—' }}</b></li>
      <li>资金流向 <b>{{ flow ? fmtTs(flow.fetched_at) : '—' }}</b><i v-if="flow && flow.stale">（取数失败，沿用上一次）</i></li>
      <li>涨跌停池 <b>{{ poolTime ? fmtTs(poolTime) : '—' }}</b></li>
    </ul>
    <p class="muted note">
      成交额为沪深两市之和（不含北交所）；资金流向为沪深两市合计，主力 = 超大单 + 大单（东方财富口径）；涨跌停家数取自交易所口径的涨跌停池（含 ST），封板率 = 涨停 ÷（涨停 + 炸板）。
    </p>
  </el-card>
</template>

<style scoped>
.breadth { margin-bottom: 14px; }
.bar { display: flex; height: 12px; border-radius: 999px; overflow: hidden; background: var(--el-fill-color); }
.bar .up { background: var(--as-rise); }
.bar .flat-seg { background: var(--el-border-color); }
.bar .down { background: var(--as-fall); }
.bar-legend { display: flex; justify-content: space-between; gap: 8px; margin-top: 6px; font-size: 13px; font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; background: var(--el-border-color-light); border: 1px solid var(--el-border-color-light); border-radius: 8px; overflow: hidden; }
.trend { font-size: 15px; font-weight: 700; }
.flow-detail { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 10px; font-size: 13px; color: var(--as-muted); }
.flow-detail b { font-weight: 700; }
.note { margin: 10px 0 0; }
@media (max-width: 640px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.times { list-style: none; display: flex; flex-wrap: wrap; gap: 2px 18px; margin: 10px 0 0; padding: 0; font-size: 12px; color: var(--as-muted); }
.times b { font-weight: 600; color: var(--el-text-color-regular); }
.times i { font-style: normal; color: var(--el-color-warning); }
</style>
