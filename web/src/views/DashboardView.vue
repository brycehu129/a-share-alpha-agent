<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { get } from '../api'
import { useDashboard } from '../composables/useDashboard'
import { useLoad } from '../composables/useLoad'
import { fmtTs, todayStr } from '../format'
import IndexStrip from '../components/dashboard/IndexStrip.vue'
import MarketGauge from '../components/dashboard/MarketGauge.vue'
import MarketPulse from '../components/dashboard/MarketPulse.vue'
import MarketRankings from '../components/dashboard/MarketRankings.vue'
import ResilienceScan from '../components/dashboard/ResilienceScan.vue'
import NextDayWatch from '../components/dashboard/NextDayWatch.vue'
import LimitBoards from '../components/dashboard/LimitBoards.vue'
import LhbBoard from '../components/dashboard/LhbBoard.vue'
import StockDetailDrawer from '../components/dashboard/StockDetailDrawer.vue'
import PostcloseSection from '../components/dashboard/PostcloseSection.vue'

// 看板只看市场数据：市场行情、次日关注、涨跌停复盘、龙虎榜、盘后分析五个 tab。候选池/虚拟账户/证据在「候选池」页。
// tab 写进 URL（?tab=postclose），刷新和旧的 /postclose 链接都能落回原处。
const route = useRoute()
const router = useRouter()
const TABS = ['market', 'nextday', 'limit', 'lhb', 'postclose']
const REFRESHABLE = ['market', 'nextday', 'limit', 'lhb'] // 盘后分析有自己的刷新逻辑
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
// 收盘复盘名单与龙虎榜/次日关注走另一个接口；名单只读落盘文件，接口每分钟刷新一次当前行情。
const { resp, d, agent, loading, error, refresh: refreshDashboard } = useDashboard({ intervalMs: 30000 })
const { data: reviewResp, loading: reviewLoading, error: reviewError, reload: reloadReview } = useLoad(() => get('/api/market/review'), { intervalMs: 60000 })
const review = computed(() => (reviewResp.value ? reviewResp.value.review : null))
const pulsePools = ref(null)
const pulsePoolsLoading = ref(false)
const pulsePoolsError = ref('')
const rankings = ref(null)
const rankingsLoading = ref(false)
const rankingsError = ref('')
const rankingsStale = ref(false)
const pulsePoolMessage = computed(() => {
  if (pulsePoolsError.value) return pulsePoolsError.value
  const errors = (pulsePools.value && pulsePools.value.errors) || {}
  return errors.all || Object.entries(errors).map(([k, v]) => `${k}：${v}`).join('；')
})
async function reloadPulsePools() {
  pulsePoolsLoading.value = true
  try {
    const resp = await get('/api/market/pulse-pools')
    pulsePools.value = resp.pulse_pools
    pulsePoolsError.value = ''
  } catch (e) {
    pulsePoolsError.value = e.message || String(e)
  } finally {
    pulsePoolsLoading.value = false
  }
}
async function reloadRankings() {
  rankingsLoading.value = true
  try {
    const resp = await get('/api/market/rankings')
    rankings.value = resp.rankings
    rankingsError.value = ''
    rankingsStale.value = false
  } catch (e) {
    rankingsError.value = e.message || String(e)
    rankingsStale.value = !!rankings.value
  } finally {
    rankingsLoading.value = false
  }
}
// 抗跌扫描：默认读留档（不打网络），只有点「重新扫描」才现扫一次。
// 阈值只是显示过滤器，改了就带参数重取，让时间线按新阈值重算。
const resilience = ref(null)
const resilienceTimeline = ref([])
const resilienceLoading = ref(false)
const resilienceError = ref('')
const resilienceThresholds = ref({})
async function reloadResilience(refreshNow = false) {
  resilienceLoading.value = true
  try {
    const params = new URLSearchParams(resilienceThresholds.value)
    if (refreshNow) params.set('refresh', '1')
    const qs = params.toString()
    const resp = await get(`/api/market/resilience${qs ? '?' + qs : ''}`)
    resilience.value = resp.scan
    resilienceTimeline.value = resp.timeline || []
    resilienceError.value = ''
  } catch (e) {
    resilienceError.value = e.message || String(e)
  } finally {
    resilienceLoading.value = false
  }
}
function onResilienceThresholds(next) {
  resilienceThresholds.value = next
  reloadResilience(false)
}

function reloadMarketExtras() {
  reloadPulsePools()
  reloadRankings()
  reloadResilience(false)
}
onMounted(() => { if (tab.value === 'market') reloadMarketExtras() })
watch(tab, (next, prev) => { if (next === 'market' && prev !== 'market') reloadMarketExtras() })
function refresh() {
  refreshDashboard()
  reloadReview()
  if (tab.value === 'market') reloadMarketExtras()
}

const SESSION = { weekend: '周末休市', pre_open: '盘前', call_auction: '集合竞价', morning: '上午盘中', lunch_break: '午间休市', afternoon: '下午盘中', closing: '收盘处理', post_close: '盘后' }
const live = computed(() => (d.value && d.value.live) || null)
const quoteTime = computed(() => {
  const q = live.value && live.value.indices && live.value.indices.quotes[0]
  return q ? fmtTs(q.quote_at) : ''
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
      <h1>看板 <span class="eyebrow">A股市场数据 · 行情、次日关注、涨跌停复盘、龙虎榜、盘后分析</span></h1>
      <div v-if="REFRESHABLE.includes(tab)" class="meta">
        <el-tag v-if="d && tab === 'market'" :type="liveTag.type" round>{{ liveTag.text }}</el-tag>
        <el-tag v-if="failedParts.length && tab === 'market'" type="warning" effect="plain" round>暂无：{{ failedParts.join('、') }}</el-tag>
        <span v-if="live">页面数据更新于 <span class="num">{{ fmtTs(live.fetched_at) }}</span></span>
        <el-button :icon="Refresh" round :loading="loading || reviewLoading || pulsePoolsLoading || rankingsLoading || resilienceLoading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="dash-tabs">
      <el-tab-pane label="市场行情" name="market">
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
          <el-button size="small" @click="refresh">重试</el-button>
        </el-alert>
        <el-alert v-if="pulsePoolMessage" :title="`盘中涨跌停统计加载失败：${pulsePoolMessage}`" type="error" show-icon :closable="false" style="margin-bottom: 16px">
          <el-button size="small" @click="reloadPulsePools">重试</el-button>
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
            <p v-if="screen && screen.cutoff" class="muted score-note">市场评分取自日级批处理（截至 {{ screen.cutoff }} 收盘，生成于 <span class="num">{{ fmtTs(agent && agent.generated_at) }}</span>），不随盘中行情变化。</p>

            <MarketPulse
              v-loading="pulsePoolsLoading"
              :live="live"
              :pools="pulsePools && pulsePools.pools"
              :pool-date="pulsePools ? pulsePools.date : ''"
              :pool-time="pulsePools ? pulsePools.fetched_at : ''"
              :pool-error="pulsePoolMessage"
            />
            <MarketRankings :data="rankings" :loading="rankingsLoading" :stale="rankingsStale" :error="rankingsError" />
            <ResilienceScan
              :scan="resilience"
              :timeline="resilienceTimeline"
              :loading="resilienceLoading"
              :error="resilienceError"
              @rescan="reloadResilience(true)"
              @thresholds="onResilienceThresholds"
            />
          </div>
        </div>
      </el-tab-pane>

      <!-- 次日关注、涨跌停复盘、龙虎榜各自一个 tab；共用同一份 /api/market/review，切换时不重复请求 -->
      <el-tab-pane label="次日关注" name="nextday" lazy>
        <el-alert v-if="reviewError" :title="`次日关注加载失败：${reviewError}`" type="error" show-icon :closable="false" style="margin-bottom: 16px">
          <el-button size="small" @click="reloadReview">重试</el-button>
        </el-alert>
        <div v-loading="reviewLoading && !reviewResp" style="min-height: 240px">
          <NextDayWatch v-if="tab === 'nextday'" :watch="review && review.next_day_watch" :prev-watch="review && review.prev_watch" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="涨跌停复盘" name="limit" lazy>
        <el-alert v-if="reviewError" :title="`涨跌停复盘加载失败：${reviewError}`" type="error" show-icon :closable="false" style="margin-bottom: 16px">
          <el-button size="small" @click="reloadReview">重试</el-button>
        </el-alert>
        <div v-loading="reviewLoading && !reviewResp" style="min-height: 240px">
          <LimitBoards v-if="tab === 'limit'" :review="review" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="龙虎榜" name="lhb" lazy>
        <el-alert v-if="reviewError" :title="`龙虎榜加载失败：${reviewError}`" type="error" show-icon :closable="false" style="margin-bottom: 16px">
          <el-button size="small" @click="reloadReview">重试</el-button>
        </el-alert>
        <div v-loading="reviewLoading && !reviewResp" style="min-height: 240px">
          <LhbBoard v-if="tab === 'lhb'" :lhb="review && review.lhb" :today-label="today" />
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
