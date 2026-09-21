<script setup>
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { get } from '../api'
import { useLoad } from '../composables/useLoad'

const route = useRoute()
const router = useRouter()
const day = ref(typeof route.query.day === 'string' ? route.query.day : '')

const { data, loading, error, reload } = useLoad(() => get('/api/sentinel' + (day.value ? `?day=${encodeURIComponent(day.value)}` : '')))

// 日期变化 → 写进 URL（可分享/刷新保留）并重新加载
watch(day, (d) => {
  router.replace({ query: d ? { day: d } : {} })
  reload()
})
watch(
  () => data.value && data.value.day,
  (d) => { if (d && !day.value) day.value = d },
)
</script>

<template>
  <div>
    <div class="page-head">
      <h1>哨兵</h1>
      <el-date-picker v-model="day" type="date" value-format="YYYY-MM-DD" format="YYYY-MM-DD" placeholder="选择日期" :clearable="false" aria-label="查看日期" />
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" @click="reload()">重试</el-button>
    </el-alert>

    <div v-loading="loading && !data" class="stack" style="min-height: 200px">
      <template v-if="data">
        <p class="muted" style="margin: 0">
          告警 {{ data.alerts.length }} 条 · 情景 {{ data.scenarios.length }} 条 · AI 调用 {{ data.ai_calls }} 次 · 待研判 {{ data.pending }} 条。系统只提醒、不下单；情景是研究参考，不构成投资建议。
        </p>

        <div class="card-title"><span>告警</span></div>
        <el-card v-if="!data.alerts.length" shadow="never"><span class="muted">这一天没有告警。</span></el-card>
        <el-card v-for="(a, i) in data.alerts" :key="i" shadow="never">
          <div class="alert-head">
            <span class="num muted">{{ a.time }}</span>
            <el-tag v-if="a.sent" type="success" size="small">已推送</el-tag>
            <el-tag v-else type="info" size="small">未推送：{{ a.reason }}</el-tag>
          </div>
          <pre class="alert-text">{{ a.text }}</pre>
        </el-card>

        <div class="card-title"><span>情景与对账</span></div>
        <el-card shadow="never">
          <el-table :data="data.scenarios" size="small" empty-text="这一天没有情景。">
            <el-table-column prop="time" label="时间" width="80"><template #default="{ row }"><span class="num">{{ row.time }}</span></template></el-table-column>
            <el-table-column label="股票" min-width="120">
              <template #default="{ row }"><span class="stock-name">{{ row.name }}</span><span class="stock-code num">{{ row.symbol }}</span></template>
            </el-table-column>
            <el-table-column label="情景" min-width="150"><template #default="{ row }">{{ row.direction === 'up' ? '↑' : '↓' }} {{ row.label }}</template></el-table-column>
            <el-table-column label="触发价" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.trigger_price.toFixed(2) }}</span></template></el-table-column>
            <el-table-column label="目标区间" width="130" align="right"><template #default="{ row }"><span class="num">{{ row.target_low.toFixed(2) }}–{{ row.target_high.toFixed(2) }}</span></template></el-table-column>
            <el-table-column label="失效价" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.invalidate_price.toFixed(2) }}</span></template></el-table-column>
            <el-table-column label="依据强度" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.confidence }}</span></template></el-table-column>
            <el-table-column prop="action_hint" label="倾向" width="100" />
            <el-table-column prop="outcome" label="收盘对账" min-width="110" />
          </el-table>
          <p v-if="data.scenarios.length" class="muted">依据强度是模型对判断依据的自评（1–5），<b>不是胜率</b>。对账只用情景发布之后的分钟收盘价；分钟内的瞬间触及看不到，“未触发”可能有漏判。情景命中率不等于交易盈利。</p>
        </el-card>

        <template v-if="data.charts.length">
          <div class="card-title"><span>告警时的分时图</span></div>
          <p class="muted" style="margin: 0">这是告警<b>当时</b>存下的图，不是现在的走势。只画事实：分时、均价线、均线、日内高低、你的成本与止损；不画预测线。</p>
          <!-- SVG 是我们自己生成并存盘的；服务端 _safe_svg 已拒绝任何带脚本/外链的内容，所以可以内联渲染 -->
          <el-card v-for="c in data.charts" :key="c.name" shadow="never">
            <div class="chart" v-html="c.svg" />
            <div class="muted">{{ c.name }}</div>
          </el-card>
        </template>

        <template v-if="data.weekly">
          <div class="card-title"><span>情景周报（截至该日的 7 天）</span></div>
          <el-card shadow="never"><pre class="alert-text">{{ data.weekly }}</pre></el-card>
        </template>
      </template>
    </div>
  </div>
</template>

<style scoped>
.alert-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.alert-text { white-space: pre-wrap; word-break: break-word; font: inherit; margin: 0; }
.chart :deep(svg) { max-width: 100%; height: auto; }
</style>
