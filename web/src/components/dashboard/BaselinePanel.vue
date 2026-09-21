<script setup>
import { computed } from 'vue'
import { fmtNum } from '../../format'

const props = defineProps({ baseline: { type: Object, default: null } })

const VERDICT = { better: '策略优于随机', worse: '策略不如随机', indistinguishable: '目前无法区分策略与随机' }

const netText = (r) => (!r || r.barrier_net_pess === null || r.barrier_net_pess === undefined ? '—' : `${fmtNum(r.barrier_net_pess, 2)} ~ ${fmtNum(r.barrier_net_opt, 2)}%`)
const exitMix = (r) => (!r || r.stop_pct === null || r.stop_pct === undefined ? '' : `；止损${Math.round(r.stop_pct)}%/止盈${Math.round(r.target_pct)}%/到期${Math.round(r.expiry_pct)}%`)
const has = (v) => v !== null && v !== undefined

// 一个区块 = 一组基线对比；前瞻和历史回放各一块。
function block(b, title) {
  if (!b) return { title, empty: true }
  const p = b.primary_result
  const verdict = p && p.verdict ? VERDICT[p.verdict] : '样本不足'
  const detail = p && has(p.mean) ? `（差值 ${fmtNum(p.mean, 2)} pp，95% 区间 [${fmtNum(p.low, 2)}, ${fmtNum(p.high, 2)}]，${p.days} 个日期组）` : ''
  const rows = []
  Object.values(b.baselines || {}).forEach((base) => {
    const br = base.base_rate
    const baseText = br ? `${netText(br)}${exitMix(br)}` : `样本不足（${base.days} 个日期组，需要 ${b.gate.min_days}）`
    ;['top10', 'top3'].forEach((t) => {
      const v = (base.vs || {})[t]
      if (!v) return
      const s = v.strategy
      const ex = v.barrier_net_pess || {}
      const fx = v.mean_excess_pp || {}
      rows.push({
        key: `${base.label}-${t}`, label: base.label, days: base.days, baseText,
        tier: t === 'top10' ? '前10名' : '可成交的前3',
        strat: s ? `${netText(s)}${exitMix(s)}` : `样本不足（${v.days} 个日期组、${v.strategy_n} 条）`,
        diff: has(ex.mean) ? `${fmtNum(ex.mean, 2)} [${fmtNum(ex.low, 2)}, ${fmtNum(ex.high, 2)}]` : '—',
        conclusion: VERDICT[ex.verdict] || '样本不足',
        secondary: has(fx.mean) ? fmtNum(fx.mean, 2) : '—',
      })
    })
  })
  return { title, empty: false, verdict, detail, rows }
}

const blocks = computed(() => (props.baseline ? [block(props.baseline, '前瞻（每日冻结、逐日验收——这才是证据）'), block(props.baseline.backfill, '历史回放（带幸存者/分类回看偏差，只做对照，不是前瞻证据）')] : []))
const sub = computed(() => (props.baseline ? `已冻结 ${props.baseline.frozen} 份抽样 · 已验收 ${props.baseline.resolved} 份` : ''))
</script>

<template>
  <el-card shadow="never">
    <template #header><div class="card-title"><span>随机基线（策略选股 vs 随手抽样）</span><span class="sub">{{ sub }}</span></div></template>
    <el-empty v-if="!baseline" :image-size="60" description="尚无基线数据：每天 15:35 冻结当天的随机抽样，日线走完后才验收，第一批约在第 4 个交易日。" />
    <template v-else>
      <p class="muted" style="margin-top: 0">
        固定标签的“赢”（3 日、扣 0.5% 成本、且跑赢沪深 300）的<b>底数不是 50%</b>——不知道底数就没法判断胜率是好是坏。每个截止日各抽 30 只，分四组（全体合格 / 强势行业 / 中间行业 / 最弱10%行业，后两组只做信息对照），与策略留档的前 10 / 可成交的前 3 <b>逐日配对</b>，并套<b>同一套按各自 ATR 定的止损/止盈规则</b>（次日开盘买入，最长持有 5 个交易日，先碰哪个先出；日线分不清先后，给悲观~乐观区间）。区间的标准误按日期算并做了重叠校正，需要至少 {{ baseline.gate.min_days }} 个日期组；区间跨过 0 就是“目前无法区分”，这是早期最常见的结果，不是故障。<b>主指标（看数据之前就定死的）</b>：前10名相对「强势行业内」基线的屏障净收益（悲观口径）逐日差值，其余都是次要指标。
      </p>
      <div v-for="b in blocks" :key="b.title" class="block">
        <h3>{{ b.title }}</h3>
        <p v-if="b.empty" class="empty">暂无数据。</p>
        <template v-else>
          <p><b>主指标：{{ b.verdict }}</b>{{ b.detail }}</p>
          <el-table :data="b.rows" size="small" empty-text="暂无对比行">
            <el-table-column prop="label" label="基线" min-width="120" />
            <el-table-column prop="days" label="日期组" width="80" align="right" />
            <el-table-column prop="baseText" label="底数：屏障净收益（悲观~乐观）" min-width="220" />
            <el-table-column prop="tier" label="策略档" width="110" />
            <el-table-column prop="strat" label="策略：屏障净收益" min-width="220" />
            <el-table-column prop="diff" label="净收益差值(悲观) [95%区间]" min-width="190"><template #default="{ row }"><span class="num">{{ row.diff }}</span></template></el-table-column>
            <el-table-column prop="conclusion" label="结论" min-width="150" />
            <el-table-column prop="secondary" label="次要：固定标签超额差(pp)" min-width="140" align="right"><template #default="{ row }"><span class="num">{{ row.secondary }}</span></template></el-table-column>
          </el-table>
        </template>
      </div>
    </template>
  </el-card>
</template>

<style scoped>
.block { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--el-border-color-lighter); }
h3 { font-size: 14px; margin: 0 0 8px; }
.empty { margin: 0; font-size: 13px; color: var(--as-muted); background: var(--el-fill-color-light); border-radius: 8px; padding: 10px 12px; }
</style>
