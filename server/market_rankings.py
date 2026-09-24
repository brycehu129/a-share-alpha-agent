"""盘中板块强度与个股主力资金排行。只取公开行情，不落盘、不生成建议。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlencode

from collect_quotes import CST
import market_review

ENDPOINT = 'https://push2.eastmoney.com/api/qt/clist/get'
FIELDS = 'f12,f14,f2,f3,f62,f184,f104,f105,f106'
BOARD_FILTERS = {'industry': 'm:90+t:2+f:!50', 'concept': 'm:90+t:3+f:!50'}
STOCK_FILTER = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
DEFAULT_SIZE = 10


class RankingError(ValueError):
    pass


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float('inf') else None


def _diff(payload, label):
    try:
        rows = payload['data']['diff']
    except (KeyError, TypeError) as exc:
        raise RankingError('%s响应结构异常' % label) from exc
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        raise RankingError('%s响应结构异常（diff 不是列表）' % label)
    return rows


def parse_sectors(payload, kind):
    """解析板块并计算涨幅、上涨占比、主力净流入占比三个维度。"""
    out = []
    for raw in _diff(payload, kind):
        code, name = str(raw.get('f12') or ''), str(raw.get('f14') or '').strip()
        change, main_ratio = _num(raw.get('f3')), _num(raw.get('f184'))
        up, down, flat = _num(raw.get('f104')), _num(raw.get('f105')), _num(raw.get('f106'))
        if not code or not name or None in (change, main_ratio, up, down, flat):
            continue
        total = up + down + flat
        if total <= 0:
            continue
        out.append({'code': code, 'name': name, 'change_pct': round(change, 2),
                    'up': int(up), 'down': int(down), 'flat': int(flat),
                    'up_pct': round(up / total * 100, 2),
                    'main_net': _num(raw.get('f62')), 'main_net_pct': round(main_ratio, 2)})
    if not out:
        raise RankingError('%s没有可用板块数据' % kind)
    return out


def _percentiles(rows, field):
    """含并列平均名次的 0..100 百分位；单行时取 50。"""
    values = sorted(row[field] for row in rows)
    if len(values) == 1:
        return {values[0]: 50.0}
    result = {}
    for value in set(values):
        indices = [i for i, candidate in enumerate(values) if candidate == value]
        result[value] = sum(indices) / len(indices) / (len(values) - 1) * 100
    return result


def rank_sectors(rows, size=DEFAULT_SIZE):
    pct = {field: _percentiles(rows, field) for field in ('change_pct', 'up_pct', 'main_net_pct')}
    scored = []
    for row in rows:
        score = (pct['change_pct'][row['change_pct']] * .4 +
                 pct['up_pct'][row['up_pct']] * .3 +
                 pct['main_net_pct'][row['main_net_pct']] * .3)
        scored.append({**row, 'strength': round(score, 1)})
    strong = sorted(scored, key=lambda x: (-x['strength'], x['name']))[:size]
    weak = sorted(scored, key=lambda x: (x['strength'], x['name']))[:size]
    return {'strong': strong, 'weak': weak, 'total': len(scored)}


def parse_stocks(payload, label):
    out = []
    for raw in _diff(payload, label):
        code, name = str(raw.get('f12') or ''), str(raw.get('f14') or '').strip()
        price, change, main_net, ratio = (_num(raw.get(k)) for k in ('f2', 'f3', 'f62', 'f184'))
        symbol = market_review.symbol_of_code(code)
        if not symbol or symbol.startswith('bj') or not name or None in (price, change, main_net, ratio):
            continue
        out.append({'symbol': symbol, 'code': code, 'name': name, 'price': round(price, 3),
                    'change_pct': round(change, 2), 'main_net': main_net, 'main_net_pct': round(ratio, 2)})
    return out


def _fetch(params, http=None):
    query = {'pn': 1, 'np': 1, 'fltt': 2, 'invt': 2, 'fields': FIELDS, **params}
    return market_review.get_json(ENDPOINT + '?' + urlencode(query), http)


def fetch_sector(kind, http=None):
    payload = _fetch({'pz': 500, 'po': 1, 'fid': 'f3', 'fs': BOARD_FILTERS[kind]}, http)
    return rank_sectors(parse_sectors(payload, kind))


def fetch_stocks(direction, http=None, size=DEFAULT_SIZE):
    payload = _fetch({'pz': max(size * 2, 20), 'po': 1 if direction == 'inflow' else 0,
                      'fid': 'f62', 'fs': STOCK_FILTER}, http)
    rows = parse_stocks(payload, direction)
    rows.sort(key=lambda x: (-x['main_net'], x['code']) if direction == 'inflow'
              else (x['main_net'], x['code']))
    return rows[:size]


def current_rankings(now=None, http=None):
    """四组并发、分别降级；调用即现取，不设服务端缓存。"""
    now = now or datetime.now(CST)
    jobs = {'industry': lambda: fetch_sector('industry', http),
            'concept': lambda: fetch_sector('concept', http),
            'inflow': lambda: fetch_stocks('inflow', http),
            'outflow': lambda: fetch_stocks('outflow', http)}
    values, errors = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {key: pool.submit(job) for key, job in jobs.items()}
        for key, future in futures.items():
            try:
                values[key] = future.result()
            except Exception as exc:
                values[key] = None
                errors[key] = str(exc)[:200]
    return {'fetched_at': now.isoformat(),
            'sectors': {'industry': values['industry'], 'concept': values['concept']},
            'stocks': {'inflow': values['inflow'], 'outflow': values['outflow']},
            'errors': errors,
            'source_note': '东方财富按成交额分档估算；主力＝超大单＋大单，非交易所披露。'}
