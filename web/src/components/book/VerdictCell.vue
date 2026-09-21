<script setup>
import { computed } from 'vue'
import { fmtNum } from '../../format'

// 一行的「系统结论」：标签 + 第一条理由；悬停看全部理由、系统止损/止盈位与数据局限。
// 结论由规则算出（server/book_verdict.py），不是用户声明的；页面只负责展示。
const props = defineProps({ verdict: { type: Object, default: null } })

// 颜色只是辅助（要不要你注意），准确的信息在标签文字和理由里；实心标签 = 需要你现在看一眼的结论。
const TYPE = { buy: 'success', blocked: 'warning', wait: 'info', nodata: 'info', exit: 'danger', reduce: 'warning', t: 'primary', hold: 'info' }
const type = computed(() => TYPE[props.verdict && props.verdict.action] || 'info')
const strong = computed(() => ['buy', 'exit', 'reduce'].includes(props.verdict && props.verdict.action))
</script>

<template>
  <span v-if="!verdict" class="muted">—</span>
  <el-popover v-else placement="left" :width="340" trigger="hover">
    <template #reference>
      <div class="verdict">
        <el-tag :type="type" :effect="strong ? 'dark' : 'plain'" size="default">{{ verdict.label }}<template v-if="verdict.track && ['buy', 'blocked'].includes(verdict.action)"> · {{ verdict.track }}</template></el-tag>
        <span class="first">{{ verdict.reasons[0] }}</span>
      </div>
    </template>
    <div class="detail">
      <ul>
        <li v-for="(r, i) in verdict.reasons" :key="i">{{ r }}</li>
      </ul>
      <p v-if="verdict.stop_price" class="levels">
        系统止损位 <b class="num">{{ fmtNum(verdict.stop_price) }}</b>（{{ fmtNum(verdict.to_stop_pct) }}%） ·
        系统止盈位 <b class="num">{{ fmtNum(verdict.target_price) }}</b>（+{{ fmtNum(verdict.to_target_pct) }}%）
      </p>
      <p v-for="c in verdict.caveats || []" :key="c" class="caveat">⚠ {{ c }}</p>
      <p class="muted">规则给出的提示，不是投资建议；系统只提醒、不下单。</p>
    </div>
  </el-popover>
</template>

<style scoped>
.verdict { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; cursor: default; }
.first { color: var(--as-muted); font-size: 12px; line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.detail ul { margin: 0 0 8px; padding-left: 18px; line-height: 1.6; }
.levels { margin: 0 0 6px; font-size: 13px; }
.caveat { margin: 0 0 6px; font-size: 12.5px; color: var(--el-color-warning); }
.detail .muted { margin: 0; }
</style>
