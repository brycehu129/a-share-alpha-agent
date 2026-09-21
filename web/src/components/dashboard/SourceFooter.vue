<script setup>
import { computed } from 'vue'
import { fmtDateTime } from '../../format'

const props = defineProps({ data: { type: Object, required: true } })

const rateText = (v) => (v === null || v === undefined ? '样本不足' : v + '%')

// 口径与局限：固定说明 + 随数据变化的几条。全部按文本渲染。
const limits = computed(() => {
  const d = props.data
  const cs = d.agent.calibration_short
  const r = d.research || {}
  const list = [
    `行业分类来源：${r.industry_provider}，当前映射 ${r.mapping_mapped}/${r.mapping_universe} 只，未映射 ${r.mapping_unmapped} 只。`,
    '游资净买卖/涨跌停数据来自单独同步的接口（hm_detail/limit_list_d），目前只覆盖近期少数交易日；“无数据”是“还没同步到”，不是“当日无活动”，打分里的游资加成同样遵循这个口径（无数据=零加成，不是负分）。',
  ]
  if (cs) {
    list.push(`短线两条track合计历史回看样本 ${cs.historical_n || 0} 条，前瞻留档已到期 ${cs.live_n || 0} 条；样本不足时胜率留空，不代表零胜率。`)
    list.push(`突破track前瞻胜率：${rateText(cs.live_win_rate_breakout)}；回调反弹track前瞻胜率：${rateText(cs.live_win_rate_pullback)}。`)
  } else if (d.agent.calibration) {
    list.push(`研究校准样本：历史 ${d.agent.calibration.historical_n} 条，前瞻已到期 ${d.agent.calibration.live_n} 条；样本不足时胜率留空，不代表零胜率。`)
  }
  const own = (cs && cs.limitations) || (d.agent.calibration && d.agent.calibration.limitations) || []
  return list.concat(own)
})
const isHttp = (u) => /^https?:\/\//i.test(String(u))
</script>

<template>
  <footer class="footer">
    <div class="grid">
      <div>
        <h3>数据来源（同批次运行）</h3>
        <ul>
          <li v-for="(s, i) in data.sources || []" :key="i">
            <a v-if="isHttp(s.url)" :href="s.url" target="_blank" rel="noopener noreferrer">{{ s.kind }}</a><span v-else>{{ s.kind }}</span>
            · {{ fmtDateTime(s.generated_at) }}
          </li>
        </ul>
      </div>
      <div>
        <h3>口径与局限</h3>
        <ul><li v-for="(l, i) in limits" :key="i">{{ l }}</li></ul>
      </div>
    </div>
    <p class="muted">本页面由服务器直接从仓库 market-data 分支的 dashboard/latest.json 渲染，每次打开/刷新都会重新拉取一次（服务器端做了30秒缓存，避免短时间内重复请求GitHub）。</p>
  </footer>
</template>

<style scoped>
.footer { border-top: 1px solid var(--el-border-color-light); padding-top: 18px; display: flex; flex-direction: column; gap: 10px; }
.grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr); gap: 22px; }
h3 { font-size: 13px; margin: 0 0 8px; color: var(--el-text-color-regular); }
ul { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; font-size: 12.5px; color: var(--el-text-color-regular); line-height: 1.6; }
@media (max-width: 860px) { .grid { grid-template-columns: minmax(0, 1fr); } }
</style>
