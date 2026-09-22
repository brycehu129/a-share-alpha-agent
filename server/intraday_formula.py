"""Minute-level helpers translated from a common intraday chart formula.

This module is deliberately advisory-only: it computes support/resistance, VWAP
context, and a price-anchored MACD state for the current session, but it does
not decide exits for the core holding strategy.
"""


def _f(value):
    return float(value) if value not in (None, '') else None


def _ema(values, period):
    alpha = 2.0 / (period + 1.0)
    out = []
    current = None
    for value in values:
        current = value if current is None else current + alpha * (value - current)
        out.append(current)
    return out


def _support_resistance(previous_close, high_value, low_value):
    if previous_close is None or high_value is None or low_value is None:
        return None, None
    upper = max(previous_close, high_value)
    lower = min(previous_close, low_value)
    span = upper - lower
    return lower + span * 0.5 / 9.0, lower + span * 8.0 / 9.0


def macd(prices):
    if not prices:
        return {}
    ema12 = _ema(prices, 12)
    ema26 = _ema(prices, 26)
    dif = [fast - slow for fast, slow in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    macd1 = [10.0 * (fast - slow) for fast, slow in zip(dif, dea)]
    macd2 = _ema(macd1, 2)
    return {
        'dif': dif[-1], 'dea': dea[-1], 'macd1': macd1[-1], 'macd2': macd2[-1],
        'bullish': macd1[-1] >= macd2[-1], 'above_zero': macd1[-1] >= 0,
    }


def intraday_facts(quote, minute):
    """Return advisory intraday facts translated from the chart formula.

    The original script assumes chart-native state such as rolling intraday
    bars. Here we reconstruct only what the current backend can observe safely:
    session VWAP, support/resistance derived from previous close and today's
    range, a minute-close MACD, and support/resistance crosses on the observed
    minute-close series.
    """
    if not minute:
        return {}
    prices = [_f(bar.get('price')) for bar in minute.get('bars', [])]
    prices = [price for price in prices if price is not None]
    if not prices:
        return {}

    last = _f(quote.get('last'))
    previous_close = _f(quote.get('previous_close'))
    quote_high = _f(quote.get('high'))
    quote_low = _f(quote.get('low'))
    vwap = _f(minute.get('vwap'))
    current_support, current_resistance = _support_resistance(previous_close, quote_high, quote_low)

    support_series, resistance_series = [], []
    running_high = previous_close if previous_close is not None else prices[0]
    running_low = previous_close if previous_close is not None else prices[0]
    for price in prices:
        running_high = max(running_high, price)
        running_low = min(running_low, price)
        support, resistance = _support_resistance(previous_close if previous_close is not None else price,
                                                  running_high, running_low)
        support_series.append(support)
        resistance_series.append(resistance)

    out = {
        'xg_high_480': round(max(prices[-480:]), 4),
        'support': round(current_support, 4) if current_support is not None else None,
        'resistance': round(current_resistance, 4) if current_resistance is not None else None,
    }
    if vwap is not None:
        out['formula_vwap'] = round(vwap, 4)
        if last is not None:
            out['vs_formula_vwap_pct'] = round((last / vwap - 1.0) * 100.0, 4)

    macd_state = macd(prices)
    if macd_state:
        out.update({key: round(value, 4) if isinstance(value, float) else value for key, value in macd_state.items()})
        if macd_state['bullish'] and macd_state['above_zero']:
            out['macd_state'] = 'bullish_above_zero'
        elif macd_state['bullish']:
            out['macd_state'] = 'bullish_below_zero'
        elif macd_state['above_zero']:
            out['macd_state'] = 'bearish_above_zero'
        else:
            out['macd_state'] = 'bearish_below_zero'

    if len(prices) >= 2 and support_series[-2] is not None and resistance_series[-2] is not None:
        out['buy_cross_support'] = prices[-2] < support_series[-2] and prices[-1] >= support_series[-1]
        out['sell_cross_resistance'] = prices[-2] > resistance_series[-2] and prices[-1] <= resistance_series[-1]
    else:
        out['buy_cross_support'] = False
        out['sell_cross_resistance'] = False
    return {key: value for key, value in out.items() if value is not None}