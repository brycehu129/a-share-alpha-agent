export function isIntradayReason(reason) {
  return typeof reason === 'string' && (reason.includes('上穿盘中支撑') || reason.includes('跌回盘中阻力'))
}

export function firstIntradayReason(reasons) {
  return Array.isArray(reasons) ? reasons.find(isIntradayReason) || null : null
}