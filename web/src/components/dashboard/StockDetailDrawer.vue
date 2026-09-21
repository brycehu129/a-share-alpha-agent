<script setup>
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { get } from '../../api'
import { fmtAmount, fmtBoards, fmtNum, fmtPct, fmtPrice, fmtTs } from '../../format'
import { stockDetail } from '../../composables/useStockDetail'
import RiseFall from '../RiseFall.vue'

// 个股详情抽屉：龙虎榜、涨跌停池、次日关注三处共用。四块数据（行情、日K、公司资料、龙虎榜席位）各自加载各自失败，
// 缺哪块就在哪块显示原因，不影响其余。K 线图（ECharts）只在这里才会被加载。
const KlineChart = defineAsyncComponent(() => import('./KlineChart.vue'))

const open = computed({ get: () => stockDetail.open, set: (v) => { stockDetail.open = v } })
const data = ref(null)
const loading = ref(false)
const error = ref('')
let seq = 0

async function load(refresh = false) {
  if (!stockDetail.symbol) return
  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    const r = await get(`/api/market/stock?symbol=${encodeURIComponent(stockDetail.symbol)}${refresh ? '&refresh=1' : ''}`)
    if (mine === seq) data.value = r.detail
  } catch (e) {
    if (mine === seq) error.value = e.message || String(e)
  } finally {
    if (mine === seq) loading.value = false
  }
}
// 每次打开都重取（行情是实时的）；换股票先清空，免得闪出上一只的数据。
watch(() => [stockDetail.open, stockDetail.symbol], ([isOpen]) => {
  if (!isOpen) return
  data.value = null
  load()
})

const q = computed(() => (data.value && data.value.quote) || null)
const profile = computed(() => (data.value && data.value.profile) || null)
const ctx = computed(() => (data.value && data.value.review) || null)
const errors = computed(() => (data.value && data.value.errors) || {})
const title = computed(() => (q.value && q.value.name) || stockDetail.name || stockDetail.symbol)
const exchange = computed(() => ({ sh: '上证', sz: '深证', bj: '北证' })[stockDetail.symbol.slice(0, 2)] || '')
const board = computed(() => {
  const c = stockDetail.symbol.slice(2)
  return c.startsWith('688') ? '科创板' : c.startsWith('30') ? '创业板' : stockDetail.symbol.startsWith('bj') ? '北交所' : '主板'
})
const lastPct = computed(() => (q.value ? Number(q.value.change_pct) : null))
const chgAmount = computed(() => (q.value ? Number(q.value.last) - Number(q.value.previous_close) : null))
const num = (v, d = 2) => (v === null || v === undefined || v === '' ? '—' : fmtNum(v, d))
const wanYi = (yi) => (yi === null || yi === undefined ? '—' : `${fmtNum(yi, 2)}亿`)

// 行情快照的格子：和东财/同花顺 App 的一致
const snapshot = computed(() => {
  const x = q.value
  if (!x) return []
  return [
    ['最新价', fmtPrice(x.last)], ['涨跌额', chgAmount.value === null ? '—' : (chgAmount.value > 0 ? '+' : '') + chgAmount.value.toFixed(2)],
    ['今开', fmtPrice(x.open)], ['昨收', fmtPrice(x.previous_close)],
    ['最高', fmtPrice(x.high)], ['最低', fmtPrice(x.low)],
    ['成交量', x.volume_raw ? fmtNum(Number(x.volume_raw) / 1e4, 2) + '万手' : '—'], ['成交额', x.amount_wan ? fmtAmount(Number(x.amount_wan) * 1e4) : '—'],
    ['换手率', num(x.turnover_pct) + '%'], ['量比', num(x.volume_ratio)],
    ['振幅', num(x.amplitude_pct) + '%'], ['涨停/跌停价', `${num(x.limit_up)} / ${num(x.limit_down)}`],
    ['总市值', wanYi(x.total_cap_yi === null ? null : Number(x.total_cap_yi))], ['流通市值', wanYi(x.float_cap_yi === null ? null : Number(x.float_cap_yi))],
    ['市盈率', num(x.pe)], ['市净率', num(x.pb)],
  ]
})

const POOL_LABEL = { zt: '涨停', dt: '跌停', zb: '炸板', yzt: '昨日涨停', qs: '强势股' }
const poolBadges = computed(() => {
  const p = (ctx.value && ctx.value.pools) || {}
  return Object.keys(p).map((k) => {
    const r = p[k]
    let extra = ''
    if (k === 'zt') extra = `${fmtBoards(r.boards)} · 首封 ${(r.first_seal || '—').slice(0, 5)} · 封板资金 ${fmtAmount(r.seal_fund)} · 炸板 ${r.open_times || 0} 次`
    if (k === 'zb') extra = `首封 ${(r.first_seal || '—').slice(0, 5)} · 炸板 ${r.open_times || 0} 次`
    if (k === 'dt') extra = `封单资金 ${fmtAmount(r.seal_fund)} · 连续跌停 ${r.days || 1} 天`
    if (k === 'yzt') extra = `昨日${fmtBoards(r.boards)}，今日 ${fmtPct(r.pct)}`
    return { k, label: POOL_LABEL[k] || k, extra, type: k === 'dt' ? 'success' : k === 'zb' ? 'warning' : k === 'qs' ? 'info' : 'danger' }
  })
})
const VERDICT = { focus: '重点关注', watch: '观察', avoid: '回避' }
const seatGroups = computed(() => (data.value && data.value.seats) || [])
const groupTab = ref(0)
watch(seatGroups, () => { groupTab.value = 0 })
// 页签名：当日口径 / 连续多日累计口径；有多个当日口径（不同上榜原因）时加序号
const tabLabel = (g, i) => (g.multiday ? '累计口径' : seatGroups.value.filter((x) => !x.multiday).length > 1 ? `当日 ${seatGroups.value.slice(0, i + 1).filter((x) => !x.multiday).length}` : '当日')
const seatRows = (list, side) => list.map((s) => ({ ...s, side }))
const shortReason = (rs) => rs.map((r) => r.replace(/^有价格涨跌幅限制的/, '').replace(/^非S证券/, '')).join('；')
</script>

<template>
  <el-drawer v-model="open" size="min(760px, 100vw)" direction="rtl" destroy-on-close :with-header="true">
    <template #header>
      <div class="hd">
        <span class="hd-name">{{ title }}</span>
        <span class="hd-code num">{{ stockDetail.symbol.slice(2) }}</span>
        <el-tag v-if="exchange" size="small" effect="plain">{{ exchange }}</el-tag>
        <el-tag v-if="board" size="small" effect="plain">{{ board }}</el-tag>
      </div>
    </template>
    <div v-loading="loading && !data" class="body" style="min-height: 260px">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false"><el-button size="small" @click="load(true)">重试</el-button></el-alert>

      <template v-if="data">
        <!-- 头部：价格 + 概念标签 -->
        <section>
          <div v-if="q" class="price-row">
            <span class="price num" :class="lastPct > 0 ? 'rise' : lastPct < 0 ? 'fall' : 'flat'">{{ fmtPrice(q.last) }}</span>
            <RiseFall :value="lastPct" class="chg" />
            <span class="muted num">行情时间 {{ fmtTs(q.quote_at) }}</span>
            <el-button size="small" :loading="loading" style="margin-left: auto" @click="load(true)">刷新</el-button>
          </div>
          <p v-else-if="errors.quote" class="muted">行情暂无：{{ errors.quote }}</p>
          <div v-if="profile" class="tags">
            <el-tag v-if="profile.industry_path.length" size="small" type="info">{{ profile.industry_path.join(' · ') }}</el-tag>
            <el-tag v-if="profile.region" size="small" type="info">{{ profile.region }}</el-tag>
            <el-tag v-for="c in profile.concepts" :key="c" size="small" effect="plain" round>{{ c }}</el-tag>
          </div>
          <p v-else-if="errors.profile" class="muted">公司资料/概念暂无：{{ errors.profile }}</p>
        </section>

        <!-- 复盘位置：这只股票在今天的涨跌停池、龙虎榜、次日关注里的情况 -->
        <section v-if="ctx && (poolBadges.length || ctx.lhb || ctx.watch)">
          <h3>盘后复盘 <span class="sub num">{{ ctx.date }}<template v-if="ctx.fetched_at"> · 取数 {{ fmtTs(ctx.fetched_at) }}</template></span></h3>
          <div class="badges">
            <div v-for="b in poolBadges" :key="b.k" class="badge"><el-tag :type="b.type" size="small">{{ b.label }}</el-tag><span class="muted">{{ b.extra }}</span></div>
            <div v-if="ctx.lhb" class="badge">
              <el-tag type="warning" size="small">龙虎榜</el-tag>
              <span class="muted">{{ ctx.lhb_date }} · 买 {{ fmtAmount(ctx.lhb.buy) }} 卖 {{ fmtAmount(ctx.lhb.sell) }} · 净{{ ctx.lhb.net > 0 ? '买' : '卖' }} {{ fmtAmount(ctx.lhb.net) }}</span>
            </div>
          </div>
          <div v-if="ctx.watch" class="watch">
            <div><el-tag size="small" type="primary">次日关注</el-tag> <b class="num">规则分 {{ ctx.watch.score }}</b> <span class="muted">{{ (ctx.watch.tags || []).join(' · ') }}</span></div>
            <p v-if="ctx.watch.reasons && ctx.watch.reasons.length" class="line"><b>加分</b> {{ ctx.watch.reasons.join('；') }}</p>
            <p v-if="ctx.watch.risks && ctx.watch.risks.length" class="line"><b>风险</b> {{ ctx.watch.risks.join('；') }}</p>
            <template v-if="ctx.watch.ai">
              <p class="line"><b>AI · {{ VERDICT[ctx.watch.ai.verdict] || ctx.watch.ai.verdict }}</b> {{ ctx.watch.ai.view }}</p>
              <p class="line"><b>思路</b> {{ ctx.watch.ai.plan }}</p>
              <p class="line"><b>风险</b> {{ ctx.watch.ai.risk }}</p>
            </template>
          </div>
        </section>

        <!-- K 线 -->
        <section>
          <h3>日 K <span class="sub">前复权 · 近 {{ data.kline ? data.kline.length : 0 }} 个交易日</span></h3>
          <KlineChart v-if="data.kline" :bars="data.kline" />
          <p v-else class="muted">日 K 暂无：{{ errors.kline || '没有数据' }}</p>
        </section>

        <!-- 行情快照 -->
        <section v-if="q">
          <h3>行情快照</h3>
          <div class="snap">
            <div v-for="[k, v] in snapshot" :key="k" class="snap-cell"><span class="muted">{{ k }}</span><b class="num">{{ v }}</b></div>
          </div>
        </section>

        <!-- 龙虎榜席位 -->
        <section v-if="data.seats !== undefined">
          <h3>龙虎榜详情 <span class="sub num">{{ ctx && ctx.lhb_date }}<template v-if="ctx && ctx.lhb_fetched_at"> · 取数 {{ fmtTs(ctx.lhb_fetched_at) }}</template></span></h3>
          <p v-if="errors.seats" class="muted">席位暂无：{{ errors.seats }}</p>
          <p v-else-if="!seatGroups.length" class="muted">这只股票在该日龙虎榜上没有席位明细。</p>
          <template v-else>
            <el-tabs v-if="seatGroups.length > 1" v-model="groupTab" type="card" size="small">
              <el-tab-pane v-for="(g, i) in seatGroups" :key="i" :name="i" :label="tabLabel(g, i)" />
            </el-tabs>
            <template v-for="(g, i) in seatGroups" :key="i">
              <div v-show="groupTab === i">
                <p class="reason">{{ shortReason(g.reasons) }}</p>
                <p class="muted">买入 <b class="rise num">{{ fmtAmount(g.buy_total) }}</b> · 卖出 <b class="fall num">{{ fmtAmount(g.sell_total) }}</b> · 净{{ g.net >= 0 ? '买' : '卖' }} <b class="num" :class="g.net >= 0 ? 'rise' : 'fall'">{{ fmtAmount(g.net) }}</b><template v-if="g.multiday">（累计口径）</template></p>
                <el-table :data="[...seatRows(g.buy, '买入'), ...seatRows(g.sell, '卖出')]" size="small" :show-header="true">
                  <el-table-column label="方向" width="62"><template #default="{ row }"><span :class="row.side === '买入' ? 'rise' : 'fall'">{{ row.side }}</span></template></el-table-column>
                  <el-table-column label="营业部" min-width="200" prop="name" show-overflow-tooltip />
                  <el-table-column label="买入额" width="92" align="right"><template #default="{ row }"><span class="num" :class="row.buy ? 'rise' : ''">{{ fmtAmount(row.buy) }}</span></template></el-table-column>
                  <el-table-column label="卖出额" width="92" align="right"><template #default="{ row }"><span class="num" :class="row.sell ? 'fall' : ''">{{ fmtAmount(row.sell) }}</span></template></el-table-column>
                  <el-table-column label="净额" width="96" align="right"><template #default="{ row }"><span class="num" :class="row.net > 0 ? 'rise' : 'fall'">{{ (row.net > 0 ? '' : '-') + fmtAmount(row.net) }}</span></template></el-table-column>
                </el-table>
              </div>
            </template>
          </template>
        </section>

        <!-- 公司资料 -->
        <section v-if="profile">
          <h3>公司资料</h3>
          <dl class="kv">
            <template v-if="profile.full_name"><dt>公司全称</dt><dd>{{ profile.full_name }}</dd></template>
            <template v-if="profile.industry_path.length"><dt>所属行业</dt><dd>{{ profile.industry_path.join(' / ') }}</dd></template>
            <template v-if="profile.address"><dt>办公地址</dt><dd>{{ profile.address }}</dd></template>
            <template v-if="profile.website"><dt>官网</dt><dd>{{ profile.website }}</dd></template>
            <template v-if="profile.phone"><dt>电话</dt><dd class="num">{{ profile.phone }}</dd></template>
            <template v-if="profile.email"><dt>邮箱</dt><dd>{{ profile.email }}</dd></template>
          </dl>
          <p v-if="profile.intro" class="muted intro">{{ profile.intro }}<template v-if="profile.intro.length >= 400">…</template></p>
        </section>
        <p class="muted">详情取数时间 <span class="num">{{ fmtTs(data.fetched_at) }}</span>。行情来自腾讯，龙虎榜/涨跌停/公司资料来自东方财富公开接口。以上是事实数据，不构成投资建议。</p>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.hd { display: flex; align-items: center; gap: 8px; }
.hd-name { font-size: 18px; font-weight: 700; }
.hd-code { color: var(--as-muted); font-size: 14px; }
.body { display: flex; flex-direction: column; gap: 18px; }
h3 { margin: 0 0 8px; font-size: 15px; }
h3 .sub { font-weight: 400; font-size: 12.5px; color: var(--as-muted); margin-left: 6px; }
.price-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 14px; margin-bottom: 8px; }
.price { font-size: 30px; font-weight: 800; }
.chg { font-size: 16px; font-weight: 700; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; }
.badges { display: flex; flex-direction: column; gap: 6px; }
.badge { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.watch { margin-top: 10px; padding: 10px 12px; border-radius: 8px; background: var(--el-fill-color-light); font-size: 13px; }
.watch .line { margin: 4px 0 0; line-height: 1.6; }
.snap { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px 10px; }
.snap-cell { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.snap-cell b { font-size: 15px; overflow-wrap: anywhere; }
.reason { margin: 0 0 4px; font-size: 13px; font-weight: 600; }
.kv { display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 6px 12px; margin: 0; font-size: 13px; }
.kv dt { color: var(--as-muted); }
.kv dd { margin: 0; overflow-wrap: anywhere; }
.intro { margin: 10px 0 0; line-height: 1.7; }
@media (max-width: 640px) { .snap { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
