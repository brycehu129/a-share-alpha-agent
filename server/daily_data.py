"""Small, explicit data-access sample; no universe selection or predictions."""
import hashlib
import json
import time
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from collect_quotes import CST, parse_quotes

STOCKS = ['sz000001', 'sh600000', 'sz000651', 'sh600519', 'sz300750']
INDICES = ['sh000001', 'sh000300', 'sh000852']


def request(url):
    with urlopen(Request(url, headers={'User-Agent': 'AlphaResearchCollector/0.3'}), timeout=20) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('response exceeds limit')
    return raw


def parse_daily(raw, symbol, adjustment, today):
    body = json.loads(raw)
    if body.get('code') != 0:
        raise ValueError('daily provider error')
    block = body['data'][symbol]
    key = 'qfqday' if adjustment == 'qfq' else 'day'
    # Never silently label an unadjusted fallback as adjusted.
    rows = block.get(key)
    if not isinstance(rows, list) or not rows:
        raise ValueError('missing requested series: ' + key)
    result = []
    previous = None
    for row in rows:
        day = date.fromisoformat(row[0])
        if day > today or (previous is not None and day <= previous):
            raise ValueError('future, duplicate or unordered daily date')
        numbers = [Decimal(str(v)) for v in row[1:6]]
        if len(numbers) != 5 or any(not n.is_finite() or n < 0 for n in numbers):
            raise ValueError('invalid OHLCV')
        opening, close, high, low, volume = numbers
        if low <= 0 or not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError('inconsistent OHLC')
        result.append(dict(zip(['date', 'open', 'close', 'high', 'low', 'volume_raw'],
                               [day.isoformat()] + [str(n) for n in numbers])))
        previous = day
    return result


def collect_datasets(now=None):
    now = now or datetime.now(CST)
    output = {'version': 'access-0.3', 'fetched_at': now.isoformat(), 'stocks': [],
              'series': [], 'errors': [], 'calendar': {}, 'status': 'success'}
    quote_url = 'https://qt.gtimg.cn/q=' + ','.join(STOCKS)
    try:
        raw = request(quote_url)
        output['stocks'] = parse_quotes(raw, STOCKS, datetime.now(CST))
        output['quote_source'] = quote_url
        output['quote_response_sha256'] = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, KeyError, ArithmeticError) as exc:
        output['errors'].append('stock quotes: ' + str(exc))
    for symbol in INDICES + STOCKS:
        for adjustment in (['none', 'qfq'] if symbol in STOCKS else ['none']):
            url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urlencode({
                'param': f'{symbol},day,,{now.date().isoformat()},320,' + ('qfq' if adjustment == 'qfq' else '')})
            item = {'symbol': symbol, 'adjustment': adjustment, 'source_url': url}
            try:
                raw = request(url)
                item['response_sha256'] = hashlib.sha256(raw).hexdigest()
                item['bars'] = parse_daily(raw, symbol, adjustment, now.date())
                item['status'] = 'success'
            except (OSError, ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
                item.update(status='failed', bars=[], error=str(exc))
                output['errors'].append(symbol + '/' + adjustment + ': ' + str(exc))
            output['series'].append(item)
            time.sleep(0.4)
    calendar_series = [s for s in output['series'] if s['symbol'] in INDICES and s['status'] == 'success']
    # Intersection is evidence of observed sessions, not an authoritative future calendar.
    observed = sorted(set.intersection(*[{b['date'] for b in s['bars']} for s in calendar_series])) if len(calendar_series) == len(INDICES) else []
    output['calendar'] = {'kind': 'observed_index_sessions', 'dates': observed,
                          'coverage_start': observed[0] if observed else None,
                          'coverage_end': observed[-1] if observed else None,
                          'today': 'observed' if now.date().isoformat() in observed else 'unknown',
                          'future_sessions': 'unknown', 'official_calendar_verified': False}
    if output['errors'] or not observed:
        output['status'] = 'partial'
    return output


def dataset_markdown(data):
    lines = ['', '## 个股与历史日线访问验证', '',
             '固定5只股票仅用于验证数据访问，不是选股结果。', '',
             '| 代码 | 复权方式 | 日线条数 | 起止日期 | 状态 |', '|---|---|---:|---|---|']
    for s in data['series']:
        bars = s['bars']
        period = bars[0]['date'] + ' → ' + bars[-1]['date'] if bars else '—'
        lines.append(f'| {s["symbol"]} | {s["adjustment"]} | {len(bars)} | {period} | {s["status"]} |')
    cal = data['calendar']
    lines += ['', f'个股报价获取：{len(data["stocks"])} / 5。日线数据访问状态：{data["status"]}。', '',
              f'3个指数共同出现的历史日期：{len(cal["dates"])} 个；当天状态：{cal["today"]}。', '',
              '日历仅为行情中观察到的历史交易日期；未来交易日、缺失日期是否休市均未知，不能用于自动调度。', '',
              '每组日线最多请求320条。未复权与前复权分开保存；前复权值可能随以后除权变化，不能直接作为严格时点回测数据。', '',
              '当天日线可能尚未完成；尚未校验停牌、除权事件与交易所最终收盘。成交量保留源值，未统一单位。', '',
              '所有源地址、获取时间、原始响应摘要和解析后日线保存在同编号 JSON 的 datasets 字段。', '']
    if data['errors']:
        lines += ['存在采集失败项，请查看 JSON 中 errors；未以其他复权口径替代。', '']
    return '\n'.join(lines)
