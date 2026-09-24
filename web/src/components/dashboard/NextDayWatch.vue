<script setup>
import { computed, ref, watch as vueWatch } from 'vue'
import { fmtAmount, fmtBoards, fmtNum, fmtPct, fmtTs } from '../../format'
import { openStock } from '../../composables/useStockDetail'

// 次日关注：从当日涨停池 + 强势股池 + 龙虎榜用透明规则筛出的强势股，附 AI 点评。分数是规则分数，不是概率；
// 每一分从哪来都列在「加分项」里，风险点单独列出。AI 只点评 core 组，不增删候选，也不给具体价位。
//
// rules-2：候选按 bucket 分三组（可参与主榜 / 高位只观察 / 一字买不进），高位/一字不再挤占主榜；
// prevWatch（上一交易日兑现）是收盘后 watch_outcome.py 结算出的「那批票今天实际走成什么样」，
// 和当日名单共用同一套卡片布局，只是在 AI 块下方多一段客观走势 + AI 复盘。
const props = defineProps({ watch: { type: Object, default: null }, prevWatch: { type: Object, default: null } })

const BUCKET_LABEL = { core: '可参与（主榜）', high: '高位 · 只做观察', unbuyable: '一字 · 买不进' }
const BUCKET_HINT = { core: '', high: '连板过高或近期涨幅过大，次日接力风险大，只做观察不建议参与。',
                      unbuyable: '开盘即封死、全天未开板、换手极低，次日大概率仍是一字，正常买不进。' }
const PHASE_LABEL = { heating: '情绪升温', steady: '情绪平稳', cooling: '情绪退潮' }
const RESULT_TAG = { one_word: { text: '一字板', type: 'warning' }, limit_up: { text: '再涨停', type: 'success' },
                     broke: { text: '炸板', type: 'warning' }, limit_down: { text: '跌停', type: 'danger' },
                     up: { text: '收涨', type: 'success' }, flat: { text: '横盘', type: 'info' }, down: { text: '收跌', type: 'danger' } }
const VERDICT_TAG = { unbuyable: { text: '买不进', type: 'warning' }, hit: { text: '接得住', type: 'success' },
                      miss: { text: '未接住', type: 'danger' }, flat: { text: '持平', type: 'info' }, unknown: { text: '未知', type: 'info' } }

const view = ref('today')
vueWatch(() => props.prevWatch, (v) => { if (!v && view.value === 'prev') view.value = 'today' })

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

function groupByBucket(list) {
  const by = { core: [], high: [], unbuyable: [] }
  for (const it of list || []) (by[it.bucket] || by.core).push(it)
  return ['core', 'high', 'unbuyable'].map((k) => ({ key: k, label: BUCKET_LABEL[k], hint: BUCKET_HINT[k], items: by[k] }))
    .filter((g) => g.items.length)
}

const todaySections = computed(() => groupByBucket(items.value))
const prevItems = computed(() => (props.prevWatch && props.prevWatch.items) || [])
const prevSections = computed(() => groupByBucket(prevItems.value))
const prevSummary = computed(() => props.prevWatch && props.prevWatch.summary)
const prevAiReason = computed(() => {
  const m = props.prevWatch && props.prevWatch.ai_meta
  if (!m) return ''
  if (m.status === 'ok') return ''
  return `AI 复盘：${m.status || '未生成'}${m.error ? '：' + m.error : ''}。下面是规则结算结果。`
})

function positionLine(pos) {
  if (!pos) return ''
  const parts = []
  if (pos.run_up_10 !== null && pos.run_up_10 !== undefined) parts.push(`10日 ${fmtPct(pos.run_up_10, 0)}`)
  if (pos.from_high_60 !== null && pos.from_high_60 !== undefined) parts.push(`距60日高 ${fmtPct(pos.from_high_60, 0)}`)
  return parts.join(' · ')
}

function outcomeLine(o) {
  if (!o) return ''
  const parts = []
  if (o.open_pct !== null && o.open_pct !== undefined) parts.push(`高开 ${fmtPct(o.open_pct, 1)}`)
  if (o.high_pct !== null && o.high_pct !== undefined) parts.push(`最高 ${fmtPct(o.high_pct, 1)}`)
  if (o.low_pct !== null && o.low_pct !== undefined) parts.push(`最低 ${fmtPct(o.low_pct, 1)}`)
  if (o.close_pct !== null && o.close_pct !== undefined) parts.push(`收 ${fmtPct(o.close_pct, 1)}`)
  if (o.close_vs_open !== null && o.close_vs_open !== undefined) parts.push(`相对开盘 ${fmtPct(o.close_vs_open, 1)}`)
  return parts.join(' · ')
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-title">
        <span>
          次日关注 <span v-if="watch && watch.date" class="date num">{{ watch.date }}</span>
          <el-tag v-if="s && s.phase" size="small" effect="plain" :type="s.phase === 'cooling' ? 'warning' : s.phase === 'heating' ? 'success' : 'info'" style="margin-left: 6px">{{ PHASE_LABEL[s.phase] || s.phase }}</el-tag>
        </span>
        <span class="sub">规则打分{{ watch ? ' ' + fmtTs(watch.generated_at) : '' }} · AI 点评{{ meta && meta.completed_at ? ' ' + fmtTs(meta.completed_at) : '' }} · 非概率，不构成投资建议</span>
      </div>
      <div v-if="prevWatch" class="view-switch">
        <el-radio-group v-model="view" size="small">
          <el-radio-button value="today">今日名单</el-radio-button>
          <el-radio-button value="prev">上一交易日兑现（{{ prevWatch.date }}）</el-radio-button>
        </el-radio-group>
      </div>
    </template>

    <p v-if="!watch" class="muted" style="margin: 0">次日关注暂无：涨停池/强势股池/龙虎榜落盘后由定时任务（工作日 16:30、17:30）生成。</p>
    <template v-else-if="view === 'today'">
      <p v-if="!items.length" class="muted" style="margin: 0">{{ watch.note || '当日没有满足条件的候选。' }}</p>
      <template v-else>
        <div v-if="s" class="sentiment num">
          <span>涨停 <b class="rise">{{ s.zt ?? '—' }}</b></span>
          <span>炸板 <b>{{ s.zb ?? '—' }}</b></span>
          <span>跌停 <b class="fall">{{ s.dt ?? '—' }}</b></span>
          <span>封板率 <b>{{ s.seal_rate === null || s.seal_rate === undefined ? '—' : fmtNum(s.seal_rate, 0) + '%' }}</b></span>
          <span>最高板 <b>{{ s.max_boards ?? '—' }}</b><template v-if="s.max_board_names && s.max_board_names.length">（{{ s.max_board_names.join('、') }}）</template></span>
          <span v-if="s.yzt_avg_pct !== null && s.yzt_avg_pct !== undefined">昨日涨停今日均 <b :class="s.yzt_avg_pct >= 0 ? 'rise' : 'fall'">{{ fmtPct(s.yzt_avg_pct) }}</b>，收跌占 {{ fmtNum(s.yzt_down_pct, 0) }}%</span>
        </div>
        <p v-if="watch.note" class="muted note-inline">{{ watch.note }}</p>
        <div v-if="ai && ai.market_view" class="ai-market"><el-tag size="small" type="primary" effect="plain">AI</el-tag> {{ ai.market_view }}</div>
        <p v-else-if="aiReason" class="muted" style="margin: 0 0 10px">AI 点评：{{ aiReason }}。下面是规则筛选结果。</p>

        <div v-for="g in todaySections" :key="g.key" class="bucket">
          <div class="bucket-head"><span class="bucket-label">{{ g.label }}</span><span v-if="g.hint" class="muted bucket-hint">{{ g.hint }}</span></div>
          <div class="grid">
            <button v-for="(it, i) in g.items" :key="it.symbol" type="button" class="card" @click="openStock(it.symbol, it.name)">
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
              <span v-if="positionLine(it.position)" class="facts num muted">位置 {{ positionLine(it.position) }}</span>
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
        </div>
        <p class="muted note">
          规则：连板高度（2–4 板加分，≥5 板只提示风险）、封单占流通市值、首封早晚、是否炸板、换手、板块内涨停家数、龙虎榜净买卖、
          近 10 日累计涨幅与对 20 日线乖离（位置过高只提示风险不加分）；ST/次新已排除；一字板（含首板）单独归组，不进主榜；
          高位标的（≥4 连板或 10 日涨幅≥50%）单独归组，只做观察；情绪退潮时连板加分折半、高位与主榜名额一并收缩。
          规则版本 {{ watch.rules_version }}。AI 只点评可参与（主榜）这几只，不看新闻公告，不给价位——它的判断也不是胜率。
        </p>
      </template>
    </template>

    <template v-else>
      <p v-if="!prevItems.length" class="muted" style="margin: 0">上一交易日没有可结算的名单。</p>
      <template v-else>
        <div v-if="prevSummary" class="sentiment num">
          <span>结算 <b>{{ prevSummary.n }}</b> 只</span>
          <span>接得住 <b class="rise">{{ prevSummary.verdict_counts.hit ?? 0 }}</b></span>
          <span>未接住 <b class="fall">{{ prevSummary.verdict_counts.miss ?? 0 }}</b></span>
          <span>持平 <b>{{ prevSummary.verdict_counts.flat ?? 0 }}</b></span>
          <span>一字买不进 <b>{{ prevSummary.verdict_counts.unbuyable ?? 0 }}</b>（不计入命中）</span>
          <span v-if="prevSummary.avg_close_vs_open !== null && prevSummary.avg_close_vs_open !== undefined">
            平均相对开盘 <b :class="prevSummary.avg_close_vs_open >= 0 ? 'rise' : 'fall'">{{ fmtPct(prevSummary.avg_close_vs_open) }}</b>
          </span>
        </div>
        <div v-if="prevWatch.ai_market_view" class="ai-market"><el-tag size="small" type="primary" effect="plain">AI</el-tag> {{ prevWatch.ai_market_view }}</div>
        <p v-else-if="prevAiReason" class="muted" style="margin: 0 0 10px">{{ prevAiReason }}</p>

        <div v-for="g in prevSections" :key="g.key" class="bucket">
          <div class="bucket-head"><span class="bucket-label">{{ g.label }}</span><span v-if="g.hint" class="muted bucket-hint">{{ g.hint }}</span></div>
          <div class="grid">
            <button v-for="(it, i) in g.items" :key="it.symbol" type="button" class="card" @click="openStock(it.symbol, it.name)">
              <span class="top">
                <span class="rank num">{{ i + 1 }}</span>
                <span class="who"><span class="name">{{ it.name }}</span><span class="code num">{{ it.symbol }}</span></span>
                <span class="score num" :class="scoreTone(it.score)">{{ it.score }}</span>
              </span>
              <span class="tags"><el-tag v-for="t in it.tags" :key="t" size="small" effect="plain" round>{{ t }}</el-tag></span>
              <span v-if="it.reasons && it.reasons.length" class="list plus"><i>昨日加分</i>{{ it.reasons.join('；') }}</span>
              <span v-if="it.risks && it.risks.length" class="list risk"><i>昨日风险</i>{{ it.risks.join('；') }}</span>
              <span v-if="it.ai" class="ai">
                <span class="ai-head"><el-tag size="small" :type="(VERDICT[it.ai.verdict] || VERDICT.watch).type">昨日 AI · {{ (VERDICT[it.ai.verdict] || VERDICT.watch).text }}</el-tag></span>
                <span class="ai-line"><b>思路</b> {{ it.ai.plan }}</span>
                <span class="ai-line"><b>风险</b> {{ it.ai.risk }}</span>
              </span>
              <span v-if="it.outcome" class="outcome">
                <span class="outcome-head">
                  <el-tag size="small" :type="(RESULT_TAG[it.outcome.result] || {}).type || 'info'">{{ (RESULT_TAG[it.outcome.result] || {}).text || '未知' }}</el-tag>
                  <el-tag size="small" effect="plain" :type="(VERDICT_TAG[it.outcome.verdict] || {}).type || 'info'">{{ (VERDICT_TAG[it.outcome.verdict] || {}).text || '未知' }}</el-tag>
                </span>
                <span v-if="outcomeLine(it.outcome)" class="ai-line num">{{ outcomeLine(it.outcome) }}</span>
                <span v-if="it.outcome.path" class="ai-line">{{ it.outcome.path }}</span>
                <span v-if="it.outcome.quote_missing" class="ai-line muted">今日行情未取到，不下结论</span>
                <span v-if="it.outcome.ai_review" class="ai-line"><b>AI 复盘</b> {{ it.outcome.ai_review }}</span>
              </span>
            </button>
          </div>
        </div>
        <p class="muted note">
          {{ (prevWatch.limitations || []).join(' ') }}
        </p>
      </template>
    </template>
  </el-card>
</template>

<style scoped>
.card-title { display: flex; flex-direction: column; gap: 2px; }
.date { font-size: 13px; font-weight: 600; color: var(--as-muted); margin-left: 6px; }
.view-switch { margin-top: 8px; }
.note-inline { margin: 0 0 10px; }
.sentiment { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 13px; color: var(--as-muted); margin-bottom: 10px; }
.sentiment b { color: var(--el-text-color-primary); }
.sentiment b.rise { color: var(--as-rise); }
.sentiment b.fall { color: var(--as-fall); }
.ai-market { margin-bottom: 12px; padding: 8px 12px; border-radius: 8px; background: var(--el-color-primary-light-9); line-height: 1.6; font-size: 13.5px; }
.bucket { margin-bottom: 16px; }
.bucket:last-child { margin-bottom: 0; }
.bucket-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
.bucket-label { font-size: 13px; font-weight: 700; }
.bucket-hint { font-size: 12px; }
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
.ai, .outcome { display: flex; flex-direction: column; gap: 3px; padding: 8px 10px; border-radius: 8px; background: var(--el-fill-color-light); font-size: 12.5px; line-height: 1.6; }
.ai-head, .outcome-head { display: flex; gap: 6px; }
.ai-line b { margin-right: 4px; }
.note { margin: 12px 0 0; }
@media (max-width: 860px) { .grid { grid-template-columns: minmax(0, 1fr); } }
</style>
