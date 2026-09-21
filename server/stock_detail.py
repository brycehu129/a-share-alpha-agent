"""个股详情（看板里点击股票弹出的抽屉）：行情快照、日 K + 均线、公司资料/概念、龙虎榜席位。只用标准库。

四块各自取、各自失败：任何一块取不到，其它块照常返回，失败原因放在 errors 里由前端显示。
行情快照复用 live_quote（腾讯），日 K 用腾讯前复权日线，公司资料/概念用东财 F10，龙虎榜席位用 market_review。
"""
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

import live_quote
import market_review
from collect_quotes import CST

KLINE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
PROFILE_URL = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
FETCH_BARS = 220          # 多取一些，保证展示区间最左边的 MA60 也有 60 根可算
SHOW_BARS = 120
MA_WINDOWS = (5, 10, 20, 60)
CACHE_SECONDS = 60

_lock = threading.Lock()
_cache = {}


def secucode(symbol):
    m = re.fullmatch(r'(sh|sz|bj)(\d{6})', symbol or '')
    if not m:
        raise market_review.ReviewError('股票代码格式不对: %s' % symbol)
    return '%s.%s' % (m.group(2), m.group(1).upper())


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def moving_average(closes, window):
    """简单移动平均；不足 window 根的位置是 None（不用短窗口硬凑，免得均线起点失真）。"""
    out, total = [], 0.0
    for i, c in enumerate(closes):
        total += c
        if i >= window:
            total -= closes[i - window]
        out.append(round(total / window, 3) if i >= window - 1 else None)
    return out


def parse_kline(payload, symbol):
    """腾讯日线 → [{date, open, close, low, high, volume, ma5..ma60}]，volume 单位手。"""
    try:
        rows = payload['data'][symbol].get('qfqday') or payload['data'][symbol]['day']
    except (KeyError, TypeError, AttributeError) as exc:
        raise market_review.ReviewError('日 K 响应形状不对') from exc
    bars, last_date = [], None
    for r in rows:
        try:
            date, o, c, h, low, vol = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (ValueError, IndexError, TypeError):
            continue
        if last_date is not None and date <= last_date:
            continue                      # 重复或乱序的日期丢掉，不让图上出现回退
        if not (low <= min(o, c) and max(o, c) <= h) or low <= 0:
            continue                      # OHLC 自相矛盾的一根不画
        bars.append({'date': date, 'open': o, 'close': c, 'low': low, 'high': h, 'volume': vol})
        last_date = date
    if len(bars) < 2:
        raise market_review.ReviewError('日 K 数据不足')
    closes = [b['close'] for b in bars]
    for w in MA_WINDOWS:
        for b, v in zip(bars, moving_average(closes, w)):
            b['ma%d' % w] = v
    return bars[-SHOW_BARS:]


def fetch_kline(symbol, http=None, today=None):
    today = today or datetime.now(CST).date().isoformat()
    q = urlencode({'param': '%s,day,,%s,%d,qfq' % (symbol, today, FETCH_BARS)}, safe=',')
    return parse_kline(market_review.get_json('%s?%s' % (KLINE_URL, q), http), symbol)


def _split_tags(text):
    return [t.strip() for t in re.split(r'[,，]', text or '') if t.strip()]


def parse_profile(payload):
    try:
        row = payload['result']['data'][0]
    except (KeyError, TypeError, IndexError) as exc:
        raise market_review.ReviewError('公司资料为空') from exc
    industry = [x for x in (row.get('EM2016') or '').split('-') if x]
    return {'full_name': row.get('ORG_NAME'), 'industry_path': industry, 'region': row.get('REGIONBK'),
            'concepts': _split_tags(row.get('BLGAINIAN')), 'address': row.get('ADDRESS'), 'website': row.get('ORG_WEB'),
            'phone': row.get('ORG_TEL'), 'email': row.get('ORG_EMAIL'), 'intro': (row.get('ORG_PROFIE') or '').strip()[:400] or None}


def fetch_profile(symbol, http=None):
    q = urlencode({'reportName': 'RPT_F10_ORG_BASICINFO', 'columns': 'ALL', 'filter': '(SECUCODE="%s")' % secucode(symbol)})
    return parse_profile(market_review.get_json('%s?%s' % (PROFILE_URL, q), http))


def fetch_quote(symbol, snapshot=None):
    snap = snapshot or live_quote.snapshot([symbol])
    quotes = snap.get('quotes') or []
    if not quotes:
        raise market_review.ReviewError('行情为空')
    q = quotes[0]
    keys = ('name', 'last', 'previous_close', 'open', 'high', 'low', 'change_pct', 'quote_at', 'amount_wan', 'volume_raw',
            'turnover_pct', 'pe', 'pb', 'float_cap_yi', 'total_cap_yi', 'limit_up', 'limit_down', 'volume_ratio', 'amplitude_pct')
    return {k: q.get(k) for k in keys}


def review_context(symbol, review):
    """这只股票在最近一次盘后复盘里的位置：在哪些池子里、龙虎榜行、涨停/炸板细节。"""
    if not review:
        return None
    ctx = {'date': review.get('date'), 'pools': {}, 'lhb': None, 'lhb_date': (review.get('lhb') or {}).get('date')}
    for kind, pool in (review.get('pools') or {}).items():
        if not pool:
            continue
        row = next((r for r in pool['rows'] if r['symbol'] == symbol), None)
        if row:
            ctx['pools'][kind] = row
    lhb = review.get('lhb')
    if lhb:
        ctx['lhb'] = next((r for r in lhb['rows'] if r['symbol'] == symbol), None)
    watch = (review.get('next_day_watch') or {}).get('items') or []
    ctx['watch'] = next((w for w in watch if w.get('symbol') == symbol), None)
    return ctx


def detail(symbol, review=None, http=None, snapshot=None, force=False):
    secucode(symbol)                                       # 先校验格式，失败直接抛
    key = (symbol, (review or {}).get('trade_date'), ((review or {}).get('lhb') or {}).get('trade_date'))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    out = {'symbol': symbol, 'errors': {}}
    steps = {'quote': lambda: fetch_quote(symbol, snapshot), 'kline': lambda: fetch_kline(symbol, http),
             'profile': lambda: fetch_profile(symbol, http)}
    lhb = (review or {}).get('lhb')
    if lhb and any(r['symbol'] == symbol for r in lhb['rows']):
        steps['seats'] = lambda: market_review.fetch_seats(symbol[2:], lhb['trade_date'], http)
    for name, fn in steps.items():
        try:
            out[name] = fn()
        except Exception as exc:                           # 单块失败不影响其它块
            out[name] = None
            out['errors'][name] = str(exc)[:200]
    out['review'] = review_context(symbol, review)
    out['fetched_at'] = datetime.now(CST).isoformat()
    with _lock:
        _cache[key] = (now, out)
    return out
