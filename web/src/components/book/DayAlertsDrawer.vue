<script setup>
import { computed, ref, watch } from 'vue'
import { get } from '../../api'
import CollectionLine from './CollectionLine.vue'

// 「全天告警汇总」抽屉：某一天所有股票的告警、情景与收盘对账、情景周报、采集健康。
// 哨兵不再是独立页面；单只股票的详情在持仓/自选每一行的「告警」按钮里。
const props = defineProps({ modelValue: { type: Boolean, default: false } })
const emit = defineEmits(['update:modelValue'])
const open = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })

const day = ref('') // 空 = 今天
const data = ref(null)
const loading = ref(false)
const error = ref('')
let seq = 0

async function load() {
  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    const r = await get('/api/sentinel' + (day.value ? `?day=${encodeURIComponent(day.value)}` : ''))
    if (mine === seq) data.value = r
  } catch (e) {
    if (mine === seq) error.value = e.message || String(e)
  } finally {
    if (mine === seq) loading.value = false
  }
}

watch(
  () => props.modelValue,
  (isOpen) => {
    if (isOpen) load()
  },
)
watch(day, () => {
  if (props.modelValue) load()
})
const pickerDay = computed({
  get: () => day.value || (data.value && data.value.day) || '',
  set: (v) => {
    day.value = v
  },
})
</script>

<template>
  <el-drawer v-model="open" title="全天告警汇总" size="min(860px, 100vw)" direction="rtl" destroy-on-close>
    <div v-loading="loading && !data" class="body" style="min-height: 240px">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>

      <template v-if="data">
        <div class="head-row">
          <el-date-picker v-model="pickerDay" type="date" value-format="YYYY-MM-DD" format="YYYY-MM-DD" :clearable="false" size="small" aria-label="查看日期" />
          <el-button size="small" :loading="loading" @click="load">刷新</el-button>
        </div>
        <p class="muted" style="margin: 0">
          告警 {{ data.alerts.length }} 条 · 情景 {{ data.scenarios.length }} 条（每只股票每次研判 2–3 条，不是推送条数）· AI 调用 {{ data.ai_calls }} 次 · 待研判 {{ data.pending }} 条。系统只提醒、不下单；情景是研究参考，不构成投资建议。
        </p>

        <CollectionLine :collection="data.collection" />

        <h3>判断总表</h3>
        <el-card shadow="never">
          <el-table :data="data.judgments || []" size="small" empty-text="这一天没有形成可留档的判断。">
            <el-table-column prop="time" label="时间" width="80"><template #default="{ row }"><span class="num">{{ row.time }}</span></template></el-table-column>
            <el-table-column label="股票" min-width="120">
              <template #default="{ row }"><span class="stock-name">{{ row.name }}</span><span class="stock-code num">{{ row.symbol }}</span></template>
            </el-table-column>
            <el-table-column prop="source" label="来源" min-width="120" />
            <el-table-column prop="action_hint" label="判断" width="96" />
            <el-table-column prop="glance" label="速判" min-width="220" />
            <el-table-column prop="outcome" label="当日结果" min-width="150" />
          </el-table>
          <p v-if="data.judgments && data.judgments.length" class="muted">一行代表同一次研判；同一时刻同一股票的多条情景会合并展示，下面的“情景与对账”仍保留逐条明细。</p>
        </el-card>

        <h3>告警</h3>
        <el-card v-if="!data.alerts.length" shadow="never"><span class="muted">这一天没有告警。</span></el-card>
        <el-card v-for="(a, i) in data.alerts" :key="i" shadow="never">
          <div class="alert-head">
            <span class="num muted">{{ a.time }}</span>
            <el-tag v-if="a.sent" type="success" size="small">已推送</el-tag>
            <el-tag v-else type="info" size="small">未推送：{{ a.reason }}</el-tag>
          </div>
          <pre class="alert-text">{{ a.text }}</pre>
        </el-card>

        <h3>情景与对账</h3>
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

        <template v-if="data.weekly">
          <h3>情景周报（截至该日的 7 天）</h3>
          <el-card shadow="never"><pre class="alert-text">{{ data.weekly }}</pre></el-card>
        </template>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.body { display: flex; flex-direction: column; gap: 12px; }
.head-row { display: flex; align-items: center; gap: 8px; }
h3 { margin: 6px 0 0; font-size: 15px; }
.alert-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.alert-text { white-space: pre-wrap; word-break: break-word; font: inherit; margin: 0; }
</style>
