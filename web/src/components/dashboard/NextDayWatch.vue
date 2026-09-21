<script setup>
import { computed } from 'vue'
import { fmtAmount, fmtBoards, fmtNum, fmtPct, fmtTs } from '../../format'
import { openStock } from '../../composables/useStockDetail'

// 次日关注：从当日涨停池 + 龙虎榜里用透明规则筛出的强势股，附 AI 点评。分数是规则分数，不是概率；
// 每一分从哪来都列在「加分项」里，风险点单独列出。AI 只点评这几只，不增删候选，也不给具体价位。
const props = defineProps({ watch: { type: Object, default: null } })

const items = computed(() => (props.watch && props.watch.items) || [])
const s = computed(() => (props.watch && props.watch.sentiment) || null)
const ai = computed(() => (props.watch && props.watch.ai) || null)
const meta = computed(() => (props.watch && props.watch.ai_meta) || null)
const VERDICT = { focus: { text: '重点关注', type: 'primary' }, watch: { text: '观察', type: 'info' }, avoid: { text: '回避', type: 'warning' } }
const scoreTone = (n) => (n >= 75 ? 'hi' : n >= 60 ? 'mid' : 'lo')
const aiReason = computed(() => {
  if (!meta.value) return '尚未生成（17:30 的定时任务会生成 AI 点评）'
  if (meta.value.status === 'ok') return ''
  return `${meta.value.status || '未生成'}${meta.value.error ? '：' + meta.value.error : ''}`
})
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-title">
        <span>次日关注 <span v-if="watch && watch.date" class="date num">{{ watch.date }}</span></span>
        <span class="sub">规则打分{{ watch ? ' ' + fmtTs(watch.generated_at) : '' }} · AI 点评{{ meta && meta.completed_at ? ' ' + fmtTs(meta.completed_at) : '' }} · 非概率，不构成投资建议</span>
      </div>
    </template>

    <p v-if="!watch" class="muted" style="margin: 0">次日关注暂无：涨停池/龙虎榜落盘后由定时任务（工作日 16:30、17:30）生成。</p>
    <p v-else-if="!items.length" class="muted" style="margin: 0">{{ watch.note || '当日没有满足条件的候选。' }}</p>
    <template v-else>
      <div v-if="s" class="sentiment num">
        <span>涨停 <b class="rise">{{ s.zt ?? '—' }}</b></span>
        <span>炸板 <b>{{ s.zb ?? '—' }}</b></span>
        <span>跌停 <b class="fall">{{ s.dt ?? '—' }}</b></span>
        <span>封板率 <b>{{ s.seal_rate === null || s.seal_rate === undefined ? '—' : fmtNum(s.seal_rate, 0) + '%' }}</b></span>
        <span>最高板 <b>{{ s.max_boards ?? '—' }}</b><template v-if="s.max_board_names && s.max_board_names.length">（{{ s.max_board_names.join('、') }}）</template></span>
        <span v-if="s.yzt_avg_pct !== null && s.yzt_avg_pct !== undefined">昨日涨停今日均 <b :class="s.yzt_avg_pct >= 0 ? 'rise' : 'fall'">{{ fmtPct(s.yzt_avg_pct) }}</b>，收跌占 {{ fmtNum(s.yzt_down_pct, 0) }}%</span>
      </div>
      <div v-if="ai && ai.market_view" class="ai-market"><el-tag size="small" type="primary" effect="plain">AI</el-tag> {{ ai.market_view }}</div>
      <p v-else-if="aiReason" class="muted" style="margin: 0 0 10px">AI 点评：{{ aiReason }}。下面是规则筛选结果。</p>

      <div class="grid">
        <button v-for="(it, i) in items" :key="it.symbol" type="button" class="card" @click="openStock(it.symbol, it.name)">
          <span class="top">
            <span class="rank num">{{ i + 1 }}</span>
            <span class="who"><span class="name">{{ it.name }}</span><span class="code num">{{ it.symbol }}<template v-if="it.industry"> · {{ it.industry }}</template></span></span>
            <span class="score num" :class="scoreTone(it.score)" :title="'规则分数（0–100），不是概率'">{{ it.score }}</span>
          </span>
          <span class="facts num">
            <span>{{ fmtBoards(it.boards) }}</span>
            <span v-if="it.first_seal">首封 {{ it.first_seal.slice(0, 5) }}</span>
            <span v-if="it.seal_fund !== null && it.seal_fund !== undefined">封单 {{ fmtAmount(it.seal_fund) }}</span>
            <span v-if="it.turnover !== null && it.turnover !== undefined">换手 {{ it.turnover }}%</span>
            <span v-if="it.lhb_net !== null && it.lhb_net !== undefined" :class="it.lhb_net > 0 ? 'rise' : 'fall'">龙虎榜 {{ it.lhb_net > 0 ? '净买' : '净卖' }} {{ fmtAmount(it.lhb_net) }}</span>
          </span>
          <span class="tags"><el-tag v-for="t in it.tags" :key="t" size="small" effect="plain" round>{{ t }}</el-tag></span>
          <span v-if="it.reasons.length" class="list plus"><i>加分</i>{{ it.reasons.join('；') }}</span>
          <span v-if="it.risks.length" class="list risk"><i>风险</i>{{ it.risks.join('；') }}</span>
          <span v-if="it.ai" class="ai">
            <span class="ai-head"><el-tag size="small" :type="(VERDICT[it.ai.verdict] || VERDICT.watch).type">AI · {{ (VERDICT[it.ai.verdict] || VERDICT.watch).text }}</el-tag></span>
            <span class="ai-line">{{ it.ai.view }}</span>
            <span class="ai-line"><b>思路</b> {{ it.ai.plan }}</span>
            <span class="ai-line"><b>风险</b> {{ it.ai.risk }}</span>
          </span>
        </button>
      </div>
      <p class="muted note">
        规则：连板高度（2–4 板加分，≥5 板只提示风险）、封单占流通市值、首封早晚、是否炸板、换手、板块内涨停家数、龙虎榜净买卖；ST/次新已排除；一字板次日难买入会扣分并标注。
        规则版本 {{ watch.rules_version }}。AI 只点评这几只，不看新闻公告，不给价位——它的判断也不是胜率。
      </p>
    </template>
  </el-card>
</template>

<style scoped>
.date { font-size: 13px; font-weight: 600; color: var(--as-muted); margin-left: 6px; }
.sentiment { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 13px; color: var(--as-muted); margin-bottom: 10px; }
.sentiment b { color: var(--el-text-color-primary); }
.sentiment b.rise { color: var(--as-rise); }
.sentiment b.fall { color: var(--as-fall); }
.ai-market { margin-bottom: 12px; padding: 8px 12px; border-radius: 8px; background: var(--el-color-primary-light-9); line-height: 1.6; font-size: 13.5px; }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.card { display: flex; flex-direction: column; gap: 6px; padding: 12px 14px; text-align: left; background: var(--el-bg-color); border: 1px solid var(--el-border-color-light); border-radius: 10px; font: inherit; color: inherit; cursor: pointer; min-width: 0; }
.card:hover, .card:focus-visible { border-color: var(--el-color-primary); outline: none; }
.top { display: flex; align-items: center; gap: 10px; }
.rank { width: 22px; height: 22px; line-height: 22px; text-align: center; border-radius: 50%; background: var(--el-fill-color); font-size: 12px; font-weight: 700; flex: none; }
.who { display: flex; flex-direction: column; min-width: 0; margin-right: auto; }
.name { font-weight: 700; font-size: 15px; }
.code { font-size: 11.5px; color: var(--as-muted); }
.score { font-size: 22px; font-weight: 800; }
.score.hi { color: var(--as-rise); }
.score.mid { color: var(--as-gold-hi); }
.score.lo { color: var(--as-muted); }
.facts { display: flex; flex-wrap: wrap; gap: 2px 12px; font-size: 12.5px; }
.tags { display: flex; flex-wrap: wrap; gap: 4px; }
.list { font-size: 12.5px; line-height: 1.6; color: var(--el-text-color-regular); }
.list i { font-style: normal; font-weight: 700; margin-right: 6px; font-size: 11.5px; padding: 0 5px; border-radius: 3px; }
.list.plus i { color: var(--as-rise); background: color-mix(in srgb, var(--as-rise) 12%, transparent); }
.list.risk i { color: var(--as-gold-hi); background: color-mix(in srgb, var(--as-gold-hi) 14%, transparent); }
.ai { display: flex; flex-direction: column; gap: 3px; padding: 8px 10px; border-radius: 8px; background: var(--el-fill-color-light); font-size: 12.5px; line-height: 1.6; }
.ai-head { display: flex; }
.ai-line b { margin-right: 4px; }
.note { margin: 12px 0 0; }
@media (max-width: 860px) { .grid { grid-template-columns: minmax(0, 1fr); } }
</style>
