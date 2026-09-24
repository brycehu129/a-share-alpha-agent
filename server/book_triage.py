"""亏损仓分诊：把跌破成本的持仓，按均线结构分成"趋势已坏 / 仍是回调 / 区间震荡"。

**只影响措辞和是否给补仓提示，绝不能把"彻底卖出"的结论降级。** 卖不卖是 book_verdict 的止损/
止盈判断该管的事（book_levels.effective_stop 已经把成本/保本/移动止损取最紧的一条），分诊管不着；
分诊只回答"如果要补仓，这只股票现在的结构像不像一次正常回调"，供 sentinel.add_signal（补仓提示）
做前置门槛。纯函数，没有 I/O。

四档：
- **healthy**：现价在成本之上，谈不上"亏损仓分诊"。
- **broken**（趋势已坏）：跌破 MA20 且 MA20 本身在下行；或同时跌破 MA20 与 MA60；或跌破 20 日低点。
  三条命中任意一条就判 broken，不再看后面的条件——这是最保守的档，宁可错判一次正常回调，也不要
  把趋势已坏的票当成"回调"去建议补仓。
- **pullback**（仍是回调）：现价仍在 MA20 之上（浮亏但没跌破均线）；或者虽在 MA20 下方，但 MA20
  仍在上行、现价仍在 MA60 之上，且回撤幅度还没到一个止损幅度（stop_pct，book_levels 算好的那个）。
- **range**（区间震荡）：近 20 日波动率不低（ATR% 达标）、20 日收盘区间足够宽、MA20 走平——像是在
  一个箱体里来回，不是趋势下行也不是标准回调。
- 都不满足时退回 **broken**：均线数据齐全但形态既不像回调也不像区间震荡，保守起见按最坏的档处理。
"""

RANGE_ATR_MIN_PCT = 3.0      # 区间震荡：近 20 日 ATR% 门槛（这只股票自己的波动率，百分数）
RANGE_WIDTH_MIN_PCT = 12.0   # 20 日收盘区间宽度门槛（(最高-最低)/最低，百分数）
RANGE_SLOPE_MAX_PCT = 1.0    # MA20 斜率"接近走平"的容忍度（5 日变化，百分数）
BUCKET_LABEL = {'broken': '趋势已坏', 'pullback': '仍是回调', 'range': '区间震荡', 'healthy': '现价在成本之上'}


def _ma20_five_days_ago(closes):
    """closes 是按日期升序的收盘价（不含今天），至少要有 25 根才谈得上"5 天前的 MA20"。"""
    if len(closes) < 25:
        return None
    window = closes[:-5]
    return sum(window[-20:]) / 20


def triage(cost, last, bars, facts, stop_pct, atr_pct):
    """cost/last：成本价、现价。bars：日线（升序，含 close，不含今天，和 book_levels.exit_levels
    的 before 纪律一致）。facts：live_check.price_facts 的结果（今天的 ma20/ma60/low20_close）。
    stop_pct/atr_pct：book_levels.exit_levels 已经解析好的值（含典型值兜底），这里不重新算一遍。

    返回 {'bucket', 'reasons'}；均线数据不够时 bucket=None，明说缺数据，不猜。"""
    if last > cost:
        return {'bucket': 'healthy', 'reasons': ['现价在成本价之上']}
    ma20, ma60 = facts.get('ma20'), facts.get('ma60')
    if ma20 is None:
        return {'bucket': None, 'reasons': ['本地日线不足 20 根，算不出均线，无法分诊']}

    closes = [float(b['close']) for b in bars]
    ma20_prior = _ma20_five_days_ago(closes)
    below20 = last < ma20
    below60 = ma60 is not None and last < ma60
    low20 = facts.get('low20_close')

    if below20 and ma20_prior is not None and ma20 < ma20_prior:
        return {'bucket': 'broken', 'reasons': ['跌破 MA20 %.2f，且 MA20 本身在下行（5 日前 %.2f）' % (ma20, ma20_prior)]}
    if below20 and below60:
        return {'bucket': 'broken', 'reasons': ['同时跌破 MA20 %.2f 与 MA60 %.2f' % (ma20, ma60)]}
    if low20 is not None and last < low20:
        return {'bucket': 'broken', 'reasons': ['跌破 20 日低点 %.2f' % low20]}

    if not below20:
        return {'bucket': 'pullback', 'reasons': ['现价 %.2f 仍在 MA20 %.2f 之上，只是浮亏' % (last, ma20)]}
    if ma20_prior is not None and ma20 >= ma20_prior and not below60:
        drawdown = (cost - last) / cost
        if drawdown < stop_pct:
            return {'bucket': 'pullback', 'reasons': [
                'MA20 仍在上行、现价仍在 MA60 之上，回撤 %.1f%% 还没到一个止损幅度（%.1f%%）' % (
                    drawdown * 100, stop_pct * 100)]}

    if len(closes) >= 20 and atr_pct * 100 >= RANGE_ATR_MIN_PCT:
        window = closes[-20:]
        width_pct = (max(window) - min(window)) / min(window) * 100
        if width_pct >= RANGE_WIDTH_MIN_PCT and ma20_prior:
            slope_pct = abs(ma20 / ma20_prior - 1) * 100
            if slope_pct <= RANGE_SLOPE_MAX_PCT:
                return {'bucket': 'range', 'reasons': [
                    '近 20 日波动率 %.1f%%、区间宽度 %.1f%%，MA20 基本走平（5 日变化 %.2f%%）' % (
                        atr_pct * 100, width_pct, slope_pct)]}

    return {'bucket': 'broken', 'reasons': ['跌破 MA20，且不满足回调或区间震荡的条件，保守按趋势已坏处理']}
