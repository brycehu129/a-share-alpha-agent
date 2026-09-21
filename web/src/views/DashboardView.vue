<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { useDashboard } from '../composables/useDashboard'
import { fmtDateTime } from '../format'
import IndexStrip from '../components/dashboard/IndexStrip.vue'
import MarketGauge from '../components/dashboard/MarketGauge.vue'
import BreadthPanel from '../components/dashboard/BreadthPanel.vue'
import HotmoneyBoard from '../components/dashboard/HotmoneyBoard.vue'
import PostcloseSection from '../components/dashboard/PostcloseSection.vue'

// 看板只看市场数据：指数、市场评分、涨跌家数、涨跌停、龙虎榜；盘后分析是它的第二个 tab。
// 候选池/虚拟账户/证据在「候选池」页。tab 写进 URL（?tab=postclose），刷新和旧的 /postclose 链接都能落回原处。
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

const { resp, d, agent, loading, error, refresh } = useDashboard()

const statusTag = computed(() => (agent.value && agent.value.status === 'ready' ? { type: 'success', text: '数据完整（ready）' } : { type: 'warning', text: '部分完整（partial）' }))
const sourceTag = computed(() => {
  const r = resp.value
  if (!r) return null
  if (!r.data) return { type: 'danger', text: `拉取失败：${r.error || '未知错误'}` }
  const at = fmtDateTime(r.fetched_at)
  return r.stale
    ? { type: 'warning', text: `缓存数据（本次拉取GitHub失败，展示上一次成功结果）· ${at}` }
    : { type: 'info', text: `服务器实时拉取 · ${at}` }
})
const screen = computed(() => (agent.value && agent.value.screen) || null)
const universe = computed(() => (d.value && d.value.market && d.value.market.universe) || null)
</script>

<template>
  <div>
    <div class="page-head">
      <h1>看板 <span class="eyebrow">A股市场数据 · 指数、评分、涨跌家数、龙虎榜</span></h1>
      <div v-if="tab === 'market'" class="meta">
        <el-tag v-if="agent" :type="statusTag.type" round>状态：{{ statusTag.text }}</el-tag>
        <el-tag v-if="sourceTag" :type="sourceTag.type" effect="plain" round>{{ sourceTag.text }}</el-tag>
        <span v-if="d">数据时间 <span class="num">{{ fmtDateTime(d.exported_at) }}</span>（北京时间）</span>
        <el-button :icon="Refresh" round :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="dash-tabs">
      <el-tab-pane label="市场行情" name="market">
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
          <el-button size="small" @click="refresh">重试</el-button>
        </el-alert>

        <div v-loading="loading && !resp" style="min-height: 240px">
          <el-card v-if="resp && !d" shadow="never">
            <h3 style="margin-top: 0">数据拉取失败</h3>
            <p>{{ resp.error || '未知错误' }}</p>
            <p class="muted">服务器直接从 GitHub <code>market-data</code> 分支拉取 <code>dashboard/latest.json</code>，拉取失败通常是网络问题，或者日级批处理还没生成过这个文件。点「刷新」会重新尝试一次。</p>
          </el-card>

          <div v-else-if="d" class="stack">
            <section class="market-strip">
              <IndexStrip :quotes="(d.market && d.market.quotes) || []" />
              <MarketGauge v-if="screen" :screen="screen" />
              <el-card v-else shadow="never" class="gauge-empty"><span class="muted">市场评分暂无：日级批处理的选股结果还没有生成。</span></el-card>
            </section>

            <BreadthPanel :universe="universe" :limit-counts="d.limit_counts || null" :limit-approx="d.limit_approx || null" />
            <HotmoneyBoard :board="d.hotmoney_board || null" />
          </div>
        </div>
      </el-tab-pane>

      <!-- 懒加载：只有切到这个 tab 才挂载，盘后分析的轮询也就只在看它的时候才会开始 -->
      <el-tab-pane label="盘后分析" name="postclose" lazy>
        <PostcloseSection v-if="tab === 'postclose'" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.eyebrow { display: block; font-size: 12px; font-weight: 600; color: var(--as-muted); letter-spacing: 0.04em; margin-top: 2px; }
.market-strip { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 14px; align-items: stretch; }
.gauge-empty { display: flex; align-items: center; }
.dash-tabs :deep(.el-tabs__header) { margin-bottom: 16px; }
.dash-tabs :deep(.el-tabs__item) { font-weight: 600; }
@media (max-width: 860px) { .market-strip { grid-template-columns: minmax(0, 1fr); } }
</style>
