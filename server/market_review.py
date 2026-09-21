#!/usr/bin/env python3
"""看板「市场行情」的数据：大盘统计（成交额/资金/涨跌家数）、涨跌停池、龙虎榜。只用标准库。

数据源是东方财富公开接口（无需 token、不占 Tushare 积分）；指数实时价复用 live_quote（腾讯）。
这些都是公开行情，不含任何持仓，所以直接落在 server/data/market_review/，不走 git。

设计取舍：
- **每一组数据各自失败、各自降级**。一个接口挂了只让对应的块显示"暂无"，不拖垮整页；
  绝不在拿不到数据时拿别的日子的数据顶替而不标日期。
- **交易日**由涨停池是否有数据判定（休市日池子是空的），往回最多找 7 天；日期一律带在输出里。
- 东财是非官方接口，字段可能变。所有解析都集中在 parse_* 里并做形状校验，校验不过就当这一组失败。
- 涨跌停家数 = 池子长度（交易所口径，含 ST/北交所），不再按涨跌幅阈值近似。

入口：
    python3 market_review.py              # 抓取最新交易日并落盘（16:30 / 17:30 的定时任务）
    python3 market_review.py --day 20260921
"""
import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from collect_quotes import CST

UA = 'Mozilla/5.0 (alpha-shadow market_review)'
POOL_UT = '7eea3edcaed734bea9cbfc24409ed989'
POOL_ENDPOINT = 'https://push2ex.eastmoney.com/'
DATACENTER = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
PUSH2 = 'https://push2.eastmoney.com/api/qt/'
KLINE = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'

MAX_BYTES = 4_000_000
TIMEOUT = 8
SCAN_DAYS = 7
POOL_KINDS = ('zt', 'dt', 'zb', 'yzt', 'qs')
POOL_API = {'zt': ('getTopicZTPool', 'fbt:asc'), 'dt': ('getTopicDTPool', 'fund:asc'),
            'zb': ('getTopicZBPool', 'fbt:asc'), 'yzt': ('getYesterdayZTPool', 'zs:desc'),
            'qs': ('getTopicQSPool', 'zdp:desc')}

INDEX_SYMBOLS = ['sh000001', 'sh000300', 'sh000688', 'sh000852', 'sz399001', 'sz399006']
INDEX_LABEL = {'sh000001': '上证指数', 'sh000300': '沪深300', 'sh000688': '科创50',
               'sh000852': '中证1000', 'sz399001': '深证成指', 'sz399006': '创业板指'}

LIVE_CACHE_SECONDS = 30
POOL_CACHE_SECONDS = 60


class ReviewError(ValueError):
    """某一组数据取不到或形状不对。上层把它记成该组的失败原因，不外泄成 500。"""


# --- HTTP ---------------------------------------------------------------------------------

def http_get(url, timeout=TIMEOUT):
    req = Request(url, headers={'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/'})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ReviewError('响应超过大小上限')
    return raw


def _try_json(url, http, retries):
    last = None
    for attempt in range(retries + 1):
        try:
            return json.loads(http(url).decode('utf-8')), None
        except (OSError, URLError, ValueError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    return None, last


def get_json(url, http=None, retries=1):
    """带重试地取 JSON。重试只针对网络/解析层面的临时失败。

    东财的 push2* 行情主机对个别出口 IP 会在 TLS 握手阶段直接断开连接（同一台机器上 http 明文却通），
    所以 https 失败后对这类主机再试一次 http。这些接口只返回公开行情、请求里不带任何凭证，明文可以接受；
    并且只有在 https 已经失败之后才会降级，不是默认走明文。"""
    http = http or http_get
    data, err = _try_json(url, http, retries)
    if err is None:
        return data
    host = urlsplit(url).hostname or ''
    if url.startswith('https://') and re.match(r'(\d+\.)?push2[a-z]*\.eastmoney\.com$', host):
        data, err2 = _try_json('http://' + url[len('https://'):], http, 0)
        if err2 is None:
            return data
    raise ReviewError('请求失败: %s' % (str(err)[:160],))


# --- 通用小工具 ---------------------------------------------------------------------------

def symbol_of_code(code):
    """6 位代码 → sh/sz/bj 前缀代码。认不出来返回 None。"""
    code = str(code or '')
    if not re.fullmatch(r'\d{6}', code):
        return None
    if code[0] == '6':
        return 'sh' + code
    if code[0] in '03':
        return 'sz' + code
    if code[0] in '489':
        return 'bj' + code
    return None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float('inf'), float('-inf')) else None


def hhmmss(v):
    """东财的封板时间是整数 hhmmss（92500 / 130001）→ '09:25:00'。"""
    n = _num(v)
    if n is None or n <= 0:
        return None
    s = '%06d' % int(n)
    return '%s:%s:%s' % (s[:2], s[2:4], s[4:])


def iso_day(day):
    return '%s-%s-%s' % (day[:4], day[4:6], day[6:])


def _base(row):
    """各个池子共有的字段。price 在东财里是"厘"（×1000）。"""
    symbol = symbol_of_code(row.get('c'))
    price = _num(row.get('p'))
    if symbol is None or price is None:
        return None
    return {'symbol': symbol, 'code': row['c'], 'name': (row.get('n') or '').replace('　', '').replace('  ', ''),
            'price': round(price / 1000, 3), 'pct': _round(row.get('zdp')),
            'amount': _num(row.get('amount')), 'float_cap': _num(row.get('ltsz')),
            'turnover': _round(row.get('hs')), 'industry': row.get('hybk') or None}


def _round(v, digits=2):
    n = _num(v)
    return round(n, digits) if n is not None else None


# --- 涨跌停池 -----------------------------------------------------------------------------

def parse_pool(kind, payload):
    """→ (总数, 行列表)。形状不对抛 ReviewError；单行残缺就丢掉那一行。"""
    try:
        data = payload['data']
        rows = data['pool']
        total = int(data['tc'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewError('%s 池响应形状不对' % kind) from exc
    if not isinstance(rows, list):
        raise ReviewError('%s 池响应形状不对' % kind)
    out = []
    for r in rows:
        item = _base(r)
        if item is None:
            continue
        if kind == 'zt':
            item.update(boards=int(_num(r.get('lbc')) or 1), first_seal=hhmmss(r.get('fbt')),
                        last_seal=hhmmss(r.get('lbt')), seal_fund=_num(r.get('fund')),
                        open_times=int(_num(r.get('zbc')) or 0))
        elif kind == 'dt':
            item.update(seal_fund=_num(r.get('fund')), last_seal=hhmmss(r.get('lbt')),
                        days=int(_num(r.get('days')) or 1), open_times=int(_num(r.get('oc')) or 0))
        elif kind == 'zb':
            item.update(first_seal=hhmmss(r.get('fbt')), open_times=int(_num(r.get('zbc')) or 0),
                        amplitude=_round(r.get('zf')), limit_price=round((_num(r.get('ztp')) or 0) / 1000, 3) or None)
        elif kind == 'yzt':
            item.update(boards=int(_num(r.get('ylbc')) or 1), first_seal=hhmmss(r.get('yfbt')),
                        amplitude=_round(r.get('zf')))
        elif kind == 'qs':
            item.update(new_high=int(_num(r.get('nh')) or 0), volume_ratio=_round(r.get('lb')))
        out.append(item)
    return total, out


def fetch_pool(kind, day, http=None):
    api, sort = POOL_API[kind]
    q = urlencode({'ut': POOL_UT, 'dpt': 'wz.ztzt', 'Pageindex': 0, 'pagesize': 600, 'sort': sort, 'date': day})
    total, rows = parse_pool(kind, get_json('%s%s?%s' % (POOL_ENDPOINT, api, q), http))
    return {'total': total, 'rows': rows}


def find_trade_day(now, http=None, scan=SCAN_DAYS):
    """从 now 往回找第一个涨停池非空的日子。周末/休市日池子是空的。找不到返回 None。"""
    day = now.date()
    for _ in range(scan):
        if day.weekday() < 5:
            ymd = day.strftime('%Y%m%d')
            try:
                pool = fetch_pool('zt', ymd, http)
            except ReviewError:
                pool = None
            if pool and pool['total'] > 0:
                return ymd, pool
        day -= timedelta(days=1)
    return None, None


def fetch_pools(day, http=None, first_zt=None):
    """一个交易日的 5 个池子。每个池子各自失败：{'zt': {...}|None, ..., 'errors': {kind: 原因}}。"""
    out, errors = {}, {}
    for kind in POOL_KINDS:
        if kind == 'zt' and first_zt is not None:
            out[kind] = first_zt
            continue
        try:
            out[kind] = fetch_pool(kind, day, http)
        except ReviewError as exc:
            out[kind] = None
            errors[kind] = str(exc)
    out['errors'] = errors
    return out


# --- 龙虎榜 -------------------------------------------------------------------------------

def _datacenter(report, columns, flt, sort_col, http, page_size=500):
    q = urlencode({'sortColumns': sort_col, 'sortTypes': -1, 'pageSize': page_size, 'pageNumber': 1,
                   'reportName': report, 'columns': columns, 'filter': flt})
    body = get_json('%s?%s' % (DATACENTER, q), http)
    result = body.get('result') if isinstance(body, dict) else None
    if result is None:
        # 当天还没有数据时，datacenter 返回 result=null 加提示，不是错误。
        return []
    rows = result.get('data')
    if not isinstance(rows, list):
        raise ReviewError('%s 响应形状不对' % report)
    return rows


def _is_multiday(reason):
    return '连续' in (reason or '')


def parse_lhb(rows):
    """按个股合并。同一只股票会因多个上榜原因出现多行：金额取"当日口径"的那一行（不含"连续N个交易日"
    这类累计口径），没有就取净额绝对值最大的；所有上榜原因都保留。"""
    by_code = {}
    for r in rows:
        symbol = symbol_of_code(r.get('SECURITY_CODE'))
        buy, sell, net = _num(r.get('BILLBOARD_BUY_AMT')), _num(r.get('BILLBOARD_SELL_AMT')), _num(r.get('BILLBOARD_NET_AMT'))
        if symbol is None or net is None:
            continue
        by_code.setdefault(symbol, []).append((r, buy, sell, net))
    out = []
    for symbol, group in by_code.items():
        single = [g for g in group if not _is_multiday(g[0].get('EXPLANATION'))]
        r, buy, sell, net = max(single or group, key=lambda g: abs(g[3]))
        out.append({'symbol': symbol, 'code': r['SECURITY_CODE'], 'name': r.get('SECURITY_NAME_ABBR') or '',
                    'price': _num(r.get('CLOSE_PRICE')), 'pct': _round(r.get('CHANGE_RATE')),
                    'buy': buy, 'sell': sell, 'net': net, 'turnover': _round(r.get('TURNOVERRATE')),
                    'float_cap': _num(r.get('FREE_MARKET_CAP')), 'deal_amount': _num(r.get('BILLBOARD_DEAL_AMT')),
                    'net_ratio': _round(r.get('DEAL_NET_RATIO')),
                    'reasons': sorted({g[0].get('EXPLANATION') for g in group if g[0].get('EXPLANATION')})})
    out.sort(key=lambda x: -x['net'])
    return out


def fetch_lhb(day, http=None):
    iso = iso_day(day)
    flt = "(TRADE_DATE>='%s')(TRADE_DATE<='%s')" % (iso, iso)
    rows = _datacenter('RPT_DAILYBILLBOARD_DETAILSNEW', 'ALL', flt, 'BILLBOARD_NET_AMT', http)
    return {'rows': parse_lhb(rows)}


def parse_seats(buy_rows, sell_rows):
    """按上榜原因分组的买入/卖出前五营业部。同一批席位在多个原因下重复出现（比如"7%"和"换手20%"
    两条原因买卖榜完全一样），完全相同的分组合并成一组、原因并列，避免详情里出现两张一样的表。"""
    groups = {}
    for side, rows in (('buy', buy_rows), ('sell', sell_rows)):
        for r in rows:
            reason = r.get('EXPLANATION') or ''
            g = groups.setdefault(reason, {'reason': reason, 'buy': [], 'sell': []})
            g[side].append({'name': r.get('OPERATEDEPT_NAME') or '', 'buy': _num(r.get('BUY')) or 0.0,
                            'sell': _num(r.get('SELL')) or 0.0, 'net': _num(r.get('NET')) or 0.0})
    merged = {}
    for g in groups.values():
        g['buy'].sort(key=lambda s: -s['buy'])
        g['sell'].sort(key=lambda s: -s['sell'])
        key = json.dumps([g['buy'], g['sell']], sort_keys=True, ensure_ascii=False)
        if key in merged:
            merged[key]['reasons'].append(g['reason'])
        else:
            merged[key] = {'reasons': [g['reason']], 'buy': g['buy'], 'sell': g['sell']}
    out = list(merged.values())
    for g in out:
        g['buy_total'] = sum(s['buy'] for s in g['buy'])
        g['sell_total'] = sum(s['sell'] for s in g['sell'])
        g['net'] = g['buy_total'] - g['sell_total']
        g['multiday'] = all(_is_multiday(x) for x in g['reasons'])
    out.sort(key=lambda g: (g['multiday'], -abs(g['net'])))     # 当日口径排前面
    return out


def fetch_seats(code, day, http=None):
    if not re.fullmatch(r'\d{6}', str(code)):
        raise ReviewError('股票代码格式不对')
    flt = '(TRADE_DATE=\'%s\')(SECURITY_CODE="%s")' % (iso_day(day), code)
    buy = _datacenter('RPT_BILLBOARD_DAILYDETAILSBUY', 'ALL', flt, 'BUY', http, 100)
    sell = _datacenter('RPT_BILLBOARD_DAILYDETAILSSELL', 'ALL', flt, 'SELL', http, 100)
    return parse_seats(buy, sell)


# --- 大盘统计（实时）----------------------------------------------------------------------

def _push2(path, params, http):
    return get_json('%s%s?%s' % (PUSH2, path, urlencode(params, safe=',')), http)


def parse_breadth(payload):
    """ulist 返回每个市场（沪/深/北）的上涨/下跌/平盘家数（f104/f105/f106）。"""
    try:
        diff = payload['data']['diff']
    except (KeyError, TypeError) as exc:
        raise ReviewError('涨跌家数响应形状不对') from exc
    up = down = flat = 0
    seen = 0
    for d in diff:
        u, dn, fl = _num(d.get('f104')), _num(d.get('f105')), _num(d.get('f106'))
        if u is None or dn is None or fl is None:
            continue
        up, down, flat, seen = up + int(u), down + int(dn), flat + int(fl), seen + 1
    if seen == 0:
        raise ReviewError('涨跌家数为空')
    total = up + down + flat
    return {'up': up, 'down': down, 'flat': flat, 'total': total,
            'up_pct': round(up / total * 100, 1) if total else None, 'markets': seen}


def fetch_breadth(http=None):
    return parse_breadth(_push2('ulist.np/get', {'fltt': 1, 'fields': 'f12,f14,f104,f105,f106',
                                                 'secids': '1.000001,0.399001,0.899050'}, http))


def _kline_series(payload):
    try:
        return {ln.split(',')[0]: float(ln.split(',')[6]) for ln in payload['data']['klines']}
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ReviewError('成交额 K 线响应形状不对') from exc


def _turnover(now_amount, today, prev_day, prev_amount):
    delta = now_amount - prev_amount
    return {'date': today, 'prev_date': prev_day, 'amount': now_amount, 'prev_amount': prev_amount, 'delta': delta,
            'delta_pct': round(delta / prev_amount * 100, 1) if prev_amount else None,
            'trend': 'expand' if delta > 0 else 'shrink' if delta < 0 else 'flat'}


def parse_turnover_klines(sh, sz):
    """两市（沪+深）成交额，单位元。sh/sz 是各自指数日 K 的 kline 响应，取最后两根：今日与上一交易日。
    只有两边日期对得上才相加，对不上说明有一边缺数据，宁可报错也不拿错位的日子相加。"""
    a, b = _kline_series(sh), _kline_series(sz)
    days = sorted(set(a) & set(b))
    if len(days) < 2:
        raise ReviewError('成交额 K 线不足两个交易日')
    today, prev = days[-1], days[-2]
    return _turnover(a[today] + b[today], today, prev, a[prev] + b[prev])


def parse_prev_turnover(sh, sz, today):
    """上一个交易日（严格早于 today）的两市成交额 → (日期, 元)。"""
    a, b = _kline_series(sh), _kline_series(sz)
    days = sorted(d for d in set(a) & set(b) if d < today)
    if not days:
        raise ReviewError('成交额 K 线里没有早于 %s 的交易日' % today)
    return days[-1], a[days[-1]] + b[days[-1]]


def _kline_payloads(http):
    def one(secid):
        q = urlencode({'secid': secid, 'fields1': 'f1,f2,f3', 'fields2': 'f51,f52,f53,f54,f55,f56,f57',
                       'klt': 101, 'fqt': 0, 'lmt': 5, 'end': datetime.now(CST).strftime('%Y%m%d')}, safe=',')
        return get_json('%s?%s' % (KLINE, q), http)
    return one('1.000001'), one('0.399001')


def fetch_turnover(http=None):
    return parse_turnover_klines(*_kline_payloads(http))


def turnover_from_quotes(indices, prev):
    """今日成交额直接用腾讯指数行情里的成交额（沪+深，万元）——和指数价格同一次请求、同一时刻，
    不必再为它打一次东财。prev = (上一交易日, 元)，一天内基本不变，由调用方缓存。"""
    by = {q['symbol']: q for q in (indices or {}).get('quotes', [])}
    sh, sz = by.get('sh000001'), by.get('sz399001')
    if not sh or not sz or sh.get('amount_wan') is None or sz.get('amount_wan') is None:
        raise ReviewError('指数行情里没有成交额')
    today = (sh.get('quote_at') or '')[:10]
    if not today or today != (sz.get('quote_at') or '')[:10]:
        raise ReviewError('沪深指数行情日期不一致，不相加')
    return _turnover((sh['amount_wan'] + sz['amount_wan']) * 1e4, today, prev[0], prev[1])


def parse_flow(payload):
    """沪深合计的资金流向（元）：主力=超大单+大单，另有中单、小单。"""
    try:
        line = payload['data']['klines'][-1].split(',')
        date = line[0]
        main, small, mid, big, huge = (float(x) for x in line[1:6])
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ReviewError('资金流向响应形状不对') from exc
    return {'date': date, 'main_net': main, 'huge_net': huge, 'big_net': big, 'mid_net': mid, 'small_net': small}


def fetch_flow(http=None):
    return parse_flow(_push2('stock/fflow/daykline/get', {'lmt': 1, 'klt': 101, 'secid': '1.000001', 'secid2': '0.399001',
                                                          'fields1': 'f1,f2,f3,f7', 'fields2': 'f51,f52,f53,f54,f55,f56'}, http))


def fetch_indices(snapshot=None):
    """6 个指数的实时行情（腾讯）。价格、涨跌幅、行情时间全部来自这一次请求，不读任何归档。"""
    if snapshot is None:
        import live_quote
        snapshot = live_quote.snapshot(INDEX_SYMBOLS)
    quotes = {q['symbol']: q for q in snapshot.get('quotes', [])}
    out = []
    for s in INDEX_SYMBOLS:
        q = quotes.get(s)
        if q is None:
            continue
        out.append({'symbol': s, 'name': INDEX_LABEL[s], 'last': q['last'], 'change_pct': q['change_pct'],
                    'quote_at': q['quote_at'], 'amount_wan': _num(q.get('amount_wan'))})
    if not out:
        raise ReviewError('指数行情为空')
    return {'quotes': out, 'session': snapshot.get('session'), 'fetched_at': snapshot.get('fetched_at')}


_live_lock = threading.Lock()
_live_cache = {}
# 各组数据各自的缓存时间：东财对短时间内的密集请求会直接断连接（实测出口 IP 被临时拒绝），
# 所以慢变化的数据不要跟着页面的 30 秒节奏打接口——上一交易日成交额一天内不会变，资金流向和涨跌家数一两分钟就够新。
TTL = {'indices': LIVE_CACHE_SECONDS, 'breadth': 60, 'flow': 120, 'prev_turnover': 3600}   # prev_turnover 也是日 K 的缓存时间
STALE_OK_SECONDS = 900      # 取失败时，15 分钟内的上一次成功值还能顶着用（标 stale），再久就老老实实报缺失


def _cached(key, ttl, fn, now_ts, force=False):
    with _live_lock:
        hit = _live_cache.get(key)
    if hit and not force and now_ts - hit[0] < ttl:
        return hit[1]
    try:
        value = fn()
    except Exception:
        if hit and now_ts - hit[0] < STALE_OK_SECONDS:
            return {**hit[1], 'stale': True} if isinstance(hit[1], dict) else hit[1]
        raise
    with _live_lock:
        _live_cache[key] = (now_ts, value)
    return value


def market_stats(now=None, http=None, snapshot=None, force=False):
    """看板顶部要的实时统计：指数、成交额、资金、涨跌家数。每一组各自取、各自缓存、各自失败，
    失败只在 errors 里留一句，其它块照常显示。今日成交额取自指数行情，只有「上一交易日成交额」要打东财
    （日 K，一小时缓存一次）。各组并发取，最坏情况的耗时是最慢的那一组，而不是它们相加。"""
    now_ts = time.time()
    out = {'fetched_at': datetime.now(CST).isoformat(), 'errors': {}}
    day = datetime.now(CST).strftime('%Y%m%d')
    jobs = {
        'indices': lambda: _cached('indices', TTL['indices'], lambda: fetch_indices(snapshot), now_ts, force),
        'breadth': lambda: _cached('breadth', TTL['breadth'], lambda: fetch_breadth(http), now_ts, force),
        'flow': lambda: _cached('flow', TTL['flow'], lambda: fetch_flow(http), now_ts, force),
        'klines': lambda: _cached('klines:' + day, TTL['prev_turnover'], lambda: _kline_payloads(http), now_ts),
    }
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
        for k, f in futures.items():
            try:
                results[k] = f.result()
            except Exception as exc:        # 单组失败不外泄：看板照常显示其它块
                results[k], errors[k] = None, str(exc)[:200]
    out['indices'], out['breadth'], out['flow'] = results['indices'], results['breadth'], results['flow']
    out['errors'].update({k: errors[k] for k in ('indices', 'breadth', 'flow') if k in errors})

    try:
        if not out['indices']:
            raise ReviewError('指数行情缺失，无法算成交额')
        if not results['klines']:
            raise ReviewError('上一交易日成交额取不到：%s' % errors.get('klines', ''))
        quote_day = (out['indices']['quotes'][0].get('quote_at') or '')[:10]
        prev = parse_prev_turnover(*results['klines'], quote_day)
        out['turnover'] = turnover_from_quotes(out['indices'], prev)
    except Exception as exc:
        out['turnover'] = None
        out['errors']['turnover'] = str(exc)[:200]

    import live_quote
    session = (out['indices'] or {}).get('session') or live_quote.clock_session(now)
    out['session'] = session
    # 收盘前成交额/资金还在累计，和上一日的完整值比"缩量"会误导；前端据此加注。
    out['complete'] = session in ('closing', 'post_close', 'weekend')
    return out


# --- 落盘与读取 ---------------------------------------------------------------------------

def data_dir():
    import os
    import portfolio_book
    return Path(os.environ.get('MARKET_REVIEW_DIR') or (Path(portfolio_book.default_dir()).parent / 'market_review'))


def save_review(review, directory=None):
    import tushare_sync
    d = Path(directory) if directory else data_dir()
    tushare_sync.save(d / (review['trade_date'] + '.json'), review)


def load_latest(directory=None):
    """最近一个校验通过的落盘文件；损坏的跳过、退回更早的一天。没有返回 None。"""
    import tushare_sync
    d = Path(directory) if directory else data_dir()
    if not d.is_dir():
        return None
    for p in sorted((p for p in d.glob('*.json') if re.fullmatch(r'\d{8}', p.stem)), reverse=True)[:SCAN_DAYS]:
        try:
            return tushare_sync.read(p)
        except (OSError, ValueError, KeyError):
            continue
    return None


_pools_cache = {}


def live_pools(day, http=None, now_ts=None):
    """今天的涨跌停池，直接从东财取（60 秒缓存）。用于"盘后定时任务还没跑"的时段——盘中、以及 15:00~16:30。
    涨停池为空（没开盘/休市/接口失败）返回 None，调用方退回落盘的那份。"""
    now_ts = time.time() if now_ts is None else now_ts
    with _live_lock:
        hit = _pools_cache.get(day)
        if hit and now_ts - hit[0] < POOL_CACHE_SECONDS:
            return hit[1]
    try:
        zt = fetch_pool('zt', day, http)
    except ReviewError:
        return None
    result = None
    if zt['total'] > 0:
        pools = fetch_pools(day, http, first_zt=zt)
        result = {'pools': pools, 'errors': pools.pop('errors')}
    with _live_lock:
        _pools_cache[day] = (now_ts, result)
    return result


def _lhb_block(stored):
    lhb = (stored or {}).get('lhb')
    if not lhb:
        return None
    return {**lhb, 'trade_date': stored['trade_date'], 'date': stored['date']}


def current_review(now=None, http=None, directory=None):
    """页面用的复盘数据。涨跌停池优先给"今天"的：落盘的那份不是今天的、且已过 9:25 就现取；
    龙虎榜（16:30 之后才有）和次日关注永远来自落盘文件，各自带着自己的日期。"""
    now = now or datetime.now(CST)
    stored = load_latest(directory)
    today = now.strftime('%Y%m%d')
    out = {'source': 'stored', 'trade_date': None, 'date': None, 'fetched_at': None, 'pools': None,
           'errors': {}, 'lhb': _lhb_block(stored), 'next_day_watch': (stored or {}).get('next_day_watch')}
    if stored:
        out.update(trade_date=stored['trade_date'], date=stored['date'], fetched_at=stored['fetched_at'],
                   pools=stored['pools'], errors=dict(stored.get('errors') or {}))
    if now.weekday() < 5 and (now.hour, now.minute) >= (9, 25) and (not stored or stored['trade_date'] < today):
        live = live_pools(today, http)
        if live:
            out.update(source='live', trade_date=today, date=iso_day(today), fetched_at=now.isoformat(),
                       pools=live['pools'], errors=live['errors'])
    return out


def build_review(now=None, day=None, http=None):
    """抓一个交易日的全部盘后数据。day=None 时自动找最近的交易日。"""
    now = now or datetime.now(CST)
    first_zt = None
    if day is None:
        day, first_zt = find_trade_day(now, http)
        if day is None:
            raise ReviewError('最近 %d 天没有找到有涨停数据的交易日（休市或接口不可用）' % SCAN_DAYS)
    pools = fetch_pools(day, http, first_zt)
    errors = dict(pools.pop('errors'))
    lhb = None
    try:
        lhb = fetch_lhb(day, http)
    except ReviewError as exc:
        errors['lhb'] = str(exc)
    return {'schema': 1, 'trade_date': day, 'date': iso_day(day), 'fetched_at': now.isoformat(),
            'pools': pools, 'lhb': lhb, 'errors': errors}


def run(day=None, directory=None, now=None):
    """定时任务入口：抓取并落盘。17:30 那次会覆盖 16:30 的（龙虎榜 16:30~17:30 才陆续补全）。
    已有的 next_day_watch 留着不丢——它由盘后分析写入，这里只更新原始数据。"""
    review = build_review(now=now, day=day)
    prior = load_latest(directory)
    if prior and prior.get('trade_date') == review['trade_date'] and prior.get('next_day_watch'):
        review['next_day_watch'] = prior['next_day_watch']
    save_review(review, directory)
    return review


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--day', help='YYYYMMDD，默认自动找最近交易日')
    p.add_argument('--dir', type=Path, help='落盘目录，默认 server/data/market_review')
    a = p.parse_args()
    try:
        r = run(day=a.day, directory=a.dir)
    except ReviewError as exc:
        print('市场复盘抓取失败: %s' % exc)
        return 1
    zt = r['pools'].get('zt')
    print('交易日 %s：涨停 %s 只 · 龙虎榜 %s 只 · 部分失败 %s' % (
        r['date'], zt['total'] if zt else '—', len(r['lhb']['rows']) if r['lhb'] else '—', r['errors'] or '无'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
