<script setup>
import { computed, reactive, watch } from 'vue'
import { post } from '../../api'
import { useAction } from '../../composables/useAction'
import { direction, fmtMoney, fmtNum, fmtPrice, lotSize, todayStr } from '../../format'

// 从持仓卖出：数量不能超过「可卖」（T+1：当天买入的股当天不能卖）。卖出不改变剩余持仓的成本价，只记已实现盈亏。
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  holding: { type: Object, default: null }, // 持仓行：symbol / name / shares / sellable / cost_price / last
})
const emit = defineEmits(['update:modelValue', 'done'])
const open = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })

const form = reactive({ price: null, shares: 0, date: todayStr() })
const action = useAction()
const lot = computed(() => (props.holding ? lotSize(props.holding.symbol) : 100))
// 今天的可卖数量；补录历史卖出时后端按那一天重新校验，这里只是给个合理的上限提示。
const sellable = computed(() => (props.holding ? (form.date === todayStr() ? props.holding.sellable : props.holding.shares) : 0))

watch(
  () => props.modelValue,
  (isOpen) => {
    if (!isOpen || !props.holding) return
    Object.assign(form, { price: props.holding.last, shares: props.holding.sellable, date: todayStr() })
    action.error.value = ''
  },
)

// 快捷比例：向下取整到最小交易单位，至少一手；「全部」= 全部可卖（可以是零头）。
function pick(fraction) {
  const n = fraction === 1 ? sellable.value : Math.max(lot.value, Math.floor((sellable.value * fraction) / lot.value) * lot.value)
  form.shares = Math.min(n, sellable.value)
}
const pnl = computed(() => (form.price && form.shares && props.holding ? (form.price - props.holding.cost_price) * form.shares : null))
const pnlPct = computed(() => (form.price && props.holding ? (form.price / props.holding.cost_price - 1) * 100 : null))
const futureDate = (d) => d.getTime() > Date.now()

async function submit() {
  const r = await action.run(() =>
    post('/api/book/sell', { symbol: props.holding.symbol, price: String(form.price ?? ''), shares: String(form.shares ?? ''), date: form.date }),
  )
  if (r) {
    open.value = false
    emit('done')
  }
}
</script>

<template>
  <el-dialog v-model="open" :title="`卖出 ${holding ? holding.name || holding.symbol : ''}`" width="440px" append-to-body>
    <el-form v-if="holding" label-position="top" @submit.prevent="submit">
      <p class="muted head">
        <span class="num">{{ holding.symbol }}</span> · 持仓 <b class="num">{{ holding.shares }}</b> 股，今日可卖 <b class="num">{{ holding.sellable }}</b> 股 · 成本价
        <b class="num">{{ fmtPrice(holding.cost_price) }}</b> · 现价 <b class="num">{{ fmtNum(holding.last) }}</b>
      </p>
      <el-alert
        v-if="form.date === todayStr() && holding.sellable < holding.shares"
        :title="holding.sellable ? `T+1：今天买入的 ${holding.shares - holding.sellable} 股今天不能卖，最多可卖 ${holding.sellable} 股。` : 'T+1：这只股票是今天买入的，今天不能卖，明天开盘后才能卖出。'"
        type="warning" show-icon :closable="false" style="margin-bottom: 12px"
      />
      <el-row :gutter="12">
        <el-col :span="12">
          <el-form-item label="卖出价">
            <el-input-number v-model="form.price" :min="0.01" :step="0.01" :precision="2" :controls="false" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item :label="`卖出数量（股，${lot} 的整数倍）`">
            <el-input-number v-model="form.shares" :min="lot" :max="Math.max(sellable, lot)" :step="lot" :precision="0" :disabled="!sellable" style="width: 100%" />
          </el-form-item>
        </el-col>
      </el-row>
      <div class="quick">
        <el-button size="small" :disabled="!sellable" @click="pick(1 / 3)">1/3</el-button>
        <el-button size="small" :disabled="!sellable" @click="pick(1 / 2)">1/2</el-button>
        <el-button size="small" :disabled="!sellable" @click="pick(1)">全部可卖</el-button>
      </div>
      <el-form-item label="成交日期">
        <el-date-picker v-model="form.date" type="date" value-format="YYYY-MM-DD" :clearable="false" :disabled-date="futureDate" style="width: 100%" />
      </el-form-item>
      <div class="summary">
        <span>成交金额 <b class="num">{{ fmtMoney(form.price && form.shares ? form.price * form.shares : 0) }}</b></span>
        <span v-if="sellable && pnl !== null">
          预计已实现盈亏
          <b class="num" :class="direction(pnl)">{{ pnl > 0 ? '+' : '' }}{{ fmtNum(pnl) }}（{{ pnlPct > 0 ? '+' : '' }}{{ fmtNum(pnlPct) }}%）</b>
        </span>
        <span v-if="sellable && form.shares >= holding.shares" class="muted">这是全部持仓：卖出后这只股票会从持仓中移除（成交记录保留）。</span>
      </div>
      <el-alert v-if="action.error.value" :title="action.error.value" type="error" show-icon :closable="false" />
      <p class="muted">不含手续费和印花税。卖出不改变剩余持仓的成本价。</p>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="action.loading.value" :disabled="!sellable" @click="submit">确认卖出</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.head { margin: 0 0 12px; }
.quick { display: flex; gap: 8px; margin: -6px 0 12px; }
.summary { display: flex; flex-direction: column; gap: 4px; margin: 4px 0 10px; font-size: 13.5px; }
</style>
