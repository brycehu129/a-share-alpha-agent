import { describe, expect, it } from 'vitest'
import { arrow, fmtTs, direction, fmtAmount, fmtBoards, fmtDateTime, fmtFlow, flowLine, fmtMoney, fmtNum, fmtPct, fmtPrice, fmtVolumeTrend, fmtWan, lotSize, shortDate, todayStr } from './format'

describe('fmtPct', () => {
  it('positive values get a plus sign', () => expect(fmtPct(1.056)).toBe('+1.06%'))
  it('negative values keep the minus sign', () => expect(fmtPct(-1.294)).toBe('-1.29%'))
  it('values that round to zero show no sign at all', () => {
    expect(fmtPct(-0.001)).toBe('0.00%')
    expect(fmtPct(0.004)).toBe('0.00%')
    expect(fmtPct(0)).toBe('0.00%')
  })
  it('respects digits', () => expect(fmtPct(24.04, 1)).toBe('+24.0%'))
  it('missing values are a dash, not NaN', () => {
    expect(fmtPct(null)).toBe('—')
    expect(fmtPct(undefined)).toBe('—')
    expect(fmtPct('abc')).toBe('—')
  })
})

describe('direction / arrow (A股：涨红跌绿)', () => {
  it('up / down / flat', () => {
    expect(direction(0.5)).toBe('rise')
    expect(direction(-0.5)).toBe('fall')
    expect(direction(0)).toBe('flat')
  })
  it('a value that rounds to zero is flat, so it is never painted green as a loss', () => {
    expect(direction(-0.001)).toBe('flat')
    expect(direction(-0.001, 4)).toBe('fall')
  })
  it('missing is flat', () => expect(direction(null)).toBe('flat'))
  it('arrows match direction', () => {
    expect(arrow('rise')).toBe('▲')
    expect(arrow('fall')).toBe('▼')
    expect(arrow('flat')).toBe('–')
  })
})

describe('other formatters', () => {
  it('fmtNum', () => {
    expect(fmtNum(3.14159, 3)).toBe('3.142')
    expect(fmtNum(null)).toBe('—')
  })
  it('fmtMoney', () => {
    expect(fmtMoney(99883.4)).toBe('¥99,883')
    expect(fmtMoney(undefined)).toBe('—')
  })
  it('fmtWan', () => expect(fmtWan(12345678)).toBe('1235万'))
  it('fmtDateTime trims seconds and timezone', () => {
    expect(fmtDateTime('2026-09-21T10:24:00+08:00')).toBe('2026-09-21 10:24')
    expect(fmtDateTime('')).toBe('—')
  })
})

describe('fmtFlow（资金流：元 → 亿/万，带符号）', () => {
  it('large amounts are in 亿 with two decimals', () => {
    expect(fmtFlow(-224507630)).toBe('-2.25亿')
    expect(fmtFlow(1.5e8)).toBe('+1.50亿')
  })
  it('smaller amounts are in 万', () => {
    expect(fmtFlow(56000000)).toBe('+5600万')
    expect(fmtFlow(-4e7)).toBe('-4000万')
  })
  it('zero has no sign', () => expect(fmtFlow(0)).toBe('0万'))
  it('missing is a dash, never zero or NaN', () => {
    expect(fmtFlow(null)).toBe('—')
    expect(fmtFlow(undefined)).toBe('—')
    expect(fmtFlow('abc')).toBe('—')
  })
})

describe('flowLine（资金摘要一行）', () => {
  const flow = { main: -224507630, main_30m: -4e7, large: -148816241 }
  it('shows flow and order-book facts', () => {
    expect(flowLine(flow, { outer_pct: 52.9, bid_ask_ratio: 5.18 })).toBe('主力 -2.25亿（近30分钟 -4000万）｜大单 -1.49亿｜外盘占比 52.9%｜委比 +5.2%')
  })
  it('omits whatever is missing instead of printing zero', () => {
    expect(flowLine(null, { outer_pct: 52.9 })).toBe('外盘占比 52.9%')
    expect(flowLine(flow, {})).toBe('主力 -2.25亿（近30分钟 -4000万）｜大单 -1.49亿')
    expect(flowLine(null, null)).toBe('')
    expect(flowLine(null, { bid_ask_ratio: -3 })).toBe('委比 -3.0%')
  })
})

describe('todayStr / lotSize', () => {
  it('formats the local date with zero padding', () => expect(todayStr(new Date(2026, 0, 5, 23, 59))).toBe('2026-01-05'))
  it('does not shift to the previous day just after local midnight', () => expect(todayStr(new Date(2026, 8, 21, 0, 5))).toBe('2026-09-21'))
  it('star board lots are 200, everything else 100', () => {
    expect(lotSize('sh688981')).toBe(200)
    expect(lotSize('sh600519')).toBe(100)
    expect(lotSize('sz300458')).toBe(100)
  })
})

describe('fmtPrice', () => {
  it('keeps at least two decimals and at most three', () => {
    expect(fmtPrice(1252.57)).toBe('1252.57')
    expect(fmtPrice(11.9)).toBe('11.90')
    expect(fmtPrice(11.905)).toBe('11.905')
    expect(fmtPrice(10)).toBe('10.00')
  })
  it('missing is a dash', () => expect(fmtPrice(null)).toBe('—'))
})

describe('fmtAmount', () => {
  it('picks the unit by magnitude', () => {
    expect(fmtAmount(2031513487711)).toBe('2.03万亿')
    expect(fmtAmount(946819124309)).toBe('9468亿')
    expect(fmtAmount(555268254)).toBe('5.55亿')
    expect(fmtAmount(58783810)).toBe('5878万')
  })
  it('is unsigned and tolerant of missing values', () => {
    expect(fmtAmount(-45586807979)).toBe('456亿')
    expect(fmtAmount(null)).toBe('—')
    expect(fmtAmount(undefined)).toBe('—')
    expect(fmtAmount('x')).toBe('—')
  })
})

describe('fmtVolumeTrend', () => {
  it('shrink / expand / flat / missing', () => {
    expect(fmtVolumeTrend(-45586807979, -2.2)).toBe('缩量 456亿（-2.2%）')
    expect(fmtVolumeTrend(1.2e11, 5)).toBe('放量 1200亿（+5.0%）')
    expect(fmtVolumeTrend(0, 0)).toBe('持平')
    expect(fmtVolumeTrend(null, null)).toBe('')
    expect(fmtVolumeTrend(1e10, null)).toBe('放量 100亿')
  })
})

describe('fmtBoards / shortDate', () => {
  it('board labels', () => {
    expect(fmtBoards(1)).toBe('首板')
    expect(fmtBoards(undefined)).toBe('首板')
    expect(fmtBoards(3)).toBe('3连板')
  })
  it('short date', () => {
    expect(shortDate('2026-09-21')).toBe('09-21')
    expect(shortDate(null)).toBe('—')
  })
})

describe('fmtTs', () => {
  it('keeps seconds and drops fraction and timezone', () => {
    expect(fmtTs('2026-09-21T16:14:00+08:00')).toBe('2026-09-21 16:14:00')
    expect(fmtTs('2026-09-21T22:48:39.806799+08:00')).toBe('2026-09-21 22:48:39')
  })
  it('missing is a dash', () => {
    expect(fmtTs(null)).toBe('—')
    expect(fmtTs('')).toBe('—')
  })
})
