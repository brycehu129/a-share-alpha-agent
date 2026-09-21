import { describe, expect, it } from 'vitest'
import { arrow, direction, fmtDateTime, fmtMoney, fmtNum, fmtPct, fmtWan } from './format'

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
