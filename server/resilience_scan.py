"""盘中抗跌扫描：大盘往下时，哪些票横住或向上、主力在买、且所属板块也在吸金。

回答的问题是用户的原话——"当大盘走势往下时，绝大多数股票会跟随大盘，但有部分强势的票会
横住或者向上"。三个条件叠加：**抗跌 + 个股主力资金强 + 所属板块此时也强（大资金流入）**。

**这个模块不推送任何东西。** 结果只落到 intraday/resilience-<day>.jsonl 和页面上。
没有推送，页面就必须承担"14:00 打开也要看得到 10:15 发生过什么"的职责，所以每轮都留档。

--- 为什么是这个取数形状 -------------------------------------------------------

东财 clist 接口一页硬顶 100 行（见 market_rankings 模块文档），所以拿不到"全市场每只票的
资金流"，只能拿"按某个字段排序的前 100 名"。漏斗因此按成本排序：

  请求 A  板块 fid=f184  → 主力净流入占比最强的 100 个板块，同一行自带涨幅
  请求 B  个股 fid=f62   → 主力净流入**额**前 100（偏大盘股）
  请求 C  个股 fid=f184  → 主力净流入**占比**前 100（对中小票友好）
  请求 D  腾讯指数报价    → 另一台主机，不受东财限流影响

B 与 C 取并集去重：只按绝对金额排会系统性漏掉中小强势票，而那恰恰是本需求要找的对象。
"资金流强"因此在第一层就收敛了范围；抗跌度和板块归属都在本地算，不花请求。

--- 纪律 -----------------------------------------------------------------------

1. **大盘下跌不做硬闸门。** 只在大盘跌时才计算，就永远拿不到"大盘涨时这批票表现如何"的
   对照，也就无法回答"抗跌到底是不是真信号"。每轮都算，把当时的大盘状态记进每一行，
   由页面决定默认只看大盘下跌时的命中。
2. **阈值是显示过滤器，不是硬门槛。** 每轮把取回的票全量留档并打分，阈值只影响页面默认
   显示哪些。几天后有了真实分布再定阈值——ROADMAP 第 9 项对哨兵资金流规则写过同一条纪律。
   当前阈值全是起始设定，**未经任何验证**。
3. **取不到就明说。** 任何一段失败都降级并标 stale，绝不用上一轮的数字冒充此刻。
"""
import argparse
import json
import os
import sys
import threading
import time as _time
from datetime import datetime
from pathlib import Path

import intraday_engine as ie
import live_quote
import market_rankings
from collect_quotes import CST

# 抗跌度要和"同体量的票"比：拿微盘股去比沪深300，会把"本来就不跟大盘"当成抗跌。
# 按流通市值分档配基准指数（单位：元）。分档点是常见口径，非验证结果。
BENCHMARKS = {'sh000300': '沪深300', 'sh000905': '中证500', 'sh000852': '中证1000',
              'sh000001': '上证指数'}
PRIMARY = 'sh000300'            # 判定"大盘在跌"用它
SIZE_TIERS = ((500e8, 'sh000300'), (100e8, 'sh000905'))
SMALL_CAP_BENCHMARK = 'sh000852'

PAGE = market_rankings.PAGE_MAX
STALE_OK_SECONDS = 900
SOURCE_NOTE = ('东方财富按成交额分档估算；主力＝超大单＋大单，非交易所披露。'
               '每个榜单一页上限 100 行，所以这是"排行前列里的抗跌票"，不是全市场穷举。')

# 起始显示阈值，未经验证。页面可调；改这里不影响留档（留档永远是全量）。
DEFAULTS = {
    'excess_min_pp': 2.0,        # 抗跌度：个股涨跌幅 − 同档基准指数涨跌幅
    'main_net_pct_min': 3.0,     # 个股主力净流入占比
    'require_holds_up': False,   # 是否要求个股本身不跌（change_pct >= 0）
    'require_sector': True,      # 是否要求所属板块命中强势集合且板块在涨
}

_cache_lock = threading.Lock()
_cache = {}


class ScanError(ValueError):
    pass


# --- 基准与抗跌度 -------------------------------------------------------------

def benchmark_for(float_cap):
    """按流通市值挑基准指数。市值缺失时退回主基准并在行里标出来——不猜。"""
    if float_cap is None:
        return PRIMARY, False
    for floor, symbol in SIZE_TIERS:
        if float_cap >= floor:
            return symbol, True
    return SMALL_CAP_BENCHMARK, True


def index_changes(snapshot):
    """{symbol: change_pct}。只收 BENCHMARKS 里的，缺的就缺，不补 0。"""
    out = {}
    for q in (snapshot or {}).get('quotes', []):
        if q.get('symbol') in BENCHMARKS and q.get('change_pct') is not None:
            out[q['symbol']] = float(q['change_pct'])
    return out


# --- 组装 ---------------------------------------------------------------------

def build_rows(stocks, sector_by_name, changes):
    """把三段数据拼成逐股的一行。**不在这里过滤** —— 过滤是页面的事，留档要全量。"""
    primary_change = changes.get(PRIMARY)
    market_down = None if primary_change is None else primary_change < 0
    rows = []
    for s in stocks:
        cap = s.get('float_cap')
        bench, cap_known = benchmark_for(cap)
        bench_change = changes.get(bench)
        if bench_change is None:                      # 同档基准取不到就退回主基准
            bench, bench_change, cap_known = PRIMARY, primary_change, False
        sector = sector_by_name.get(s.get('industry')) if s.get('industry') else None
        row = {
            'symbol': s['symbol'], 'code': s['code'], 'name': s['name'],
            'price': s['price'], 'change_pct': s['change_pct'],
            'main_net': s['main_net'], 'main_net_pct': s['main_net_pct'],
            'float_cap': cap, 'industry': s.get('industry'),
            'benchmark': bench, 'benchmark_label': BENCHMARKS.get(bench),
            'benchmark_size_matched': cap_known,
            'excess_pp': None if bench_change is None else round(s['change_pct'] - bench_change, 2),
            'holds_up': s['change_pct'] >= 0,
            'market_down': market_down,
            'sector_matched': sector is not None,
            'sector_change_pct': sector['change_pct'] if sector else None,
            'sector_main_net_pct': sector['main_net_pct'] if sector else None,
            'sector_strength': sector['strength'] if sector else None,
        }
        # 对沪深300 的抗跌度单独留一份：分档基准是新口径，旧口径要能对照，否则换了分档点
        # 之后前后几天的留档就不可比了。
        row['excess_hs300'] = (None if primary_change is None
                               else round(s['change_pct'] - primary_change, 2))
        row['score'] = _score(row)
        rows.append(row)
    rows.sort(key=lambda r: (-(r['score'] if r['score'] is not None else -1e9), r['code']))
    return rows


def _score(row):
    """仅用于排序展示的合成分，**不是经过验证的信号强度**。
    抗跌度(pp) + 主力净流入占比(%)的一半 + 板块命中的固定加分。"""
    if row['excess_pp'] is None:
        return None
    score = row['excess_pp'] + row['main_net_pct'] * 0.5
    if row['sector_matched'] and (row['sector_change_pct'] or 0) > 0:
        score += 5.0
    return round(score, 2)


def passes(row, thresholds=None):
    """按阈值判断一行是否"命中"。阈值只是显示过滤器，留档不受影响。"""
    t = {**DEFAULTS, **(thresholds or {})}
    if row['excess_pp'] is None or row['excess_pp'] < t['excess_min_pp']:
        return False
    if row['main_net'] is None or row['main_net'] <= 0:
        return False
    if row['main_net_pct'] < t['main_net_pct_min']:
        return False
    if t['require_holds_up'] and not row['holds_up']:
        return False
    if t['require_sector']:
        if not row['sector_matched'] or not (row['sector_change_pct'] or 0) > 0:
            return False
    return True


# --- 取数（带 stale 回退）-----------------------------------------------------

def _with_cache(key, job, now_ts, errors, stale):
    """取数失败时沿用 15 分钟内最近一次成功值并标 stale；超时就返回 None。
    照 market_rankings.current_rankings 的做法，口径保持一致。"""
    try:
        value = job()
        with _cache_lock:
            _cache[key] = (now_ts, value)
        return value
    except Exception as exc:
        errors[key] = str(exc)[:200]
        with _cache_lock:
            cached = _cache.get(key)
        if cached and now_ts - cached[0] <= STALE_OK_SECONDS:
            stale.append(key)
            return cached[1]
        return None


def scan(now=None, http=None, quote_fn=None, now_ts=None, thresholds=None):
    """跑一次扫描。永不抛出——定时任务里一次失败不能影响下一次。"""
    now = now or datetime.now(CST)
    now_ts = _time.time() if now_ts is None else now_ts
    quote_fn = quote_fn or live_quote.snapshot
    errors, stale = {}, []

    sectors = _with_cache('sectors',
                          lambda: market_rankings.fetch_sector_rows('industry', http, fid='f184'),
                          now_ts, errors, stale)
    by_amount = _with_cache('inflow_amount',
                            lambda: market_rankings.fetch_stocks('inflow', http, size=PAGE,
                                                                 fid='f62', pz=PAGE),
                            now_ts, errors, stale)
    by_ratio = _with_cache('inflow_ratio',
                           lambda: market_rankings.fetch_stocks('inflow', http, size=PAGE,
                                                                fid='f184', pz=PAGE),
                           now_ts, errors, stale)
    quotes = _with_cache('indices', lambda: quote_fn(list(BENCHMARKS)), now_ts, errors, stale)

    sector_rows, sector_total, sector_scope = sectors if sectors else ([], None, '')
    sector_by_name = {r['name']: r for r in sector_rows}
    merged = {}
    for s in (by_amount or []) + (by_ratio or []):
        merged.setdefault(s['symbol'], s)
    changes = index_changes(quotes)

    rows = build_rows(list(merged.values()), sector_by_name, changes)
    t = {**DEFAULTS, **(thresholds or {})}
    hits = [r for r in rows if passes(r, t)]
    return {
        'generated_at': now.isoformat(),
        'session': (quotes or {}).get('session'),
        'market': {'changes': changes, 'primary': PRIMARY,
                   'primary_label': BENCHMARKS[PRIMARY],
                   'market_down': None if PRIMARY not in changes else changes[PRIMARY] < 0},
        'universe': {'stocks_scanned': len(rows), 'from_amount': len(by_amount or []),
                     'from_ratio': len(by_ratio or []),
                     'sectors_fetched': len(sector_rows), 'sectors_total': sector_total,
                     'sector_scope': sector_scope,
                     # 板块那一次请求失败时，所有行的 sector_matched 都是 False，命中数会变成 0。
                     # 那是"没法判断"，不是"没有符合条件的票"——界面必须能把两者分开说。
                     'sector_data_available': bool(sector_rows),
                     'note': '每个榜单一页上限 %d 行，不是全市场扫描' % PAGE},
        'thresholds': t,
        'hits': hits, 'rows': rows,
        'errors': errors, 'stale': stale,
        'source_note': SOURCE_NOTE,
    }


# --- 留档 ---------------------------------------------------------------------

def scan_path(directory, day):
    return Path(directory) / ('resilience-%s.jsonl' % day)


def record(directory, result):
    """每轮追加一行（全量 rows，不只是命中的）——阈值以后会改，原始值改不了。"""
    day = result['generated_at'][:10]
    ie._append_jsonl(Path(directory), 'resilience-%s.jsonl' % day, {
        'at': result['generated_at'],
        'market': result['market'],
        'universe': result['universe'],
        'thresholds': result['thresholds'],
        'hit_symbols': [r['symbol'] for r in result['hits']],
        'rows': result['rows'],
        'errors': result['errors'], 'stale': result['stale'],
    })


def read_rows(directory, day):
    return ie._read_jsonl(scan_path(directory, day))


def timeline(directory, day, thresholds=None):
    """今天每只票**第一次**命中的时刻，按时间升序。没有推送，页面靠这个回答
    "10:15 出现过谁"。阈值以当前值重算，所以改阈值后历史也会跟着重算。"""
    seen, out = {}, []
    for row in read_rows(directory, day):
        t = {**DEFAULTS, **(thresholds or row.get('thresholds') or {})}
        for r in row.get('rows', []):
            if not passes(r, t) or r['symbol'] in seen:
                continue
            seen[r['symbol']] = row['at']
            out.append({'at': row['at'], **{k: r[k] for k in
                        ('symbol', 'code', 'name', 'industry', 'price', 'change_pct',
                         'excess_pp', 'main_net', 'main_net_pct', 'sector_change_pct', 'score')}})
    return out


# --- 定时入口 -----------------------------------------------------------------

def run(history, now=None, directory=None, force=False, http=None, calendar_fn=None):
    """跑一次并留档。返回摘要；不抛出。"""
    now = now or datetime.now(CST)
    directory = Path(directory or ie.data_dir())
    summary = {'at': now.isoformat(), 'ran': False}
    if calendar_fn is None:
        from session_brief import calendar_state
        calendar_fn = lambda d: calendar_state(history, d.strftime('%Y%m%d'))
    try:
        calendar = calendar_fn(now)
    except Exception as exc:
        calendar = 'unknown(%s)' % type(exc).__name__
    ok, reason = ie.gate(now, calendar)
    if not ok and not force:
        summary['skipped'] = reason
        return summary
    result = scan(now, http=http)
    try:
        record(directory, result)
    except Exception as exc:
        summary['record_error'] = '留档失败：%s' % type(exc).__name__
    summary.update(ran=True, scanned=len(result['rows']), hits=len(result['hits']),
                   errors=result['errors'], stale=result['stale'],
                   market_down=result['market']['market_down'])
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, default=Path(os.environ.get('HISTORY_DIR', '.history')))
    p.add_argument('--force-session', action='store_true', help='忽略时段闸门（本地调试用）')
    p.add_argument('--no-record', action='store_true', help='只扫描不留档')
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    started = _time.monotonic()
    if a.no_record:
        result = scan()
        result['duration_ms'] = round((_time.monotonic() - started) * 1000)
        if a.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            m = result['market']
            print('%s · %s %s · 扫描 %d 只 · 命中 %d 只 · %dms' % (
                result['generated_at'], m['primary_label'],
                m['changes'].get(m['primary'], '—'), len(result['rows']), len(result['hits']),
                result['duration_ms']))
            for r in result['hits'][:20]:
                print('  %s %-8s 现价%-8s 涨跌%+6.2f%% 抗跌%+6.2f%% 主力%s(%.1f%%) 板块%s' % (
                    r['code'], r['name'], r['price'], r['change_pct'], r['excess_pp'],
                    _money(r['main_net']), r['main_net_pct'], r['industry'] or '—'))
            if result['errors']:
                print('  取数失败: %s' % result['errors'])
        return 0
    result = run(a.history, force=a.force_session)
    result['duration_ms'] = round((_time.monotonic() - started) * 1000)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result.get('ran'):
        print('未运行：%s' % (result.get('skipped') or result.get('error')))
    else:
        print('%s · 扫描 %d · 命中 %d · %dms%s' % (
            result['at'], result['scanned'], result['hits'], result['duration_ms'],
            ' · 取数失败 ' + ','.join(result['errors']) if result['errors'] else ''))
    return 0 if result.get('ran') or result.get('skipped') else 1


def _money(v):
    if v is None:
        return '—'
    a = abs(v)
    return ('%+.2f亿' % (v / 1e8)) if a >= 1e8 else ('%+.0f万' % (v / 1e4))


if __name__ == '__main__':
    sys.exit(main())
