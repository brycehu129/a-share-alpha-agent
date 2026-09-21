<script setup>
import { computed, reactive, watch } from 'vue'
import { post } from '../../api'
import { useAction } from '../../composables/useAction'
import { fmtMoney, fmtNum, fmtPrice, lotSize, todayStr } from '../../format'

// 从自选股买入：价格默认现价，数量按最小交易单位递增。成本价 = 你的买入价（多次买入取加权平均）。
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  stock: { type: Object, default: null }, // 自选行：symbol / name / last / limit_up / limit_down
  holding: { type: Object, default: null }, // 已持有时的持仓行：shares / cost_price
})
const emit = defineEmits(['update:modelValue', 'done'])
const open = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })

const form = reactive({ price: null, shares: 100, date: todayStr(), note: '' })
const action = useAction()
const lot = computed(() => (props.stock ? lotSize(props.stock.symbol) : 100))

watch(
  () => props.modelValue,
  (isOpen) => {
    if (!isOpen || !props.stock) return
    Object.assign(form, { price: props.stock.last, shares: lot.value, date: todayStr(), note: '' })
    action.error.value = ''
  },
)

const amount = computed(() => (form.price && form.shares ? form.price * form.shares : 0))
const after = computed(() => {
  const held = props.holding ? props.holding.shares : 0
  const cost = props.holding ? props.holding.cost_price : 0
  const total = held + (form.shares || 0)
  return total ? { shares: total, cost: (cost * held + amount.value) / total } : null
})
const outOfLimits = computed(() => {
  const s = props.stock
  if (!s || !s.limit_up || !form.price || form.date !== todayStr()) return false
  return form.price > Number(s.limit_up) + 1e-9 || form.price < Number(s.limit_down) - 1e-9
})
const futureDate = (d) => d.getTime() > Date.now()

async function submit() {
  const r = await action.run(() =>
    post('/api/book/buy', { symbol: props.stock.symbol, price: String(form.price ?? ''), shares: String(form.shares ?? ''), date: form.date, note: form.note }),
  )
  if (r) {
    open.value = false
    emit('done')
  }
}
</script>

<template>
  <el-dialog v-model="open" :title="`买入 ${stock ? stock.name || stock.symbol : ''}`" width="440px" append-to-body>
    <el-form v-if="stock" label-position="top" @submit.prevent="submit">
      <p class="muted head">
        <span class="num">{{ stock.symbol }}</span> · 现价 <b class="num">{{ fmtNum(stock.last) }}</b>
        <template v-if="stock.limit_up"> · 涨停 {{ fmtNum(stock.limit_up) }} / 跌停 {{ fmtNum(stock.limit_down) }}</template>
      </p>
      <el-row :gutter="12">
        <el-col :span="12">
          <el-form-item label="买入价">
            <el-input-number v-model="form.price" :min="0.01" :step="0.01" :precision="2" :controls="false" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item :label="`买入数量（股，${lot} 的整数倍）`">
            <el-input-number v-model="form.shares" :min="lot" :step="lot" :step-strictly="true" :precision="0" style="width: 100%" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="成交日期">
        <el-date-picker v-model="form.date" type="date" value-format="YYYY-MM-DD" :clearable="false" :disabled-date="futureDate" style="width: 100%" />
      </el-form-item>
      <el-form-item label="备注（可选）"><el-input v-model="form.note" maxlength="200" /></el-form-item>
      <div class="summary">
        <span>成交金额 <b class="num">{{ fmtMoney(amount) }}</b></span>
        <span v-if="after">买入后持仓 <b class="num">{{ after.shares }}</b> 股，成本价 <b class="num">{{ fmtPrice(after.cost) }}</b><template v-if="holding">（加权平均）</template></span>
      </div>
      <el-alert v-if="outOfLimits" title="买入价超出今日涨跌停范围，实盘无法按这个价格成交；如果是补录历史成交，请把成交日期改成当天之前。" type="warning" show-icon :closable="false" />
      <el-alert v-if="action.error.value" :title="action.error.value" type="error" show-icon :closable="false" style="margin-top: 8px" />
      <p class="muted">成本价不含手续费。当天买入的股，当天不能卖（T+1）。</p>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="action.loading.value" @click="submit">确认买入</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.head { margin: 0 0 12px; }
.summary { display: flex; flex-direction: column; gap: 4px; margin: 4px 0 10px; font-size: 13.5px; }
</style>
