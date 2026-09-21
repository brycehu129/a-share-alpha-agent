<script setup>
import { computed, ref } from 'vue'
import { openStock } from '../../composables/useStockDetail'
import PoolList from './PoolList.vue'

// 涨跌停池：参照同花顺/东财复盘的多列版式——连板天梯、涨停板、跌停板、炸板、昨日涨停、强势股，横向并排，
// 每列自己纵向滚动。窄屏时整排可横向滑动。数据来自东方财富涨跌停池（交易所口径，含 ST、北交所）。
const props = defineProps({ review: { type: Object, default: null } })

const pools = computed(() => (props.review && props.review.pools) || {})
const errors = computed(() => (props.review && props.review.errors) || {})
const rows = (k) => (pools.value[k] ? pools.value[k].rows : [])
const total = (k) => (pools.value[k] ? pools.value[k].total : null)

// 涨停板列：全部 / 首板 / 连板
const ztFilter = ref('all')
const ztRows = computed(() => {
  const r = rows('zt')
  if (ztFilter.value === 'first') return r.filter((x) => (x.boards || 1) <= 1)
  if (ztFilter.value === 'multi') return r.filter((x) => (x.boards || 1) >= 2)
  return r
})

// 连板天梯：N 板（≥2）按板数从高到低；首板按行业分组，行业里涨停多的在前。
const ladderFilter = ref('all')
const ladder = computed(() => {
  const zt = rows('zt')
  const levels = new Map()
  for (const r of zt) {
    const n = r.boards || 1
    if (n >= 2) levels.set(n, [...(levels.get(n) || []), r])
  }
  const multi = [...levels.entries()].sort((a, b) => b[0] - a[0]).map(([n, list]) => ({ n, list: list.slice().sort((a, b) => (a.first_seal || '').localeCompare(b.first_seal || '')) }))
  const byInd = new Map()
  for (const r of zt) {
    if ((r.boards || 1) <= 1) byInd.set(r.industry || '其他', [...(byInd.get(r.industry || '其他') || []), r])
  }
  const first = [...byInd.entries()].map(([industry, list]) => ({ industry, list: list.slice().sort((a, b) => (a.first_seal || '').localeCompare(b.first_seal || '')) })).sort((a, b) => b.list.length - a.list.length)
  return { multi, first, firstCount: first.reduce((s, g) => s + g.list.length, 0) }
})

const date = computed(() => (props.review && props.review.date) || '')
const sourceLabel = computed(() => (props.review && props.review.source === 'live' ? '实时' : '收盘复盘'))
</script>

<template>
  <el-card shadow="never" class="boards-card">
    <template #header>
      <div class="card-title">
        <span>涨跌停复盘 <span v-if="date" class="date num">{{ date }}</span></span>
        <span class="sub">{{ review ? sourceLabel : '' }} · 东方财富涨跌停池，交易所口径</span>
      </div>
    </template>

    <p v-if="!review || !review.pools" class="muted" style="margin: 0">涨跌停池暂无：还没有落盘过复盘数据，且现在取不到实时池（{{ (review && review.errors && review.errors.zt) || '休市或接口不可用' }}）。</p>
    <template v-else>
      <div v-if="Object.keys(errors).length" class="err muted">部分池子暂无：{{ Object.entries(errors).map(([k, v]) => `${k}（${v}）`).join('；') }}</div>
      <div class="strip">
        <!-- 连板天梯 -->
        <section class="col">
          <header>
            <b>连板天梯</b><span class="cnt num">{{ total('zt') ?? '—' }}</span>
            <el-radio-group v-model="ladderFilter" size="small">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="multi">连板</el-radio-button>
            </el-radio-group>
          </header>
          <div class="body">
            <div v-for="lv in ladder.multi" :key="lv.n" class="lv">
              <div class="lv-tag">{{ lv.n }}板 <span class="num">{{ lv.list.length }}</span></div>
              <div class="chips">
                <button v-for="r in lv.list" :key="r.symbol" type="button" class="chip" @click="openStock(r.symbol, r.name)">
                  <span class="num t">{{ (r.first_seal || '').slice(0, 5) }}</span><span class="n">{{ r.name }}</span><span class="num c">{{ r.code }}</span>
                </button>
              </div>
            </div>
            <template v-if="ladderFilter === 'all'">
              <div class="lv-tag first">首板 <span class="num">{{ ladder.firstCount }}</span></div>
              <div v-for="g in ladder.first" :key="g.industry" class="ind-group">
                <div class="ind-name">{{ g.industry }} <span class="num">{{ g.list.length }}</span></div>
                <div class="chips">
                  <button v-for="r in g.list" :key="r.symbol" type="button" class="chip" @click="openStock(r.symbol, r.name)">
                    <span class="num t">{{ (r.first_seal || '').slice(0, 5) }}</span><span class="n">{{ r.name }}</span><span class="num c">{{ r.code }}</span>
                  </button>
                </div>
              </div>
            </template>
            <p v-if="!ladder.multi.length && !ladder.firstCount" class="muted pad">暂无</p>
          </div>
        </section>

        <section class="col">
          <header>
            <b>涨停板</b><span class="cnt num">{{ total('zt') ?? '—' }}</span>
            <el-radio-group v-model="ztFilter" size="small">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="first">首板</el-radio-button>
              <el-radio-button value="multi">连板</el-radio-button>
            </el-radio-group>
          </header>
          <div class="body"><PoolList :rows="ztRows" kind="zt" /></div>
        </section>

        <section class="col">
          <header><b>跌停板</b><span class="cnt num">{{ total('dt') ?? '—' }}</span></header>
          <div class="body"><PoolList :rows="rows('dt')" kind="dt" /></div>
        </section>

        <section class="col">
          <header><b>炸板</b><span class="cnt num">{{ total('zb') ?? '—' }}</span></header>
          <div class="body"><PoolList :rows="rows('zb')" kind="zb" /></div>
        </section>

        <section class="col">
          <header><b>昨日涨停</b><span class="cnt num">{{ total('yzt') ?? '—' }}</span><span class="hint">今日表现</span></header>
          <div class="body"><PoolList :rows="rows('yzt')" kind="yzt" /></div>
        </section>

        <section class="col">
          <header><b>强势股</b><span class="cnt num">{{ total('qs') ?? '—' }}</span></header>
          <div class="body"><PoolList :rows="rows('qs')" kind="qs" /></div>
        </section>
      </div>
      <p class="muted note">点击任一股票查看行情、K 线、龙虎榜席位与所属概念。炸板 = 当日盘中触及涨停后打开；昨日涨停列展示的是这些股票今日的涨跌；强势股 = 60 日新高且近期涨幅居前等东方财富口径。</p>
    </template>
  </el-card>
</template>

<style scoped>
.date { font-size: 13px; font-weight: 600; color: var(--as-muted); margin-left: 6px; }
.err { margin-bottom: 8px; }
.strip { display: flex; gap: 12px; overflow-x: auto; padding-bottom: 6px; scroll-snap-type: x proximity; }
.col { flex: 0 0 310px; scroll-snap-align: start; border: 1px solid var(--el-border-color-light); border-radius: 8px; background: var(--el-bg-color); display: flex; flex-direction: column; min-width: 0; }
.col header { display: flex; align-items: center; gap: 8px; padding: 9px 12px; border-bottom: 1px solid var(--el-border-color-light); font-size: 14px; flex-wrap: wrap; }
.col header .cnt { margin-right: auto; color: var(--as-muted); font-size: 13px; }
.col header .hint { margin-right: auto; font-size: 11.5px; color: var(--as-muted); }
.body { max-height: 560px; overflow-y: auto; }
.lv { border-bottom: 1px solid var(--el-border-color-lighter); padding: 8px 10px; }
.lv-tag { font-weight: 700; font-size: 13px; margin-bottom: 6px; color: var(--as-rise); }
.lv-tag.first { padding: 8px 10px 0; color: var(--el-text-color-primary); }
.ind-group { padding: 6px 10px 8px; border-bottom: 1px solid var(--el-border-color-lighter); }
.ind-name { font-size: 12px; font-weight: 600; color: var(--as-muted); margin-bottom: 5px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { display: inline-flex; flex-direction: column; align-items: flex-start; gap: 0; min-width: 84px; padding: 3px 7px; border: 1px solid var(--el-border-color-light); border-radius: 6px; background: var(--el-fill-color-blank); font: inherit; color: inherit; cursor: pointer; line-height: 1.3; }
.chip:hover, .chip:focus-visible { border-color: var(--el-color-primary); outline: none; }
.chip .t { font-size: 10.5px; color: var(--as-muted); }
.chip .n { font-size: 12.5px; font-weight: 650; }
.chip .c { font-size: 10.5px; color: var(--as-muted); }
.pad { padding: 12px; }
.note { margin: 10px 0 0; }
</style>
