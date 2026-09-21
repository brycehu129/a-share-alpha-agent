<script setup>
import { computed, ref } from 'vue'
import { fmtNum, fmtWan } from '../../format'
import RiseFall from '../RiseFall.vue'

const props = defineProps({
  candidates: { type: Array, default: () => [] },
  screen: { type: Object, required: true },
  holdSessions: { type: Number, default: 3 },
})

const TRACK_LABEL = { breakout: '突破', pullback: '回调反弹' }
const SHOW_DEFAULT = 10
const expanded = ref(false)
const rows = computed(() => (expanded.value ? props.candidates : props.candidates.slice(0, SHOW_DEFAULT)))

// 整列都没有游资数据（全是「无数据」）时把这一列藏起来，别让它白占宽度。
const hasHotmoney = computed(() => props.candidates.some((c) => c.hotmoney))

const trackCounts = computed(() => ['breakout', 'pullback'].map((k) => props.candidates.filter((c) => c.strategy_type === k).length))

const scoreMin = computed(() => Math.floor(Math.min(...props.candidates.map((c) => c.score))) - 3)
const scorePct = (s) => Math.max(4, Math.min(100, ((s - scoreMin.value) / (100 - scoreMin.value)) * 100))

function probText(p) {
  return p.probability !== null && p.probability !== undefined ? `${p.probability}% (${p.low}–${p.high})` : '样本不足'
}
const probTitle = (p) => (p.probability !== null && p.probability !== undefined ? '' : `样本 n=${p.n}，日期组 ${p.cohorts}`)

function hmChips(hm) {
  const chips = []
  const net = hm.hm_net_amount
  if (net !== null && net !== undefined && net !== 0) {
    chips.push(net > 0 ? { type: 'warning', text: `游资净买${fmtWan(net)}` } : { type: 'success', text: `游资净卖${fmtWan(Math.abs(net))}` })
  }
  if (hm.limit_status === 'U') chips.push({ type: 'danger', text: hm.limit_times ? `${hm.limit_times}连板` : '涨停' })
  else if (hm.limit_status === 'D') chips.push({ type: 'success', text: '跌停' }) // 跌停是「跌」→ 绿色
  else if (hm.limit_status === 'Z') chips.push({ type: 'warning', text: '炸板' })
  return chips
}

const exclusionParts = computed(() => Object.entries(props.screen.exclusion_counts || {}).map(([k, v]) => `${k} ${v}`).join(' · '))
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-title">
        <span>候选股票与评分</span>
        <span class="sub">
          截至 {{ screen.cutoff }} · 沪深在市 {{ screen.listed }} · 有效窗口覆盖 {{ screen.coverage_pct }}% · 突破通过 {{ trackCounts[0] }} 只 · 回调反弹通过 {{ trackCounts[1] }} 只（{{ holdSessions }}日持有）
        </span>
      </div>
    </template>

    <el-table :data="rows" empty-text="今天没有通过筛选的候选。" :row-key="(r) => r.symbol">
      <el-table-column label="#" width="56" align="center">
        <template #default="{ $index }"><span class="rank-badge" :class="$index < 3 ? `rank-${$index + 1}` : ''">{{ $index + 1 }}</span></template>
      </el-table-column>
      <el-table-column label="Track" width="110">
        <template #default="{ row }"><el-tag :type="row.strategy_type === 'breakout' ? 'primary' : 'success'" effect="light" round size="small">{{ TRACK_LABEL[row.strategy_type] || row.strategy_type || '—' }}</el-tag></template>
      </el-table-column>
      <el-table-column label="股票" min-width="120">
        <template #default="{ row }"><span class="stock-name">{{ row.name }}</span><span class="stock-code num">{{ row.symbol }}</span></template>
      </el-table-column>
      <el-table-column label="行业" min-width="100">
        <template #default="{ row }"><el-tag type="info" effect="plain" round size="small">{{ row.industry }}</el-tag></template>
      </el-table-column>
      <el-table-column label="综合分" min-width="170">
        <template #default="{ row }">
          <div class="score-cell">
            <div class="score-track"><div class="score-fill" :style="{ width: scorePct(row.score) + '%' }" /></div>
            <span class="score-num num">{{ fmtNum(row.score, 1) }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="20日超额" width="110" align="right">
        <template #default="{ row }"><RiseFall :value="row.excess20_pp" :digits="1" /></template>
      </el-table-column>
      <el-table-column v-if="hasHotmoney" label="游资/涨停" min-width="150">
        <template #default="{ row }">
          <span v-if="!row.hotmoney" class="muted">无数据</span>
          <template v-else>
            <div v-if="hmChips(row.hotmoney).length" class="chips">
              <el-tag v-for="c in hmChips(row.hotmoney)" :key="c.text" :type="c.type" size="small" round>{{ c.text }}</el-tag>
            </div>
            <span v-else class="muted">当日无净买卖/涨跌停</span>
          </template>
        </template>
      </el-table-column>
      <el-table-column :label="`${holdSessions}日研究胜率`" min-width="130">
        <template #default="{ row }"><span class="muted num" :title="probTitle(row.probability)">{{ probText(row.probability) }}</span></template>
      </el-table-column>
    </el-table>

    <el-button v-if="candidates.length > SHOW_DEFAULT" link type="primary" :aria-expanded="expanded" style="margin-top: 8px" @click="expanded = !expanded">
      {{ expanded ? '收起候选列表 ▴' : `展开全部候选（${candidates.length}） ▾` }}
    </el-button>

    <p class="muted">
      <b>基础样本 {{ screen.listed }} 只，排除后剩 {{ screen.eligible }} 只，有效窗口 {{ screen.valid }} 只。</b>主要排除原因：{{ exclusionParts }}。
      游资净买卖/涨跌停数据来自单独同步的接口，覆盖范围有限（详见页面底部说明），“无数据”不代表当日没有活动。
      <template v-if="!hasHotmoney">当前候选都没有游资/涨停数据，所以表里的「游资/涨停」列已隐藏。</template>
    </p>
  </el-card>
</template>

<style scoped>
.rank-badge { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 999px; font-size: 12px; font-weight: 700; background: var(--el-fill-color); color: var(--as-muted); }
.rank-1 { background: var(--as-gold-hi); color: #fff; }
.rank-2 { background: #9aa5b1; color: #fff; }
.rank-3 { background: #b08050; color: #fff; }
.score-cell { display: flex; align-items: center; gap: 8px; }
.score-track { flex: 1; height: 6px; border-radius: 999px; background: var(--el-fill-color); overflow: hidden; min-width: 48px; }
.score-fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--as-gold-lo), var(--as-gold-hi)); }
.score-num { font-weight: 700; min-width: 40px; text-align: right; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
</style>
