"""腾讯分时数据（当日逐分钟）与均价线 VWAP。Python 3.9+，只用标准库。

接口免 token：web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh600519
每行 "HHMM 价格 累计成交量 累计成交额(元)"，例如 "0931 1259.18 529 66704818.26"。

三个实测出来的坑，都在这里处理掉：

1. **成交量单位不统一。** 沪深主板/创业板的累计成交量是"手"(100股)，**科创板(688)是"股"**。
   实测 sh688061 收盘行：累计量 2500240、累计额 113037080、价格区间 44.44–46.31——
   按"手"算 VWAP = 0.452（差了 100 倍），按"股"算 = 45.21（落在价格区间里）。
   硬套"手"会让所有科创板票的"均价线上下"判断全错，所以不硬编码板块，
   而是用"哪种单位算出的 VWAP 落在当日价格区间内"来推断；两种都不落在区间内就报错，
   不猜。输出统一折算成"股"，并保留推断出的原始单位。
2. **收盘后还有行。** 1501–1530 是盘后固定价格交易时段（有些板块才有），累计量不再变化。
   分时研判只看连续竞价 0930–1130 / 1300–1500，其余行丢弃并计数。
3. **只有分钟收盘价，没有分钟内最高/最低。** 所以这里的 high_close/low_close 是"各分钟
   收盘价里的最高/最低"，会比真实日内高低点窄一点。要真实日内极值用实时行情里的
   high/low 字段，别拿这个冒充。
"""
import hashlib
import json
import re
from datetime import datetime

from collect_quotes import CST
from daily_data import request

ENDPOINT = 'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code='
UNIT_TOLERANCE = 0.05   # VWAP 允许略超出当日价格区间 5%（区间来自分钟收盘价，本来就比真实极值窄）


class MinuteError(ValueError):
    """分时数据解析或校验失败。宁可报错，也不返回一份单位可能错了的均价线。"""


def in_continuous_session(hhmm):
    return '0930' <= hhmm <= '1130' or '1300' <= hhmm <= '1500'


def infer_volume_unit(low, high, cum_volume, cum_amount):
    """返回 ('hand'|'share', vwap_per_share)。

    两个候选：按"手"算 amount/(vol*100)，按"股"算 amount/vol。哪个落在当日价格区间
    (±容差)内就是哪个。区间相差 100 倍，两个同时落在区间内在实际数据里不会发生；
    如果发生了或者一个都不落，说明数据有问题，报错而不是猜。"""
    if cum_volume <= 0:
        raise MinuteError('累计成交量为0，无法推断单位')
    lo, hi = low * (1 - UNIT_TOLERANCE), high * (1 + UNIT_TOLERANCE)
    as_hand = cum_amount / (cum_volume * 100)
    as_share = cum_amount / cum_volume
    fits = [(unit, v) for unit, v in (('hand', as_hand), ('share', as_share)) if lo <= v <= hi]
    if len(fits) != 1:
        raise MinuteError('无法确定成交量单位：按手VWAP=%.4f，按股VWAP=%.4f，价格区间[%.2f,%.2f]'
                          % (as_hand, as_share, low, high))
    return fits[0]


def parse_minute(raw, symbol, fetched_at=None):
    fetched_at = fetched_at or datetime.now(CST)
    try:
        body = json.loads(raw)
        if body.get('code') != 0:
            raise MinuteError('分时接口返回错误码 %s' % body.get('code'))
        block = body['data'][symbol]['data']
        trade_date_raw, rows = block['date'], block['data']
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, MinuteError):
            raise
        raise MinuteError('分时响应结构异常: %s' % type(exc).__name__) from exc
    if not re.fullmatch(r'\d{8}', str(trade_date_raw)) or not isinstance(rows, list):
        raise MinuteError('分时日期或行列表异常')

    kept, dropped, prev = [], 0, None
    for line in rows:
        parts = str(line).split()
        if len(parts) != 4 or not re.fullmatch(r'\d{4}', parts[0]):
            raise MinuteError('分时行格式异常: %r' % line)
        hhmm = parts[0]
        if not in_continuous_session(hhmm):
            dropped += 1
            continue
        try:
            price, vol, amt = float(parts[1]), int(parts[2]), float(parts[3])
        except ValueError as exc:
            raise MinuteError('分时行数值异常: %r' % line) from exc
        if not (price > 0 and vol >= 0 and amt >= 0):
            raise MinuteError('分时行数值越界: %r' % line)
        if prev is not None:
            if hhmm <= prev['t']:
                raise MinuteError('分时时间不严格递增: %s 之后是 %s' % (prev['t'], hhmm))
            if vol < prev['cv'] or amt < prev['ca']:
                raise MinuteError('累计成交量/额出现回退: %s' % hhmm)
        prev = {'t': hhmm, 'p': price, 'cv': vol, 'ca': amt}
        kept.append(prev)
    if not kept:
        raise MinuteError('没有连续竞价时段的分时数据')

    prices = [r['p'] for r in kept]
    low, high = min(prices), max(prices)
    last = kept[-1]
    unit, _ = infer_volume_unit(low, high, last['cv'], last['ca']) if last['cv'] > 0 else (None, None)
    factor = {'hand': 100, 'share': 1, None: 1}[unit]

    bars, pv, pa = [], 0, 0.0
    for r in kept:
        shares = r['cv'] * factor
        bars.append({
            't': r['t'], 'price': r['p'],
            'minute_volume_shares': shares - pv,
            'cum_volume_shares': shares, 'cum_amount': r['ca'],
            'vwap': round(r['ca'] / shares, 4) if shares > 0 else None,
        })
        pv, pa = shares, r['ca']
    trade_date = '%s-%s-%s' % (trade_date_raw[:4], trade_date_raw[4:6], trade_date_raw[6:])
    return {
        'symbol': symbol, 'trade_date': trade_date, 'volume_unit': unit,
        'bars': bars, 'last_time': last['t'], 'last': last['p'], 'open': kept[0]['p'],
        'high_close': high, 'low_close': low,
        'vwap': bars[-1]['vwap'], 'cum_volume_shares': bars[-1]['cum_volume_shares'],
        'cum_amount': last['ca'],
        'complete': last['t'] >= '1500',
        'dropped_rows': dropped, 'fetched_at': fetched_at.isoformat(),
        'response_sha256': hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode()).hexdigest(),
    }


def fetch_minute(symbol, now=None):
    if not re.fullmatch(r'(sh|sz)\d{6}', symbol):
        raise MinuteError('分时数据只支持沪深个股/指数代码: ' + symbol)
    now = now or datetime.now(CST)
    try:
        raw = request(ENDPOINT + symbol)
    except OSError as exc:
        raise MinuteError('分时请求失败: %s' % type(exc).__name__) from exc
    return parse_minute(raw, symbol, now)


def is_today(result, now=None):
    """接口在非交易日/盘前返回的是上一个交易日的数据；调用方必须核对，别把昨天的
    走势当成今天的。"""
    now = now or datetime.now(CST)
    return result['trade_date'] == now.date().isoformat()
