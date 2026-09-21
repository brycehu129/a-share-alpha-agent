<script setup>
import { computed, watch } from 'vue'
import { get, post } from '../../api'
import { useLoad } from '../../composables/useLoad'
import { useAction } from '../../composables/useAction'

// 看板的「盘后分析」tab。只在切到这个 tab 时才挂载（DashboardView 里用 v-if），所以只有看它的时候才会轮询。
// 生成一次要 1–3 分钟（AI 调用是大头）：后端放后台线程跑，这里只在「运行中」时每 15 秒轮询一次。
const { data, loading, error, reload, start, stop } = useLoad(() => get('/api/postclose'), { intervalMs: 15000 })
watch(
  () => data.value && data.value.state.running,
  (running) => (running ? start() : stop()),
)

const run = useAction()
const state = computed(() => (data.value ? data.value.state : null))
const startedAt = computed(() => (state.value && state.value.started_at ? state.value.started_at.slice(11, 19) : ''))

async function generate(noPush) {
  const r = await run.run(() => post('/api/postclose/run', { no_push: noPush }))
  if (r) {
    await reload({ silent: true })
    start()
  }
}
</script>

<template>
  <div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" @click="reload()">重试</el-button>
    </el-alert>

    <div v-loading="loading && !data" class="stack" style="min-height: 200px">
      <template v-if="data">
        <el-card shadow="never">
          <div class="status-row">
            <el-tag v-if="state.running" type="warning" round>正在生成…</el-tag>
            <el-tag v-else-if="state.error" type="danger" round>上次生成有问题</el-tag>
            <el-tag v-else type="success" round>空闲</el-tag>
            <span v-if="state.running" class="muted">开始于 {{ startedAt }}，本页每 15 秒自动刷新。</span>
            <span v-else-if="state.error" class="muted">{{ state.error }}</span>
          </div>
          <el-alert v-if="run.error.value" :title="run.error.value" type="error" show-icon :closable="false" style="margin-top: 12px" />
          <div class="actions">
            <el-button type="primary" :loading="run.loading.value" :disabled="state.running" @click="generate(false)">立即生成</el-button>
            <el-button :disabled="state.running || run.loading.value" @click="generate(true)">生成但不推送</el-button>
          </div>
          <p class="muted">定时任务：工作日 16:30（北京时间）自动生成并推送到企业微信。报告含你的真实持仓，只保存在这台服务器上，不会进入公开的看板数据。</p>
        </el-card>

        <el-card v-if="!data.report" shadow="never">
          <span class="muted">还没有生成过盘后分析。点上面的按钮生成一份，或者等工作日 16:30 的定时任务。</span>
        </el-card>
        <template v-else>
          <!-- 报告 HTML 由服务端 markdown_to_html 生成：先整体转义再套格式，所以这里可以安全地用 v-html -->
          <el-card shadow="never"><div class="report" v-html="data.report.html" /></el-card>
          <p class="muted">报告编号 {{ data.report.id }} {{ data.report.ai }}</p>
        </template>
      </template>
    </div>
  </div>
</template>

<style scoped>
.status-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.report { line-height: 1.7; overflow-x: auto; }
.report :deep(h1) { font-size: 20px; margin: 4px 0 10px; }
.report :deep(h2) { font-size: 17px; margin: 22px 0 8px; padding-bottom: 6px; border-bottom: 1px solid var(--el-border-color-light); }
.report :deep(h3) { font-size: 15px; margin: 18px 0 6px; }
.report :deep(h4) { font-size: 14px; margin: 14px 0 4px; }
.report :deep(p) { margin: 6px 0; }
.report :deep(table) { width: 100%; border-collapse: collapse; font-size: 13px; margin: 10px 0; }
.report :deep(th), .report :deep(td) { padding: 7px 8px; border-bottom: 1px solid var(--el-border-color-lighter); text-align: left; vertical-align: top; }
.report :deep(th) { background: var(--el-fill-color-light); font-size: 12px; color: var(--el-text-color-regular); }
.report :deep(td.num), .report :deep(th.num) { text-align: right; font-variant-numeric: tabular-nums; }
.report :deep(blockquote) { margin: 8px 0; padding: 8px 12px; background: var(--el-fill-color-light); border-radius: 6px; font-size: 13px; color: var(--el-text-color-regular); }
</style>
