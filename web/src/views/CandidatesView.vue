<script setup>
import { computed, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { useDashboard } from '../composables/useDashboard'
import { fmtDateTime } from '../format'
import CandidatesTable from '../components/dashboard/CandidatesTable.vue'
import AccountPanel from '../components/dashboard/AccountPanel.vue'
import DecisionsTable from '../components/dashboard/DecisionsTable.vue'
import Exec02Panel from '../components/dashboard/Exec02Panel.vue'
import EvidencePanel from '../components/dashboard/EvidencePanel.vue'
import BaselinePanel from '../components/dashboard/BaselinePanel.vue'
import SourceFooter from '../components/dashboard/SourceFooter.vue'

// 候选池：规则化选股的候选股票、虚拟账户（exec-0.1 / exec-0.2）、预测与待执行计划、证据与随机基线。
// 数据和看板是同一份 /api/dashboard（服务端 30 秒缓存）。
const { resp, d, agent, loading, error, refresh } = useDashboard({ live: false })
const holdSessions = computed(() => (agent.value && agent.value.policy && agent.value.policy.hold_sessions) || 3)
const statusTag = computed(() => (agent.value && agent.value.status === 'ready' ? { type: 'success', text: '数据完整（ready）' } : { type: 'warning', text: '部分完整（partial）' }))
const openRules = ref([])
</script>

<template>
  <div>
    <div class="page-head">
      <h1>候选池 <span class="eyebrow">规则化选股 · 虚拟账户研究实验</span></h1>
      <div class="meta">
        <el-tag v-if="agent" :type="statusTag.type" round>状态：{{ statusTag.text }}</el-tag>
        <span v-if="d">数据时间 <span class="num">{{ fmtDateTime(d.exported_at) }}</span>（北京时间）</span>
        <span v-if="agent">版本 <span class="num">{{ agent.version }}</span></span>
        <el-button :icon="Refresh" round :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" @click="refresh">重试</el-button>
    </el-alert>

    <div v-loading="loading && !resp" style="min-height: 240px">
      <el-card v-if="resp && !agent" shadow="never">
        <h3 style="margin-top: 0">候选池数据暂无</h3>
        <p>{{ resp.error || '日级批处理还没有生成选股结果。' }}</p>
        <p class="muted">候选池来自日级批处理导出的 <code>dashboard/latest.json</code>。点「刷新」重新拉取；行情类数据在「看板」页，不受影响。</p>
      </el-card>

      <div v-else-if="agent" class="stack">
        <!-- 验证口径：默认折叠，不占首屏 -->
        <el-collapse v-model="openRules" class="rules">
          <el-collapse-item title="验证口径与免责声明" name="rules">
            <p class="rules-text">{{ agent.target }}</p>
          </el-collapse-item>
        </el-collapse>

        <CandidatesTable :candidates="agent.candidates || []" :screen="agent.screen || {}" :hold-sessions="holdSessions" />
        <AccountPanel :portfolio="agent.portfolio" />
        <DecisionsTable :decisions="agent.decisions" />
        <Exec02Panel :account="agent.exec02" />
        <EvidencePanel :evidence="agent.evidence" />
        <BaselinePanel :baseline="agent.baseline" />
        <SourceFooter :data="d" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.eyebrow { display: block; font-size: 12px; font-weight: 600; color: var(--as-muted); letter-spacing: 0.04em; margin-top: 2px; }
.rules { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 0 14px; background: var(--el-bg-color); }
.rules :deep(.el-collapse-item__header) { height: 40px; font-size: 13px; font-weight: 600; background: transparent; }
.rules :deep(.el-collapse-item__wrap) { background: transparent; }
.rules-text { margin: 0; line-height: 1.7; color: var(--el-text-color-regular); }
</style>
