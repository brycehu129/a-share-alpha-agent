<script setup>
import { computed, ref, watch } from 'vue'
import { get } from '../../api'
import { direction, fmtFlow, fmtNum, flowLine } from '../../format'
import CollectionLine from './CollectionLine.vue'
import FlowFacts from './FlowFacts.vue'

// 持仓/自选某一行的「告警」抽屉：此刻的资金流 + 今日分时资金流表 + 采集次数 + 今日告警（含告警当时的资金流快照）+ 今日情景。
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  symbol: { type: String, default: '' },
  name: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])
const open = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })

const day = ref('') // 空 = 今天
const data = ref(null)
const loading = ref(false)
const error = ref('')
let seq = 0

async function load() {
  if (!props.symbol) return
  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    const q = `symbol=${encodeURIComponent(props.symbol)}` + (day.value ? `&day=${encodeURIComponent(day.value)}` : '')
    const r = await get('/api/sentinel/symbol?' + q)
    if (mine === seq) data.value = r
  } catch (e) {
    if (mine === seq) error.value = e.message || String(e)
  } finally {
    if (mine === seq) loading.value = false
  }
}

// 每次打开都重新取（资金流是实时的）；换股票时清掉上一只的内容，避免闪出别人的数据。
watch(
  () => [props.modelValue, props.symbol],
  ([isOpen], [wasOpen, oldSymbol]) => {
    if (!isOpen) return
    if (!wasOpen || props.symbol !== oldSymbol) {
      data.value = null
      if (day.value) {
        day.value = '' // 上面 watch(day) 会负责重新加载，这里不再重复请求
        return
      }
    }
    load()
  },
)
watch(day, () => {
  if (props.modelValue) load()
})
// 日期选择器显示实际查看的那一天；只有用户自己选了日期才写进 day（否则会因为回填而多请求一次）。
const pickerDay = computed({
  get: () => day.value || (data.value && data.value.day) || '',
  set: (v) => {
    day.value = v
  },
})

const isToday = computed(() => !!data.value && data.value.flow_now !== null)
const tone = (v) => direction(v, 0)

function snapshot(a) {
  const parts = []
  if (!a.text.includes('资金｜')) {
    const line = flowLine(a.flow, a.book)
    if (line) parts.push(line)
  }
  const f = a.flow
  if (f && f.xlarge !== undefined) parts.push(`超大单 ${fmtFlow(f.xlarge)}｜中单 ${fmtFlow(f.mid)}｜小单 ${fmtFlow(f.small)}`)
  if (f && f.flip) parts.push(`${f.flip.at.slice(0, 2)}:${f.flip.at.slice(2)} 起由${f.flip.from}转${f.flip.to}`)
  return parts.join('｜')
}
</script>

<template>
  <el-drawer v-model="open" :title="`${name || symbol}  ${symbol}`" size="min(600px, 100vw)" direction="rtl" destroy-on-close>
    <div v-loading="loading && !data" class="body" style="min-height: 240px">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>

      <template v-if="data">
        <div class="head-row">
          <el-date-picker v-model="pickerDay" type="date" value-format="YYYY-MM-DD" format="YYYY-MM-DD" :clearable="false" size="small" aria-label="查看日期" />
          <el-button size="small" :loading="loading" @click="load">刷新</el-button>
        </div>

        <CollectionLine :collection="data.collection" symbol />

        <h3>资金流与盘口 <span class="sub">{{ isToday ? '（此刻）' : '' }}</span></h3>
        <el-alert v-if="data.flow_error" :title="`资金流暂不可用：${data.flow_error}`" type="warning" show-icon :closable="false" />
        <FlowFacts :flow="data.flow_now" :book="data.book_now" />
        <p class="muted">{{ data.flow_source_note }}外盘占比、委比来自腾讯行情。这些是事实，不是买卖建议。</p>

        <template v-if="data.flow_table && data.flow_table.length">
          <h3>分时资金流 <span class="sub">累计净流入，每 30 分钟一行</span></h3>
          <el-table :data="data.flow_table" size="small">
            <el-table-column prop="t" label="时间" width="66" />
            <el-table-column label="主力" align="right"><template #default="{ row }"><span class="num" :class="tone(row.main)">{{ fmtFlow(row.main) }}</span></template></el-table-column>
            <el-table-column label="超大单" align="right"><template #default="{ row }"><span class="num" :class="tone(row.xlarge)">{{ fmtFlow(row.xlarge) }}</span></template></el-table-column>
            <el-table-column label="大单" align="right"><template #default="{ row }"><span class="num" :class="tone(row.large)">{{ fmtFlow(row.large) }}</span></template></el-table-column>
            <el-table-column label="中单" align="right"><template #default="{ row }"><span class="num" :class="tone(row.mid)">{{ fmtFlow(row.mid) }}</span></template></el-table-column>
            <el-table-column label="小单" align="right"><template #default="{ row }"><span class="num" :class="tone(row.small)">{{ fmtFlow(row.small) }}</span></template></el-table-column>
            <el-table-column label="外盘占比" width="86" align="right"><template #default="{ row }"><span class="num">{{ row.outer_pct === null || row.outer_pct === undefined ? '—' : fmtNum(row.outer_pct, 1) + '%' }}</span></template></el-table-column>
          </el-table>
        </template>

        <h3>今日告警（{{ data.alerts.length }}）</h3>
        <el-card v-if="!data.alerts.length" shadow="never"><span class="muted">这一天这只股票没有触发告警。</span></el-card>
        <el-card v-for="(a, i) in data.alerts" :key="i" shadow="never" class="alert">
          <div class="alert-head">
            <span class="num muted">{{ a.time }}</span>
            <el-tag v-if="a.urgent" type="danger" size="small">紧急</el-tag>
            <el-tag v-if="a.sent" type="success" size="small">已推送</el-tag>
            <el-tag v-else type="info" size="small">未推送：{{ a.reason }}</el-tag>
          </div>
          <pre class="alert-text">{{ a.text }}</pre>
          <!-- 原文里已经有「资金｜主力…」一行；这里补的是原文没有的分档明细，以及没有那一行的旧记录 -->
          <p v-if="snapshot(a)" class="snapshot"><span class="muted">告警当时：</span>{{ snapshot(a) }}</p>
        </el-card>

        <h3>今日情景与收盘对账（{{ data.scenarios.length }}）</h3>
        <el-card v-if="!data.scenarios.length" shadow="never"><span class="muted">这一天这只股票没有情景研判。</span></el-card>
        <el-card v-else shadow="never">
          <el-table :data="data.scenarios" size="small">
            <el-table-column prop="time" label="时间" width="76"><template #default="{ row }"><span class="num">{{ row.time }}</span></template></el-table-column>
            <el-table-column label="情景" min-width="120"><template #default="{ row }">{{ row.direction === 'up' ? '↑' : '↓' }} {{ row.label }}</template></el-table-column>
            <el-table-column label="触发价" width="76" align="right"><template #default="{ row }"><span class="num">{{ row.trigger_price.toFixed(2) }}</span></template></el-table-column>
            <el-table-column label="目标区间" width="112" align="right"><template #default="{ row }"><span class="num">{{ row.target_low.toFixed(2) }}–{{ row.target_high.toFixed(2) }}</span></template></el-table-column>
            <el-table-column label="失效价" width="76" align="right"><template #default="{ row }"><span class="num">{{ row.invalidate_price.toFixed(2) }}</span></template></el-table-column>
            <el-table-column prop="outcome" label="收盘对账" min-width="100" />
          </el-table>
          <p class="muted">依据强度是模型对判断依据的自评，<b>不是胜率</b>；对账只用分钟收盘价，“未触发”可能有漏判。研究参考，不构成投资建议。</p>
        </el-card>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.body { display: flex; flex-direction: column; gap: 12px; }
.head-row { display: flex; align-items: center; gap: 8px; }
h3 { margin: 6px 0 0; font-size: 15px; }
h3 .sub { font-weight: 400; font-size: 12.5px; color: var(--as-muted); }
.alert-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.alert-text { white-space: pre-wrap; word-break: break-word; font: inherit; margin: 0; }
.snapshot { margin: 8px 0 0; font-size: 13px; }
</style>
