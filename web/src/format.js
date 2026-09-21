// 数字/涨跌格式化。A 股惯例：涨红跌绿（颜色见 styles/tokens.css 的 .rise/.fall/.flat）。

export function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  return Number(v).toFixed(digits)
}

// 成本价/成交价：最多 3 位小数，末尾的 0 去掉但至少留两位（1252.570 → 1252.57，11.9 → 11.90，11.905 → 11.905）。
export function fmtPrice(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  const s = Number(v).toFixed(3)
  return s.endsWith('0') ? s.slice(0, -1) : s
}

export function fmtPct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  // 先四舍五入再判符号：-0.001 显示成 “0.00%”，而不是 “-0.00%”。
  const n = Number(Number(v).toFixed(digits))
  if (n === 0) return (0).toFixed(digits) + '%'
  return (n > 0 ? '+' : '') + n.toFixed(digits) + '%'
}

export function fmtMoney(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  return '¥' + Math.round(Number(v)).toLocaleString('zh-CN')
}

export function fmtWan(v) {
  if (v === null || v === undefined) return '—'
  return (Number(v) / 1e4).toFixed(0) + '万'
}

// 资金流（元）→ 带符号的「亿/万」：+2.25亿 / -4000万。缺失是破折号，不是 0。
export function fmtFlow(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  const n = Number(v)
  const sign = n > 0 ? '+' : n < 0 ? '-' : ''
  const a = Math.abs(n)
  return a >= 1e8 ? `${sign}${(a / 1e8).toFixed(2)}亿` : `${sign}${(a / 1e4).toFixed(0)}万`
}

// 涨跌方向：>0 涨（红）、<0 跌（绿）、其余持平。四舍五入到显示精度后为 0 的，按持平处理，
// 避免出现「-0.00%」却被染成绿色这种误导。
export function direction(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return 'flat'
  const rounded = Number(Number(v).toFixed(digits))
  if (rounded > 0) return 'rise'
  if (rounded < 0) return 'fall'
  return 'flat'
}

export function arrow(dir) {
  return dir === 'rise' ? '▲' : dir === 'fall' ? '▼' : '–'
}

// 'YYYY-MM-DDTHH:MM:SS+08:00' → 'YYYY-MM-DD HH:MM'
export function fmtDateTime(iso) {
  if (!iso) return '—'
  return String(iso).slice(0, 16).replace('T', ' ')
}

// 告警/抽屉里的一行资金摘要，与服务端 money_flow.flow_line 同一口径：取不到的项省略，不写 0。
export function flowLine(flow, book) {
  const parts = []
  if (flow) {
    parts.push(`主力 ${fmtFlow(flow.main)}（近30分钟 ${fmtFlow(flow.main_30m)}）`)
    parts.push(`大单 ${fmtFlow(flow.large)}`)
  }
  if (book && book.outer_pct !== null && book.outer_pct !== undefined) parts.push(`外盘占比 ${Number(book.outer_pct).toFixed(1)}%`)
  if (book && book.bid_ask_ratio !== null && book.bid_ask_ratio !== undefined) {
    const r = Number(book.bid_ask_ratio)
    parts.push(`委比 ${r > 0 ? '+' : ''}${r.toFixed(1)}%`)
  }
  return parts.join('｜')
}

// 本地日期 'YYYY-MM-DD'（不经过 UTC，北京时间凌晨不会变成前一天）。
export function todayStr(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

// 沪深A股最小交易单位：科创板 200 股，其余 100 股（和 server/portfolio_book.lot_size 一致）。
export function lotSize(symbol) {
  return String(symbol).startsWith('sh688') ? 200 : 100
}

// 成交额/市值（元，不带符号）→ 万亿 / 亿 / 万：2031513487711 → 2.03万亿，9468亿，5878万。缺失是破折号。
export function fmtAmount(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  const n = Math.abs(Number(v))
  if (n >= 1e12) return (n / 1e12).toFixed(2) + '万亿'
  if (n >= 1e9) return Math.round(n / 1e8) + '亿'
  if (n >= 1e8) return (n / 1e8).toFixed(2) + '亿'
  return (n / 1e4).toFixed(0) + '万'
}

// 相比上一交易日的放量/缩量文案：'缩量 456亿（-2.2%）'。两个数任一缺失返回空串。
export function fmtVolumeTrend(delta, pct) {
  if (delta === null || delta === undefined || Number.isNaN(Number(delta))) return ''
  const word = delta > 0 ? '放量' : delta < 0 ? '缩量' : '持平'
  if (delta === 0) return word
  return `${word} ${fmtAmount(delta)}${pct === null || pct === undefined ? '' : `（${fmtPct(pct, 1)}）`}`
}

// 连板文案：1 → 首板，3 → 3连板。
export function fmtBoards(n) {
  return !n || n <= 1 ? '首板' : `${n}连板`
}

// 'YYYY-MM-DD' → 'MM-DD'（同一页里日期很多时用短的）。
export function shortDate(iso) {
  return iso ? String(iso).slice(5, 10) : '—'
}
