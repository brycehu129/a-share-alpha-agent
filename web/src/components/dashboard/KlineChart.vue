<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, CandlestickChart, LineChart } from 'echarts/charts'
import { AxisPointerComponent, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { themeMode } from '../../composables/useTheme'

// 日 K + MA5/10/20/60 + 成交量。这个文件只在打开个股详情时才被加载（抽屉里 defineAsyncComponent），
// ECharts 按需引入，不进看板首屏的包。红涨绿跌沿用页面的颜色变量，深色模式随主题重画。
echarts.use([CandlestickChart, LineChart, BarChart, GridComponent, TooltipComponent, DataZoomComponent, LegendComponent, AxisPointerComponent, CanvasRenderer])

const props = defineProps({ bars: { type: Array, required: true }, height: { type: Number, default: 340 } })
const el = ref(null)
let chart = null
let ro = null

const css = (name, fallback) => (getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback)
const MA = [
  { key: 'ma5', name: 'MA5', color: '#e6a23c' },
  { key: 'ma10', name: 'MA10', color: '#2f6fdd' },
  { key: 'ma20', name: 'MA20', color: '#a238d1' },
  { key: 'ma60', name: 'MA60', color: '#1a9d8f' },
]
const wan = (v) => (v >= 1e4 ? (v / 1e4).toFixed(2) + '万' : String(Math.round(v)))
const wanAxis = (v) => (v >= 1e4 ? Math.round(v / 1e4) + '万' : String(Math.round(v))) // 坐标轴上不要小数，省宽度
const f2 = (v) => (v === null || v === undefined ? '—' : Number(v).toFixed(2))

function build() {
  const bars = props.bars
  const rise = css('--as-rise', '#c23b32')
  const fall = css('--as-fall', '#2f8f5b')
  const text = css('--as-muted', '#5b6660')
  const line = css('--el-border-color-lighter', '#e5e7eb')
  const dates = bars.map((b) => b.date)
  const volumes = bars.map((b) => ({ value: b.volume, itemStyle: { color: b.close >= b.open ? rise : fall, opacity: 0.75 } }))
  return {
    animation: false,
    legend: { top: 0, left: 'center', itemWidth: 14, itemHeight: 3, textStyle: { color: text, fontSize: 12 }, data: MA.map((m) => m.name) },
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#555' } },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' }, confine: true, borderWidth: 0, backgroundColor: 'rgba(30,34,40,0.92)', textStyle: { color: '#fff', fontSize: 12 },
      formatter(params) {
        const i = params[0].dataIndex
        const b = bars[i]
        const prev = i > 0 ? bars[i - 1].close : null
        const chg = prev ? b.close - prev : null
        const pct = prev ? (chg / prev) * 100 : null
        const cls = (v) => (v > 0 ? rise : v < 0 ? fall : '#fff')
        const row = (k, v, c) => `<div style="display:flex;justify-content:space-between;gap:18px"><span>${k}</span><b style="color:${c || '#fff'}">${v}</b></div>`
        return [`<div style="margin-bottom:4px">${b.date}</div>`,
          row('涨跌幅', pct === null ? '—' : (pct > 0 ? '+' : '') + pct.toFixed(2) + '%', cls(pct)),
          row('涨跌额', chg === null ? '—' : (chg > 0 ? '+' : '') + chg.toFixed(2), cls(chg)),
          row('开盘', f2(b.open)), row('收盘', f2(b.close)), row('最低', f2(b.low)), row('最高', f2(b.high)),
          ...MA.map((m) => row(m.name, f2(b[m.key]), m.color)),
          row('成交量', wan(b.volume) + '手')].join('')
      },
    },
    grid: [{ left: 52, right: 12, top: 30, height: '56%' }, { left: 52, right: 12, top: '72%', height: '18%' }],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, boundaryGap: true, axisLine: { lineStyle: { color: line } }, axisLabel: { show: false }, axisTick: { show: false } },
      { type: 'category', data: dates, gridIndex: 1, boundaryGap: true, axisLine: { lineStyle: { color: line } }, axisLabel: { color: text, fontSize: 11 }, axisTick: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { type: 'dashed', color: line } }, axisLabel: { color: text, fontSize: 11 } },
      { scale: true, gridIndex: 1, splitNumber: 2, splitLine: { show: false }, axisLabel: { color: text, fontSize: 11, formatter: (v) => wanAxis(v) } },
    ],
    // 滚轮默认留给抽屉滚动；按住 Ctrl/⌘ 再滚才缩放，拖动平移
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100, zoomOnMouseWheel: 'ctrl', moveOnMouseWheel: false, preventDefaultMouseMove: false }],
    series: [
      { name: 'K线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: bars.map((b) => [b.open, b.close, b.low, b.high]),
        itemStyle: { color: rise, color0: fall, borderColor: rise, borderColor0: fall } },
      ...MA.map((m) => ({ name: m.name, type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: bars.map((b) => b[m.key]), smooth: true, showSymbol: false, lineStyle: { width: 1.2, color: m.color }, itemStyle: { color: m.color } })),
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes },
    ],
  }
}

function render() {
  if (!el.value) return
  if (!chart) chart = echarts.init(el.value)
  chart.setOption(build(), true)
}

onMounted(() => {
  render()
  ro = new ResizeObserver(() => chart && chart.resize())
  ro.observe(el.value)
})
onBeforeUnmount(() => {
  if (ro) ro.disconnect()
  if (chart) chart.dispose()
  chart = null
})
watch(() => props.bars, render)
watch(themeMode, () => setTimeout(render, 0)) // 等 <html class="dark"> 切换完再取颜色
</script>

<template>
  <div ref="el" class="kline" :style="{ height: height + 'px' }" role="img" aria-label="日K线、均线与成交量图" />
</template>

<style scoped>
.kline { width: 100%; }
</style>
