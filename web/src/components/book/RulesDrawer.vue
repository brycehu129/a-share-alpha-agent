<script setup>
import { computed, ref, watch } from 'vue'
import { get } from '../../api'

// 「判断规则」抽屉：把系统给出每一种结论（彻底卖出 / 可暂时卖出 / 做T / 买入信号…）的条件逐条写出来。
// 文字由后端按代码里真正在用的阈值生成（GET /api/book/rules），改了阈值这里自动跟着变。
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  section: { type: String, default: 'holding' }, // 打开时默认看哪一部分：holding / watch
})
const emit = defineEmits(['update:modelValue'])
const open = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })

const doc = ref(null)
const error = ref('')
const loading = ref(false)
const active = ref('holding')

async function load() {
  loading.value = true
  error.value = ''
  try {
    doc.value = await get('/api/book/rules')
  } catch (e) {
    error.value = e.message || String(e)
  } finally {
    loading.value = false
  }
}
watch(
  () => props.modelValue,
  (isOpen) => {
    if (!isOpen) return
    active.value = props.section
    if (!doc.value) load()
  },
)

// 与 VerdictCell 同一套配色：实心 = 需要你现在看一眼的结论。
const TYPE = { buy: 'success', blocked: 'warning', wait: 'info', nodata: 'info', exit: 'danger', reduce: 'warning', t: 'primary', hold: 'info' }
const STRONG = ['buy', 'exit', 'reduce']
</script>

<template>
  <el-drawer v-model="open" title="判断规则：系统怎么给出每一种结论" size="min(640px, 100%)" append-to-body>
    <div v-loading="loading" style="min-height: 120px">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>
      <template v-if="doc">
        <el-tabs v-model="active">
          <el-tab-pane v-for="key in ['holding', 'watch']" :key="key" :label="key === 'holding' ? '持仓' : '自选股'" :name="key">
            <h3 class="sec-title">{{ doc[key].title }}</h3>
            <section v-for="g in doc[key].groups" :key="g.action" class="group">
              <div class="group-head">
                <el-tag :type="TYPE[g.action]" :effect="STRONG.includes(g.action) ? 'dark' : 'plain'">{{ g.label }}</el-tag>
                <span class="when">{{ g.when }}</span>
              </div>
              <p v-if="g.intro" class="intro">{{ g.intro }}</p>
              <ul v-if="g.rules.length" class="rules">
                <li v-for="(r, i) in g.rules" :key="i">
                  {{ r.text }}
                  <ul v-if="r.sub" class="sub">
                    <li v-for="(s, j) in r.sub" :key="j">{{ s }}</li>
                  </ul>
                </li>
              </ul>
            </section>
            <ul class="notes">
              <li v-for="n in doc[key].notes" :key="n">{{ n }}</li>
            </ul>
          </el-tab-pane>
        </el-tabs>
        <el-alert :title="doc.disclaimer" type="info" show-icon :closable="false" />
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.sec-title { margin: 0 0 14px; font-size: 15px; }
.group { padding: 12px 14px; margin-bottom: 12px; border: 1px solid var(--el-border-color-light); border-radius: 8px; }
.group-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.when { font-size: 13px; font-weight: 600; }
.intro { margin: 8px 0 0; color: var(--as-muted); font-size: 13px; line-height: 1.6; }
.rules { margin: 8px 0 0; padding-left: 20px; line-height: 1.7; font-size: 13.5px; }
.rules li + li { margin-top: 4px; }
.sub { margin: 4px 0 2px; padding-left: 18px; color: var(--as-muted); font-size: 13px; }
.notes { padding-left: 20px; color: var(--as-muted); font-size: 13px; line-height: 1.7; margin: 4px 0 16px; }
</style>
