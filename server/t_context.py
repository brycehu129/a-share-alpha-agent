"""做T 的环境数据：大盘、板块、个股资金流。只取事实，不做判断（判断在 sentinel_rules.t_evaluate）。

为什么要有环境：只看"振幅够大、价格在日内区间高位"回答的是"价格在哪"，回答不了"这个位置值不值得做T"。
同样在日内高位，大盘和板块同步走强、资金还在流入是顺势上涨，先卖容易卖飞；个股自己冲高、板块没跟、
主力在撤，才是典型的高抛位。低吸同理：系统性杀跌里的低位是接飞刀。

三类数据都**取不到就是 None，不当 0**：调用方据此说"缺数据，暂不判断"，而不是把缺失当成"环境平淡"。
板块 = 同行业（tushare 行业分类）里抽样的最多 40 只的当日涨跌幅**中位数**——是一个代理，不是官方板块指数；
样本按代码均匀抽取、剔除 ST，样本不足 10 只就不给。板块与资金流只在做T的价格位置已经满足时才联网取
（懒取），并缓存 5 分钟。
"""
import json
import statistics
from datetime import datetime

import flow_recorder
import live_quote
from collect_quotes import CST
from tushare_sync import read

BENCH_GROWTH = ('sz399006',)                   # 创业板/科创板个股对照创业板指
BENCH_MAIN = ('sh000001', 'sh000300')          # 其余对照上证 + 沪深300 的平均
INDEX_SYMBOLS = ('sh000001', 'sz399006', 'sh000300')
PEER_SAMPLE = 40
PEER_MIN = 10
SECTOR_CACHE_S = 300
FLOW_MAX_AGE_S = 900                           # 采集器每 5 分钟一次；今天的记录超过 15 分钟就当没有


def _f(v):
    return float(v) if v not in (None, '') else None


def market_change(symbol, quotes):
    """对照指数的当日涨跌幅（%）。返回 {'change', 'name'} 或 None（指数报价缺失）。"""
    growth = symbol.startswith(('sz300', 'sz301', 'sh688'))
    names = {'sh000001': '上证', 'sz399006': '创业板', 'sh000300': '沪深300'}
    wanted = BENCH_GROWTH if growth else BENCH_MAIN
    got = [(names[s], _f(quotes[s].get('change_pct'))) for s in wanted if s in quotes and quotes[s].get('change_pct') is not None]
    if not got:
        return None
    return {'change': round(statistics.mean(v for _, v in got), 2), 'name': '/'.join(n for n, _ in got)}


def _symbol(ts_code):
    code, exchange = ts_code.split('.')
    return exchange.lower() + code


def peers(history, symbol, sample=PEER_SAMPLE):
    """(行业名, 同行业抽样代码列表)。行业缺失返回 (None, [])。"""
    rows = read(history / 'tushare_data' / 'stock_basic.json')['rows']
    me = next((r for r in rows if _symbol(r['ts_code']) == symbol), None)
    if not me or not me.get('industry'):
        return None, []
    same = sorted(_symbol(r['ts_code']) for r in rows
                  if r.get('industry') == me['industry'] and r['exchange'] in ('SSE', 'SZSE') and r['list_status'] == 'L'
                  and 'ST' not in r['name'].upper() and '退' not in r['name'] and _symbol(r['ts_code']) != symbol)
    if len(same) > sample:                     # 均匀抽取，避免只取到同一段上市批次的股票
        same = [same[int(i * len(same) / sample)] for i in range(sample)]
    return me['industry'], same


def _cache_path(cache_dir, day):
    return cache_dir / ('sector-%s.json' % day)


def sector_change(history, symbol, day, now=None, snapshot_fn=None, cache_dir=None):
    """同行业抽样的当日涨跌幅中位数。返回 {'industry','change','n'} 或 None。永不抛出。"""
    now = now or datetime.now(CST)
    try:
        industry, syms = peers(history, symbol)
    except (OSError, ValueError, KeyError):
        return None
    if not industry or not syms:
        return None
    path = _cache_path(cache_dir, day) if cache_dir else None
    cache = {}
    if path is not None and path.exists():
        try:
            cache = json.loads(path.read_text(encoding='utf-8'))
            hit = cache.get(industry)
            if hit and (now - datetime.fromisoformat(hit['at'])).total_seconds() <= SECTOR_CACHE_S:
                return {k: hit[k] for k in ('industry', 'change', 'n')}
        except (OSError, ValueError, KeyError):
            cache = {}
    try:
        snap = (snapshot_fn or live_quote.snapshot)(syms)
    except (OSError, ValueError):
        return None
    changes = [float(q['change_pct']) for q in snap['quotes']
               if q.get('quote_date') == day and not q.get('is_index') and q.get('change_pct') is not None]
    if len(changes) < PEER_MIN:
        return None
    out = {'industry': industry, 'change': round(statistics.median(changes), 2), 'n': len(changes)}
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({**cache, industry: {**out, 'at': now.isoformat()}}, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass                                # 缓存写不进去不影响结果
    return out


def flow_facts(flow_dir, symbol, day, now=None):
    """采集器最近一次留存的主力资金流。返回 {'main','main_30m','as_of'}（单位：元）或 None。"""
    now = now or datetime.now(CST)
    try:
        rows = flow_recorder.series(flow_dir, day, symbol)
    except OSError:
        return None
    if not rows:
        return None
    last = rows[-1]
    if day == now.date().isoformat() and (now - datetime.fromisoformat(last['at'])).total_seconds() > FLOW_MAX_AGE_S:
        return None
    if last.get('main') is None or last.get('main_30m') is None:
        return None
    return {'main': last['main'], 'main_30m': last['main_30m'], 'as_of': last.get('as_of')}


def build_env(symbol, quotes, day, sector_fn=None, flow_fn=None):
    """拼成 t_evaluate 要的环境。sector_fn(symbol, day) / flow_fn(symbol, day) 由调用方注入（测试和生产各用各的）。
    任何一项取不到都是 None。永不抛出。"""
    def safe(fn):
        try:
            return fn(symbol, day) if fn else None
        except Exception:
            return None
    market = market_change(symbol, quotes)
    sector = safe(sector_fn)
    flow = safe(flow_fn)
    return {'market': market['change'] if market else None, 'market_name': market['name'] if market else None,
            'sector': sector['change'] if sector else None, 'sector_name': sector['industry'] if sector else None,
            'sector_n': sector['n'] if sector else None, 'flow': flow}
