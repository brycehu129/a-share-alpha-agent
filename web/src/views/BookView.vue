<script setup>
import { reactive, ref } from 'vue'
import { get, post } from '../api'
import { useLoad } from '../composables/useLoad'
import { useAction } from '../composables/useAction'
import { fmtMoney, fmtNum, direction } from '../format'
import RiseFall from '../components/RiseFall.vue'
import SymbolAlertsDrawer from '../components/book/SymbolAlertsDrawer.vue'
import DayAlertsDrawer from '../components/book/DayAlertsDrawer.vue'

const { data, loading, error, reload } = useLoad(() => get('/api/book'))

const holdingForm = reactive(emptyHolding())
const watchForm = reactive(emptyWatch())
function emptyHolding() {
  return { symbol: '', name: '', shares: '', cost_price: '', opened_on: '', note: '', hold_type: '', stop_price: '', target_price: '', t_base_shares: '' }
}
function emptyWatch() {
  return { symbol: '', name: '', intent: 'watch', note: '', buy_low: '', buy_high: '' }
}

// 哨兵不再是独立页面：每一行的「告警」按钮打开这只股票的告警详情（含资金流），页头按钮看全天汇总。
const alertDrawer = reactive({ open: false, symbol: '', name: '' })
const dayDrawer = ref(false)
function openAlerts(row) {
  Object.assign(alertDrawer, { open: true, symbol: row.symbol, name: row.name || row.symbol })
}
const alertType = (row) => (row.alerts && row.alerts.urgent ? 'danger' : row.alerts && row.alerts.count ? 'warning' : '')

const saveHolding = useAction()
const saveWatch = useAction()
const removing = useAction()

async function onSaveHolding() {
  const r = await saveHolding.run(() => post('/api/book/holding', { ...holdingForm }))
  if (r) {
    Object.assign(holdingForm, emptyHolding())
    reload({ silent: true })
  }
}
async function onSaveWatch() {
  const r = await saveWatch.run(() => post('/api/book/watch', { ...watchForm }))
  if (r) {
    Object.assign(watchForm, emptyWatch())
    reload({ silent: true })
  }
}
async function remove(kind, row) {
  try {
    await ElMessageBox.confirm(`确定从${kind === 'holding' ? '持仓' : '自选'}中删除 ${row.name || row.symbol}（${row.symbol}）？`, '删除确认', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch (e) {
    return
  }
  const r = await removing.run(() => post(`/api/book/${kind}/remove`, { symbol: row.symbol }))
  if (r) reload({ silent: true })
}
</script>

<template>
  <div>
    <div class="page-head">
      <h1>持仓与自选股</h1>
      <el-button @click="dayDrawer = true">全天告警汇总</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" @click="reload()">重试</el-button>
    </el-alert>
    <el-alert v-if="removing.error.value" :title="removing.error.value" type="error" show-icon :closable="false" style="margin-bottom: 16px" />
    <el-alert v-if="data && data.trouble" :title="data.trouble" type="warning" show-icon :closable="false" style="margin-bottom: 16px" />

    <div v-loading="loading && !data" class="stack" style="min-height: 200px">
      <template v-if="data">
        <!-- 持仓 -->
        <el-card shadow="never">
          <template #header><div class="card-title"><span>我的持仓（{{ data.holdings.length }}）</span></div></template>
          <p v-if="data.summary" class="muted" style="margin-top: 0">
            持仓总成本 {{ fmtMoney(data.summary.cost) }}，当前市值 {{ fmtMoney(data.summary.value) }}，浮动盈亏
            <b class="num" :class="direction(data.summary.pnl)">{{ data.summary.pnl > 0 ? '+' : '' }}{{ fmtNum(data.summary.pnl) }}（{{ data.summary.pnl_pct > 0 ? '+' : '' }}{{ fmtNum(data.summary.pnl_pct) }}%）</b>。按最近一次实时报价估算，不含费用。
          </p>
          <el-table :data="data.holdings" empty-text="还没有录入持仓。">
            <el-table-column label="股票" min-width="130">
              <template #default="{ row }"><span class="stock-name">{{ row.name || row.symbol }}</span><span class="stock-code num">{{ row.symbol }}</span></template>
            </el-table-column>
            <el-table-column label="数量" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.shares }}</span></template></el-table-column>
            <el-table-column label="成本价" width="100" align="right"><template #default="{ row }"><span class="num">{{ row.cost_price }}</span></template></el-table-column>
            <el-table-column label="现价" width="100" align="right"><template #default="{ row }"><span class="num">{{ fmtNum(row.last) }}</span></template></el-table-column>
            <el-table-column label="浮动" width="110" align="right"><template #default="{ row }"><RiseFall :value="row.pct" /></template></el-table-column>
            <el-table-column prop="declared" label="你的声明 / 备注" min-width="180" />
            <el-table-column label="告警" width="92" align="center">
              <template #default="{ row }"><el-button size="small" :type="alertType(row)" plain @click="openAlerts(row)">告警 {{ row.alerts ? row.alerts.count : 0 }}</el-button></template>
            </el-table-column>
            <el-table-column label="" width="80" align="center">
              <template #default="{ row }"><el-button size="small" type="danger" plain @click="remove('holding', row)">删除</el-button></template>
            </el-table-column>
          </el-table>

          <el-divider />
          <h3 class="sub-title">录入 / 更新持仓</h3>
          <el-form label-position="top" @submit.prevent="onSaveHolding">
            <el-row :gutter="12">
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="股票代码"><el-input v-model="holdingForm.symbol" placeholder="600519 或 sh600519" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="名称（可选）"><el-input v-model="holdingForm.name" placeholder="贵州茅台" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="持仓数量（股）"><el-input v-model="holdingForm.shares" inputmode="numeric" placeholder="100 的整数倍" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="成本价"><el-input v-model="holdingForm.cost_price" inputmode="decimal" placeholder="1300.00" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="建仓日期（可选）"><el-input v-model="holdingForm.opened_on" placeholder="2026-09-01" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="备注（可选）"><el-input v-model="holdingForm.note" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8">
                <el-form-item label="持有类型（可选）">
                  <el-select v-model="holdingForm.hold_type" clearable placeholder="不声明" style="width: 100%">
                    <el-option v-for="o in data.hold_types" :key="o.value" :label="o.label" :value="o.value" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="止损价（可选）"><el-input v-model="holdingForm.stop_price" placeholder="不填就不提醒止损" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="目标价（可选）"><el-input v-model="holdingForm.target_price" placeholder="不填就不提醒目标" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="做T底仓（股，可选）"><el-input v-model="holdingForm.t_base_shares" placeholder="0 = 不做T" /></el-form-item></el-col>
            </el-row>
            <p class="muted">系统不知道你为什么买，所以止损/目标/做T底仓必须由你自己声明；不填的那类提醒就不触发。</p>
            <el-alert v-if="saveHolding.error.value" :title="saveHolding.error.value" type="error" show-icon :closable="false" />
            <div class="actions">
              <el-button type="primary" native-type="submit" :loading="saveHolding.loading.value">保存持仓</el-button>
              <span class="muted">同一只股票再次保存即覆盖。</span>
            </div>
          </el-form>
        </el-card>

        <!-- 自选 -->
        <el-card shadow="never">
          <template #header><div class="card-title"><span>自选股（{{ data.watchlist.length }}）</span></div></template>
          <el-table :data="data.watchlist" empty-text="还没有自选股。">
            <el-table-column label="股票" min-width="130">
              <template #default="{ row }"><span class="stock-name">{{ row.name || row.symbol }}</span><span class="stock-code num">{{ row.symbol }}</span></template>
            </el-table-column>
            <el-table-column prop="intent_label" label="类型" width="100" />
            <el-table-column label="现价" width="100" align="right"><template #default="{ row }"><span class="num">{{ fmtNum(row.last) }}</span></template></el-table-column>
            <el-table-column label="涨跌" width="110" align="right"><template #default="{ row }"><RiseFall :value="row.change_pct" /></template></el-table-column>
            <el-table-column prop="note" label="备注" min-width="160" />
            <el-table-column label="告警" width="92" align="center">
              <template #default="{ row }"><el-button size="small" :type="alertType(row)" plain @click="openAlerts(row)">告警 {{ row.alerts ? row.alerts.count : 0 }}</el-button></template>
            </el-table-column>
            <el-table-column label="" width="80" align="center">
              <template #default="{ row }"><el-button size="small" type="danger" plain @click="remove('watch', row)">删除</el-button></template>
            </el-table-column>
          </el-table>

          <el-divider />
          <h3 class="sub-title">加入自选</h3>
          <el-form label-position="top" @submit.prevent="onSaveWatch">
            <el-row :gutter="12">
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="股票代码"><el-input v-model="watchForm.symbol" placeholder="000001" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="名称（可选）"><el-input v-model="watchForm.name" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8">
                <el-form-item label="关注类型">
                  <el-select v-model="watchForm.intent" style="width: 100%">
                    <el-option v-for="o in data.intents" :key="o.value" :label="o.label" :value="o.value" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="备注（可选）"><el-input v-model="watchForm.note" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="买入区间下沿（可选）"><el-input v-model="watchForm.buy_low" /></el-form-item></el-col>
              <el-col :xs="24" :sm="12" :md="8"><el-form-item label="买入区间上沿（可选）"><el-input v-model="watchForm.buy_high" /></el-form-item></el-col>
            </el-row>
            <el-alert v-if="saveWatch.error.value" :title="saveWatch.error.value" type="error" show-icon :closable="false" />
            <div class="actions"><el-button type="primary" native-type="submit" :loading="saveWatch.loading.value">加入自选</el-button></div>
          </el-form>
        </el-card>

        <p class="muted">持仓数据只保存在这台服务器本地，不会进入公开的 Git 仓库，也不会发送给券商。系统永远不会自动下单。</p>
      </template>
    </div>

    <SymbolAlertsDrawer v-model="alertDrawer.open" :symbol="alertDrawer.symbol" :name="alertDrawer.name" />
    <DayAlertsDrawer v-model="dayDrawer" />
  </div>
</template>

<style scoped>
.sub-title { margin: 0 0 12px; font-size: 15px; }
</style>
