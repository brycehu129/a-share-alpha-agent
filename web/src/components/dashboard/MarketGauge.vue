<script setup>
import { computed } from 'vue'
import { fmtNum } from '../../format'

const props = defineProps({ screen: { type: Object, required: true } })
const clamp = (v) => Math.max(0, Math.min(100, Number(v)))
const score = computed(() => clamp(props.screen.market_score))
const pause = computed(() => clamp(props.screen.market_score_pause))
const weak = computed(() => Number(props.screen.market_score) < 50)
</script>

<template>
  <div class="gauge-card">
    <div class="gauge-label">市场评分（规则分数，非概率）</div>
    <div class="gauge-value"><span class="num">{{ fmtNum(screen.market_score, 1) }}</span><span class="gauge-max"> / 100</span></div>
    <!-- 刻度直接标在进度条上：40 = 暂停新计划线，50 = 趋势判定线 -->
    <div class="gauge-track" role="img" :aria-label="`市场评分 ${fmtNum(screen.market_score, 1)}，暂停线 ${screen.market_score_pause}，趋势线 50`">
      <div class="gauge-fill" :style="{ width: score + '%' }" />
      <div class="gauge-mark" :style="{ left: pause + '%' }" />
      <div class="gauge-mark" style="left: 50%" />
    </div>
    <div class="gauge-ticks">
      <span class="tick" :style="{ left: pause + '%' }">{{ screen.market_score_pause }} 暂停新计划</span>
      <span class="tick" style="left: 50%">50 趋势判定</span>
    </div>
    <el-tag :type="weak ? 'info' : 'success'" round class="gauge-regime">
      当前判定：{{ screen.regime }}{{ weak ? '（低于50，暂不视为趋势市）' : '（≥50，趋势市）' }}
    </el-tag>
  </div>
</template>

<style scoped>
.gauge-card { background: var(--el-bg-color); border: 1px solid var(--el-border-color-light); border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; height: 100%; box-sizing: border-box; }
.gauge-label { font-size: 13px; color: var(--el-text-color-regular); font-weight: 600; }
.gauge-value { font-size: 30px; font-weight: 600; line-height: 1; }
.gauge-max { font-size: 14px; color: var(--as-muted); font-weight: 500; }
.gauge-track { position: relative; height: 8px; border-radius: 999px; background: var(--el-fill-color); margin-block: 8px 0; }
.gauge-fill { position: absolute; inset: 0 auto 0 0; border-radius: 999px; background: linear-gradient(90deg, var(--as-gold-lo), var(--as-gold-hi)); }
.gauge-mark { position: absolute; top: -3px; width: 2px; height: 14px; background: var(--as-muted); opacity: 0.6; transform: translateX(-1px); }
.gauge-ticks { position: relative; height: 14px; font-size: 11px; color: var(--as-muted); }
.tick { position: absolute; transform: translateX(-50%); white-space: nowrap; }
/* 40 与 50 只差 10% 的宽度：前一个标签右对齐到 40 的刻度，后一个左对齐到 50，两者不会重叠 */
.tick:first-child { transform: translateX(-100%); padding-right: 3px; }
.tick:last-child { transform: none; padding-left: 3px; }
.gauge-regime { align-self: flex-start; height: auto; padding: 4px 10px; white-space: normal; line-height: 1.4; }
</style>
