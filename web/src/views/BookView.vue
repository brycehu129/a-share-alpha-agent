<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { get, post } from '../api'
import { useLoad } from '../composables/useLoad'
import { useAction } from '../composables/useAction'
import { fmtMoney, fmtNum, fmtPrice, direction } from '../format'
import RiseFall from '../components/RiseFall.vue'
import StatGrid from '../components/StatGrid.vue'
import StatTile from '../components/StatTile.vue'
import StockPicker from '../components/book/StockPicker.vue'
import VerdictCell from '../components/book/VerdictCell.vue'
import BuyDialog from '../components/book/BuyDialog.vue'
import SellDialog from '../components/book/SellDialog.vue'
import SymbolAlertsDrawer from '../components/book/SymbolAlertsDrawer.vue'
import DayAlertsDrawer from '../components/book/DayAlertsDrawer.vue'
import RulesDrawer from '../components/book/RulesDrawer.vue'

// 录入照券商来：「自选股」用搜索加入，从这里「买入」；「持仓」里「卖出」。成本价 = 买入价，
// 每一行的结论（具备买入信号 / 可做T / 可暂时卖出 / 彻底卖出）由系统规则给出，不由你声明。
// tab 写进 URL（?tab=holding），刷新后回到原处。
const route = useRoute()
const router = useRouter()
const TABS = ['watch', 'holding']
const tab = ref(TABS.includes(route.query.tab) ? route.query.tab : 'watch')
watch(tab, (t) => router.replace({ query: t === 'watch' ? {} : { tab: t } }))
watch(
  () => route.query.tab,
  (t) => {
    const next = TABS.includes(t) ? t : 'watch'
    if (next !== tab.value) tab.value = next
  },
)

const { data, loading, error, reload } = useLoad(() => get('/api/book'))
const refresh = () => reload({ silent: true })

const alertDrawer = reactive({ open: false, symbol: '', name: '' })
const dayDrawer = ref(false)
const rulesDrawer = reactive({ open: false, section: 'holding' })
function openRules(section) {
  Object.assign(rulesDrawer, { open: true, section: section || (tab.value === 'watch' ? 'watch' : 'holding') })
}
function openAlerts(row) {
  Object.assign(alertDrawer, { open: true, symbol: row.symbol, name: row.name || row.symbol })
}
const alertType = (row) => (row.alerts && row.alerts.urgent ? 'danger' : row.alerts && row.alerts.count ? 'warning' : '')

// 买入：只能从自选股行发起；如果已持有，把持仓行一并传给对话框算加权平均成本。
const buyDialog = reactive({ open: false, stock: null })
const heldRow = computed(() => (buyDialog.stock && data.value ? data.value.holdings.find((h) => h.symbol === buyDialog.stock.symbol) || null : null))
function openBuy(row) {
  Object.assign(buyDialog, { open: true, stock: row })
}
const sellDialog = reactive({ open: false, holding: null })
function openSell(row) {
  Object.assign(sellDialog, { open: true, holding: row })
}
function afterTrade() {
  refresh()
}

const removing = useAction()
async function remove(kind, row) {
  const hint = kind === 'holding' ? '这只是纠正录入错误用的：不会产生成交记录，也不计入盈亏。正常减仓/清仓请用「卖出」。\n\n' : ''
  try {
    await ElMessageBox.confirm(`${hint}确定从${kind === 'holding' ? '持仓' : '自选'}中删除 ${row.name || row.symbol}（${row.symbol}）？`, '删除确认', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch (e) {
    return
  }
  const r = await removing.run(() => post(`/api/book/${kind}/remove`, { symbol: row.symbol }))
  if (r) refresh()
}

const signed = (v, digits = 2) => (v > 0 ? '+' : '') + fmtNum(v, digits)
const sideLabel = { buy: '买入', sell: '卖出' }
</script>

<template>
  <div>
    <div class="page-head">
      <h1>持仓与自选股</h1>
      <div class="meta">
        <span v-if="data && data.session">行情：{{ data.session }}</span>
        <el-button :icon="Refresh" round :loading="loading" @click="reload()">刷新</el-button>
        <el-button round @click="openRules()">判断规则</el-button>
        <el-button round @click="dayDrawer = true">全天告警汇总</el-button>
      </div>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" @click="reload()">重试</el-button>
    </el-alert>
    <el-alert v-if="removing.error.value" :title="removing.error.value" type="error" show-icon :closable="false" style="margin-bottom: 16px" />
    <el-alert v-if="data && data.trouble" :title="data.trouble" type="warning" show-icon :closable="false" style="margin-bottom: 16px" />

    <div v-loading="loading && !data" style="min-height: 200px">
      <el-tabs v-if="data" v-model="tab" class="book-tabs">
        <!-- ============ 自选股 ============ -->
        <el-tab-pane :label="`自选股（${data.watchlist.length}）`" name="watch">
          <div class="stack">
            <el-card shadow="never">
              <template #header><div class="card-title"><span>添加自选</span><span class="sub">输入代码、名称或拼音首字母，选中后自动带出行情</span></div></template>
              <StockPicker @added="refresh" />
            </el-card>

            <el-card shadow="never">
              <template #header><div class="card-title"><span>我的自选</span><span class="sub">系统按策略的买入形态实时判断，有信号会在这里和企业微信提示 · <el-link type="primary" :underline="false" @click="openRules('watch')">查看判断规则</el-link></span></div></template>
              <el-table :data="data.watchlist" empty-text="还没有自选股：在上面搜索并加入。">
                <el-table-column label="股票" min-width="140">
                  <template #default="{ row }">
                    <span class="stock-name">{{ row.name || row.symbol }}</span><span class="stock-code num">{{ row.symbol }}</span>
                    <span v-if="row.held_shares" class="stock-code">已持有 {{ row.held_shares }} 股</span>
                  </template>
                </el-table-column>
                <el-table-column label="现价" width="90" align="right"><template #default="{ row }"><span class="num">{{ fmtNum(row.last) }}</span></template></el-table-column>
                <el-table-column label="涨跌" width="100" align="right"><template #default="{ row }"><RiseFall :value="row.change_pct" /></template></el-table-column>
                <el-table-column label="系统结论" min-width="210"><template #default="{ row }"><VerdictCell :verdict="row.verdict" /></template></el-table-column>
                <el-table-column prop="note" label="备注" min-width="110" />
                <el-table-column label="告警" width="88" align="center">
                  <template #default="{ row }"><el-button size="small" :type="alertType(row)" plain @click="openAlerts(row)">告警 {{ row.alerts ? row.alerts.count : 0 }}</el-button></template>
                </el-table-column>
                <el-table-column label="操作" width="132" align="center" fixed="right">
                  <template #default="{ row }">
                    <el-button size="small" type="primary" :disabled="!row.last" @click="openBuy(row)">买入</el-button>
                    <el-button size="small" plain @click="remove('watch', row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>
          </div>
        </el-tab-pane>

        <!-- ============ 持仓 ============ -->
        <el-tab-pane :label="`持仓（${data.holdings.length}）`" name="holding">
          <div class="stack">
            <StatGrid v-if="data.summary" class="summary-grid">
              <StatTile label="持仓市值" :value="fmtMoney(data.summary.value)" />
              <StatTile label="持仓成本" :value="fmtMoney(data.summary.cost)" />
              <StatTile label="浮动盈亏" :tone="direction(data.summary.pnl)">{{ signed(data.summary.pnl, 0) }}（{{ signed(data.summary.pnl_pct) }}%）</StatTile>
              <StatTile label="今日盈亏" :tone="direction(data.summary.today_pnl)">{{ signed(data.summary.today_pnl, 0) }}</StatTile>
            </StatGrid>

            <el-card shadow="never">
              <template #header><div class="card-title"><span>我的持仓</span><span class="sub">成本价 = 买入价（多次买入取加权平均）· 不含手续费 · 结论由系统规则给出 · <el-link type="primary" :underline="false" @click="openRules('holding')">查看判断规则（含彻底卖出）</el-link></span></div></template>
              <el-table :data="data.holdings">
                <template #empty>
                  还没有持仓。到「自选股」里选一只，点「买入」。
                  <el-button type="primary" link @click="tab = 'watch'">去自选股</el-button>
                </template>
                <el-table-column label="股票" min-width="130">
                  <template #default="{ row }"><span class="stock-name">{{ row.name || row.symbol }}</span><span class="stock-code num">{{ row.symbol }}</span></template>
                </el-table-column>
                <el-table-column label="持仓 / 可卖" width="104" align="right">
                  <template #default="{ row }">
                    <span class="num">{{ row.shares }}</span>
                    <span class="stock-code num" :title="row.sellable < row.shares ? 'T+1：今天买入的股今天不能卖' : ''">可卖 {{ row.sellable }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="成本价" width="96" align="right"><template #default="{ row }"><span class="num">{{ fmtPrice(row.cost_price) }}</span></template></el-table-column>
                <el-table-column label="现价" width="120" align="right">
                  <template #default="{ row }"><span class="num">{{ fmtNum(row.last) }}</span><span class="stock-code"><RiseFall :value="row.change_pct" bare /></span></template>
                </el-table-column>
                <el-table-column label="浮动盈亏" width="120" align="right">
                  <template #default="{ row }">
                    <span class="num" :class="direction(row.pnl)">{{ row.pnl === null ? '—' : signed(row.pnl, 0) }}</span>
                    <span class="stock-code"><RiseFall :value="row.pct" /></span>
                  </template>
                </el-table-column>
                <el-table-column label="系统结论" min-width="210"><template #default="{ row }"><VerdictCell :verdict="row.verdict" /></template></el-table-column>
                <el-table-column label="告警" width="88" align="center">
                  <template #default="{ row }"><el-button size="small" :type="alertType(row)" plain @click="openAlerts(row)">告警 {{ row.alerts ? row.alerts.count : 0 }}</el-button></template>
                </el-table-column>
                <el-table-column label="操作" width="132" align="center" fixed="right">
                  <template #default="{ row }">
                    <el-button size="small" type="primary" :disabled="!row.last" @click="openSell(row)">卖出</el-button>
                    <el-button size="small" plain @click="remove('holding', row)">纠错删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>

            <el-card shadow="never">
              <template #header><div class="card-title"><span>成交记录（最近 {{ data.trades.length }} 笔）</span><span class="sub">每一次买入/卖出都记在这里；T+1 的可卖数量就是按它算的</span></div></template>
              <el-table :data="data.trades" empty-text="还没有成交记录。" size="small">
                <el-table-column prop="date" label="成交日期" width="110" />
                <el-table-column label="股票" min-width="130"><template #default="{ row }">{{ row.name || row.symbol }} <span class="num muted">{{ row.symbol }}</span></template></el-table-column>
                <el-table-column label="方向" width="70"><template #default="{ row }"><el-tag :type="row.side === 'buy' ? 'primary' : 'warning'" effect="plain" size="small">{{ sideLabel[row.side] }}</el-tag></template></el-table-column>
                <el-table-column label="价格" width="90" align="right"><template #default="{ row }"><span class="num">{{ fmtPrice(row.price) }}</span></template></el-table-column>
                <el-table-column label="数量" width="80" align="right"><template #default="{ row }"><span class="num">{{ row.shares }}</span></template></el-table-column>
                <el-table-column label="金额" width="110" align="right"><template #default="{ row }"><span class="num">{{ fmtMoney(row.price * row.shares) }}</span></template></el-table-column>
                <el-table-column label="已实现盈亏" width="120" align="right">
                  <template #default="{ row }"><span v-if="row.side === 'sell'" class="num" :class="direction(row.realized_pnl)">{{ signed(row.realized_pnl, 0) }}</span><span v-else class="muted">—</span></template>
                </el-table-column>
                <el-table-column prop="note" label="备注" min-width="100" />
              </el-table>
            </el-card>
          </div>
        </el-tab-pane>
      </el-tabs>
      <p v-if="data" class="muted" style="margin-top: 16px">持仓数据只保存在这台服务器本地，不会进入公开的 Git 仓库，也不会发送给券商。系统只提醒、永远不会自动下单；结论是规则给出的提示，不构成投资建议。</p>
    </div>

    <BuyDialog v-model="buyDialog.open" :stock="buyDialog.stock" :holding="heldRow" @done="afterTrade" />
    <SellDialog v-model="sellDialog.open" :holding="sellDialog.holding" @done="afterTrade" />
    <SymbolAlertsDrawer v-model="alertDrawer.open" :symbol="alertDrawer.symbol" :name="alertDrawer.name" />
    <DayAlertsDrawer v-model="dayDrawer" />
    <RulesDrawer v-model="rulesDrawer.open" :section="rulesDrawer.section" />
  </div>
</template>

<style scoped>
.book-tabs :deep(.el-tabs__header) { margin-bottom: 16px; }
.book-tabs :deep(.el-tabs__item) { font-weight: 600; }
.summary-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
@media (max-width: 640px) {
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
