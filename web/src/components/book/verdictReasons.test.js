import { describe, expect, it } from 'vitest'
import { firstIntradayReason, isIntradayReason } from './verdictReasons'

describe('verdictReasons', () => {
  it('recognizes intraday support and resistance prompts', () => {
    expect(isIntradayReason('上穿盘中支撑 9.70（现价 9.85，MACD 转多），可留意低吸回补观察')).toBe(true)
    expect(isIntradayReason('跌回盘中阻力 10.30 下方（现价 10.10，MACD 转弱），可留意盘中减仓观察')).toBe(true)
    expect(isIntradayReason('触及系统止损位 9.50')).toBe(false)
  })

  it('returns the first intraday reason when present', () => {
    expect(firstIntradayReason(['未触发任何卖出或做T条件', '上穿盘中支撑 9.70（现价 9.85，MACD 转多），可留意分时企稳观察'])).toContain('上穿盘中支撑')
    expect(firstIntradayReason(['触及系统止盈位 11.00'])).toBe(null)
  })
})