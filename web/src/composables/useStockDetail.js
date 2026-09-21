import { reactive } from 'vue'

// 看板上点任何一只股票（涨停池、龙虎榜、次日关注……）都打开同一个详情抽屉：
// 各处只调 openStock(symbol, name)，抽屉由 DashboardView 挂一份、读这里的状态。
export const stockDetail = reactive({ open: false, symbol: '', name: '' })

export function openStock(symbol, name = '') {
  if (!symbol) return
  stockDetail.symbol = symbol
  stockDetail.name = name
  stockDetail.open = true
}
