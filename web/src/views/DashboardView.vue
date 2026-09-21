<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { get } from '../api'
import { useDashboard } from '../composables/useDashboard'
import { useLoad } from '../composables/useLoad'
import { fmtDateTime, todayStr } from '../format'
import IndexStrip from '../components/dashboard/IndexStrip.vue'
import MarketGauge from '../components/dashboard/MarketGauge.vue'
import MarketPulse from '../components/dashboard/MarketPulse.vue'
import NextDayWatch from '../components/dashboard/NextDayWatch.vue'
import LimitBoards from '../components/dashboard/LimitBoards.vue'
import LhbBoard from '../components/dashboard/LhbBoard.vue'
import StockDetailDrawer from '../components/dashboard/StockDetailDrawer.vue'
import PostcloseSection from '../components/dashboard/PostcloseSection.vue'

// 看板只看市场数据：指数、市场评分、大盘脉搏（涨跌家数/成交额/资金）、次日关注、涨跌停、龙虎榜；
// 盘后分析是它的第二个 tab。候选池/虚拟账户/证据在「候选池」页。
// tab 写进 URL（?tab=postclose），刷新和旧的 /postclose 链接都能落回原处。
const route = useRoute()
const router = useRouter()
const TABS = ['market', 'postclose']
const tab = ref(TABS.includes(route.query.tab) ? route.query.tab : 'market')
watch(tab, (t) => router.replace({ query: t === 'market' ? {} : { tab: t } }))
watch(
  () => route.query.tab,
  (t) => {
    const next = TABS.includes(t) ? t : 'market'
    if (next !== tab.value) tab.value = next
  },
)

// 指数/成交额/资金/涨跌家数是实时取数（服务端 30 秒缓存），页面每 30 秒静默刷新一次；
// 涨跌停池与龙虎榜/次日关注走另一个接口，每分钟一次。
const { resp, d, agent, loading, error, refresh: refreshDashboard } = useDashboard({ intervalMs: 30000 })
const { data: reviewResp, loading: reviewLoading, error: reviewError, reload: reloadReview } = useLoad(() => get('/api/market/review'), { intervalMs: 60000 })
const review = computed(() => (reviewResp.value ? reviewResp.value.review : null))
function refresh() {
  refreshDashboard()
  reloadReview()
}

const SESSION = { weekend: '周末休市', pre_open: '盘前', call_auction: '集合竞价', morning: '上午盘中', lunch_break: '午间休市', afternoon: '下午盘中', closing: '收盘处理', post_close: '盘后' }
const live = computed(() => (d.value && d.value.live) || null)
const quoteTime = computed(() => {
  const q = live.value && live.value.indices && live.value.indices.quotes[0]
  return q ? fmtDateTime(q.quote_at).slice(5) : ''
})
const liveTag = computed(() => {
  if (!live.value) return { type: 'danger', text: '实时行情暂不可用，指数为日级快照（可能已过期）' }
  const session = SESSION[live.value.session] || live.value.session
  return { type: 'success', text: `${session}${quoteTime.value ? ' · 行情 ' + quoteTime.value : ''}` }
})
const failedParts = computed(() => {
  const e = (live.value && live.value.errors) || {}
  const NAME = { indices: '指数', turnover: '成交额', flow: '资金流向', breadth: '涨跌家数' }
  return Object.keys(e).map((k) => NAME[k] || k)
})
const screen = computed(() => (agent.value && agent.value.screen) || null)
const today = todayStr()
</script>

<template>
  <div>
    <div class="page-head">
      <h1>看板 <span class="eyebrow">A股市场数据 · 指数、大盘脉搏、次日关注、涨跌停、龙虎榜</span></h1>
      <div v-if="tab === 'market'" class="meta">
        <el-tag v-if="d" :type="liveTag.type" round>{{ liveTag.text }}</el-tag>
        <el-tag v-if="failedParts.length" type="warning" effect="plain" round>暂无：{{ failedParts.join('、') }}</el-tag>
        <span v-if="live">更新于 <span class="num">{{ fmtDateTime(live.fetched_at).slice(11) }}</span></span>
        <el-button :icon="Refresh" round :loading="loading || reviewLoading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="dash-tabs">
      <el-tab-pane label="市场行情" name="market">
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
          <el-button size="small" @click="refresh">重试</el-button>
        </el-alert>
        <el-alert v-if="reviewError" :title="`涨跌停/龙虎榜加载失败：${reviewError}`" type="error" show-icon :closable="false" style="margin-bottom: 16px">
          <el-button size="small" @click="reloadReview">重试</el-button>
        </el-alert>

        <div v-loading="loading && !resp" style="min-height: 240px">
          <el-card v-if="resp && !d" shadow="never">
            <h3 style="margin-top: 0">数据拉取失败</h3>
            <p>{{ resp.error || '未知错误' }}</p>
            <p class="muted">实时行情与日级快照都没有取到。点「刷新」会重新尝试一次。</p>
          </el-card>

          <div v-else-if="d" class="stack">
            <section class="market-strip">
              <IndexStrip :quotes="(d.market && d.market.quotes) || []" />
              <MarketGauge v-if="screen" :screen="screen" />
              <el-card v-else shadow="never" class="gauge-empty"><span class="muted">市场评分暂无：日级批处理的选股结果还没有生成。</span></el-card>
            </section>
            <p v-if="screen && screen.cutoff" class="muted score-note">市场评分取自日级批处理，截至 {{ screen.cutoff }} 收盘，不随盘中行情变化。</p>

            <MarketPulse :live="live" :pools="review && review.pools" :pool-date="review ? review.date : ''" />
            <NextDayWatch :watch="review && review.next_day_watch" />
            <LimitBoards :review="review" />
            <LhbBoard :lhb="review && review.lhb" :today-label="today" />
          </div>
        </div>
      </el-tab-pane>

      <!-- 懒加载：只有切到这个 tab 才挂载，盘后分析的轮询也就只在看它的时候才会开始 -->
      <el-tab-pane label="盘后分析" name="postclose" lazy>
        <PostcloseSection v-if="tab === 'postclose'" />
      </el-tab-pane>
    </el-tabs>

    <!-- 个股详情：涨停池 / 龙虎榜 / 次日关注 三处共用这一个抽屉 -->
    <StockDetailDrawer />
  </div>
</template>

<style scoped>
.eyebrow { display: block; font-size: 12px; font-weight: 600; color: var(--as-muted); letter-spacing: 0.04em; margin-top: 2px; }
.market-strip { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 14px; align-items: stretch; }
.gauge-empty { display: flex; align-items: center; }
.score-note { margin: -8px 0 0; }
.dash-tabs :deep(.el-tabs__header) { margin-bottom: 16px; }
.dash-tabs :deep(.el-tabs__item) { font-weight: 600; }
@media (max-width: 860px) { .market-strip { grid-template-columns: minmax(0, 1fr); } }
</style>
