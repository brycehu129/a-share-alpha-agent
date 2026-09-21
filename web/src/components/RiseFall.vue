<script setup>
import { computed } from 'vue'
import { arrow, direction, fmtPct } from '../format'

// 带 ▲/▼ 的涨跌文本：颜色之外再给一个形状线索，色弱和黑白打印下也能分辨。
const props = defineProps({
  value: { type: Number, default: null },
  digits: { type: Number, default: 2 },
  bare: { type: Boolean, default: false }, // 不带箭头
})
const dir = computed(() => direction(props.value, props.digits))
const text = computed(() => fmtPct(props.value, props.digits))
</script>

<template>
  <span class="num" :class="dir">
    <template v-if="value === null || value === undefined">—</template>
    <template v-else><span v-if="!bare" aria-hidden="true">{{ arrow(dir) }} </span>{{ text }}</template>
  </span>
</template>
