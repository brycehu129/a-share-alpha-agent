<script setup>
import { computed } from 'vue'

// 净值曲线（自绘 SVG，不引图表库）。viewBox 固定、按比例缩放，文字和圆点不会被拉伸变形。
const props = defineProps({
  points: { type: Array, default: () => [] }, // [{date?, nav, benchmark_nav?}]
  benchmark: { type: Boolean, default: false },
  height: { type: Number, default: 160 },
  baseline: { type: Number, default: null }, // 画一条参考虚线（如 1.000）
  label: { type: String, default: '净值曲线' },
})

const W = 560
const padL = 44
const padR = 10
const padT = 14
const padB = 22

const model = computed(() => {
  const pts = props.points
  const n = pts.length
  if (n < 1) return null
  const navs = pts.map((p) => p.nav)
  const bens = props.benchmark ? pts.map((p) => p.benchmark_nav) : []
  const all = navs.concat(bens.filter((v) => v !== null && v !== undefined), props.baseline !== null ? [props.baseline] : [])
  let lo = Math.min(...all)
  let hi = Math.max(...all)
  if (hi - lo < 0.01) { lo -= 0.01; hi += 0.01 }
  const pad = (hi - lo) * 0.12
  lo -= pad
  hi += pad
  const H = props.height
  const x = (i) => (n <= 1 ? padL + (W - padL - padR) / 2 : padL + (i / (n - 1)) * (W - padL - padR))
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB)
  const path = (arr) => arr.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const grid = [0, 1, 2].map((g) => {
    const v = lo + ((hi - lo) * g) / 2
    return { y: y(v), text: v.toFixed(3) }
  })
  // x 轴标签抽稀：最多约 8 个，避免点多时互相重叠
  const step = Math.max(1, Math.ceil(n / 8))
  const xLabels = pts
    .map((p, i) => ({ i, text: p.date ? String(p.date).slice(5) : '' }))
    .filter((l) => l.text && (l.i % step === 0 || l.i === n - 1))
    .map((l) => ({ x: x(l.i), text: l.text }))
  return {
    H, grid, xLabels,
    navPath: path(navs),
    benPath: bens.length ? path(bens.map((v) => (v === null || v === undefined ? navs[0] : v))) : '',
    baseY: props.baseline !== null ? y(props.baseline) : null,
    dots: navs.map((v, i) => ({ x: x(i), y: y(v) })),
    first: navs[0], last: navs[n - 1], n,
  }
})

const aria = computed(() => {
  const m = model.value
  return m ? `${props.label}：${m.n} 个点，从 ${m.first.toFixed(4)} 到 ${m.last.toFixed(4)}` : `${props.label}：暂无数据`
})
</script>

<template>
  <svg v-if="model" class="nav-chart" :viewBox="`0 0 ${W} ${model.H}`" role="img" :aria-label="aria">
    <template v-for="g in model.grid" :key="g.y">
      <line :x1="padL" :x2="W - padR" :y1="g.y" :y2="g.y" class="grid" />
      <text x="4" :y="g.y + 4" class="axis">{{ g.text }}</text>
    </template>
    <line v-if="model.baseY !== null" :x1="padL" :x2="W - padR" :y1="model.baseY" :y2="model.baseY" class="base" />
    <path v-if="model.benPath" :d="model.benPath" class="bench" />
    <path :d="model.navPath" class="nav" />
    <circle v-for="(d, i) in model.dots" :key="i" :cx="d.x" :cy="d.y" r="3" class="dot" />
    <text v-for="l in model.xLabels" :key="l.x" :x="l.x" :y="model.H - 6" text-anchor="middle" class="axis">{{ l.text }}</text>
  </svg>
  <div v-else class="muted">暂无净值数据</div>
</template>

<style scoped>
.nav-chart { width: 100%; height: auto; display: block; }
.grid { stroke: var(--el-border-color-lighter); stroke-width: 1; }
.base { stroke: var(--el-border-color); stroke-width: 1; stroke-dasharray: 3 3; }
.axis { font-size: 10px; fill: var(--as-muted); font-family: var(--as-mono); }
.bench { fill: none; stroke: var(--as-flat); stroke-width: 2; stroke-dasharray: 4 3; }
.nav { fill: none; stroke: var(--el-color-primary); stroke-width: 2.5; }
.dot { fill: var(--el-color-primary); }
</style>
