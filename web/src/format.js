// 数字/涨跌格式化。A 股惯例：涨红跌绿（颜色见 styles/tokens.css 的 .rise/.fall/.flat）。

export function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  return Number(v).toFixed(digits)
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
