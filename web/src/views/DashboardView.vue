<script setup>
import { computed, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { get } from '../api'
import { useLoad } from '../composables/useLoad'
import { fmtDateTime } from '../format'
import IndexStrip from '../components/dashboard/IndexStrip.vue'
import MarketGauge from '../components/dashboard/MarketGauge.vue'
import CandidatesTable from '../components/dashboard/CandidatesTable.vue'
import AccountPanel from '../components/dashboard/AccountPanel.vue'
import DecisionsTable from '../components/dashboard/DecisionsTable.vue'
import Exec02Panel from '../components/dashboard/Exec02Panel.vue'
import EvidencePanel from '../components/dashboard/EvidencePanel.vue'
import BaselinePanel from '../components/dashboard/BaselinePanel.vue'
import HotmoneyBoard from '../components/dashboard/HotmoneyBoard.vue'
import SourceFooter from '../components/dashboard/SourceFooter.vue'

// 服务器每次请求时从 GitHub market-data 分支拉 dashboard/latest.json（30 秒缓存）；
// 「刷新」按钮带 refresh=1 绕过缓存强制重拉。
let force = false
const { data: resp, loading, error, reload } = useLoad(() => {
  const q = force ? '?refresh=1' : ''
  force = false
  return get('/api/dashboard' + q)
})
function refresh() {
  force = true
  reload()
}

const d = computed(() => (resp.value ? resp.value.data : null))
const agent = computed(() => (d.value ? d.value.agent : null))
const holdSessions = computed(() => (agent.value && agent.value.policy && agent.value.policy.hold_sessions) || 3)

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
const openRules = ref([])
</script>

<template>
  <div>
    <div class="page-head">
      <h1>Alpha Shadow 看板 <span class="eyebrow">A股规则化选股 · 虚拟账户研究实验</span></h1>
      <div class="meta">
        <el-tag v-if="agent" :type="statusTag.type" round>状态：{{ statusTag.text }}</el-tag>
        <el-tag v-if="sourceTag" :type="sourceTag.type" effect="plain" round>{{ sourceTag.text }}</el-tag>
        <span v-if="d">数据时间 <span class="num">{{ fmtDateTime(d.exported_at) }}</span>（北京时间）</span>
        <span v-if="agent">版本 <span class="num">{{ agent.version }}</span></span>
        <el-button :icon="Refresh" round :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

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
        <!-- 验证口径：默认折叠，不占首屏 -->
        <el-collapse v-model="openRules" class="rules">
          <el-collapse-item title="验证口径与免责声明" name="rules">
            <p class="rules-text">{{ agent.target }}</p>
          </el-collapse-item>
        </el-collapse>

        <section class="market-strip">
          <IndexStrip :quotes="d.market.quotes" />
          <MarketGauge :screen="agent.screen" />
        </section>

        <CandidatesTable :candidates="agent.candidates" :screen="agent.screen" :hold-sessions="holdSessions" />
        <AccountPanel :portfolio="agent.portfolio" />
        <DecisionsTable :decisions="agent.decisions" />
        <Exec02Panel :account="agent.exec02" />
        <EvidencePanel :evidence="agent.evidence" />
        <BaselinePanel :baseline="agent.baseline" />
        <HotmoneyBoard :board="d.hotmoney_board" />
        <SourceFooter :data="d" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.eyebrow { display: block; font-size: 12px; font-weight: 600; color: var(--as-muted); letter-spacing: 0.04em; margin-top: 2px; }
.market-strip { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 14px; align-items: stretch; }
.rules { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 0 14px; background: var(--el-bg-color); }
.rules :deep(.el-collapse-item__header) { height: 40px; font-size: 13px; font-weight: 600; background: transparent; }
.rules :deep(.el-collapse-item__wrap) { background: transparent; }
.rules-text { margin: 0; line-height: 1.7; color: var(--el-text-color-regular); }
@media (max-width: 860px) { .market-strip { grid-template-columns: minmax(0, 1fr); } }
</style>
