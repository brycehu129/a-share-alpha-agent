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
    'exclude_one_word': True,    # 排除一字涨停：全天只有一个价，买不进
    'exclude_st': True,          # 排除 ST/退市整理：候选池策略本来就排除，风险与涨跌幅限制都不同
}

# 涨停幅度按板块不同。ST 是 5%，但只有拿到名字才认得出来——market_context._limit_threshold
# 明确写了它按涨跌幅近似、认不出 ST，这里因为有名字所以能认。留一点余量避免浮点与四舍五入。
LIMIT_PCT = {'star': 19.5, 'main': 9.8, 'st': 4.8}

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


def is_st(name):
    """ST / *ST / 退市整理。按名字认——clist 不给风险警示标志位，名字是唯一线索。
    候选池策略（alpha_model）本来就排除 ST，这里保持一致。"""
    upper = str(name or '').upper().replace(' ', '')
    return 'ST' in upper or '退' in upper


def limit_threshold(symbol, name):
    """该股今日的涨停幅度（百分数）。"""
    if is_st(name):
        return LIMIT_PCT['st']
    return LIMIT_PCT['star'] if symbol.startswith(('sz30', 'sh688')) else LIMIT_PCT['main']


def limit_state(row):
    """(是否涨停, 是否一字涨停)。

    一字涨停的判据是**最高价 == 最低价** —— 全天只有一个价格，意味着从开盘就封死、
    中途没有任何一笔在更低的价位成交过，也就买不进。只是"现在封着板"（盘中涨停）不算：
    它更早的时候是能买的，而且可能炸板，属于有效信息，所以单独标记、不默认排除。

    高低价缺失时**不**判为一字——缺失当成"满足条件"会凭空排除掉一批票。
    """
    change, high, low = row.get('change_pct'), row.get('high'), row.get('low')
    if change is None:
        return False, False
    at_limit = change >= limit_threshold(row['symbol'], row.get('name'))
    if not at_limit or high is None or low is None or high <= 0 or low <= 0:
        return at_limit, False
    return True, abs(high - low) < 1e-9


def index_changes(snapshot):
    """{symbol: change_pct}。只收 BENCHMARKS 里的，缺的就缺，不补 0。"""
    out = {}
    for q in (snapshot or {}).get('quotes', []):
        if q.get('symbol') in BENCHMARKS and q.get('change_pct') is not None:
            out[q['symbol']] = float(q['change_pct'])
    return out


# --- 组装 ---------------------------------------------------------------------

def build_rows(stocks, resolve_sector, changes, sector_source=None):
    """把三段数据拼成逐股的一行。**不在这里过滤** —— 过滤是页面的事，留档要全量。

    `resolve_sector(stock) -> 板块行 | None`：板块归属的解析方式随数据源而变，
    所以由调用方注入而不是写死——新浪要查映射表（48 个粗行业），东财直接用响应里的
    f100（496 个细行业）。两家的行业名**对不上**，所以每行记下 `sector_source`，
    免得事后拿两天不同来源的留档当同一口径比较。
    """
    primary_change = changes.get(PRIMARY)
    market_down = None if primary_change is None else primary_change < 0
    rows = []
    for s in stocks:
        cap = s.get('float_cap')
        bench, cap_known = benchmark_for(cap)
        bench_change = changes.get(bench)
        if bench_change is None:                      # 同档基准取不到就退回主基准
            bench, bench_change, cap_known = PRIMARY, primary_change, False
        sector = resolve_sector(s)
        at_limit, one_word = limit_state(s)
        row = {
            'symbol': s['symbol'], 'code': s['code'], 'name': s['name'],
            'price': s['price'], 'change_pct': s['change_pct'],
            'high': s.get('high'), 'low': s.get('low'),
            'is_st': is_st(s['name']), 'limit_up': at_limit, 'one_word_limit': one_word,
            'main_net': s['main_net'], 'main_net_pct': s['main_net_pct'],
            'float_cap': cap, 'industry': s.get('industry'),
            'benchmark': bench, 'benchmark_label': BENCHMARKS.get(bench),
            'benchmark_size_matched': cap_known,
            'excess_pp': None if bench_change is None else round(s['change_pct'] - bench_change, 2),
            'holds_up': s['change_pct'] >= 0,
            'market_down': market_down,
            'sector_matched': sector is not None,
            'sector_name': sector['name'] if sector else None,
            'sector_source': sector_source,
            'sector_change_pct': sector['change_pct'] if sector else None,
            'sector_main_net_pct': sector['main_net_pct'] if sector else None,
            'sector_strength': sector['strength'] if sector else None,
            # 两家的 strength 维度不同（新浪两维、东财三维），不可直接比较，所以标明出处
            'sector_strength_basis': sector.get('strength_basis') if sector else None,
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
    """按阈值判断一行是否"命中"。阈值只是显示过滤器，留档不受影响。

    结构性排除（ST / 一字板）放在最前面：这两类不是"分数不够"，是**买不进或不该买**，
    和阈值高低无关。它们仍然留在 rows 里并带标志位，只是永远不进命中列表——
    这样事后还能回答"被排除的那些后来怎么样了"。
    """
    t = {**DEFAULTS, **(thresholds or {})}
    if t['exclude_st'] and row.get('is_st'):
        return False
    if t['exclude_one_word'] and row.get('one_word_limit'):
        return False
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


def _sector_data(http, now_ts, errors, stale, sector_dir=None):
    """板块数据：**东财优先，取不到退新浪**。返回
    {'rows','total','scope','source','degraded','resolve'} 或 None。

    为什么东财是主力而不是新浪（2026-09-24 实测，详见 sina_sectors 模块文档）：
    东财每只股票的行业在响应里白送（f100），覆盖 100%，不需要映射表；而新浪那套
    行业分类只覆盖约 49% 的沪深A股、科创板一只都没有，拿它当主力会把近一半候选
    静默判成"板块未匹配"。东财一页只回 100 个板块这件事对本用途**不是缺陷**——
    判据是"该股所属板块在主力净流入占比前 100 名内"这个成员判定，排不进前 100
    本来就说明板块不够强，截断即过滤。

    退到新浪时 `degraded=True`：口径变粗（48 vs 496）且约半数个股查不到归属，
    调用方必须把这件事显式告诉用户，不能让它看起来和平时一样。
    """
    import sina_sectors

    em = _with_cache('sectors_eastmoney',
                     lambda: market_rankings.fetch_sector_rows('industry', http, fid='f184',
                                                               retries=0),
                     now_ts, errors, stale)
    if em:
        rows, total, scope = em
        by_name = {r['name']: r for r in rows}
        return {'rows': rows, 'total': total, 'scope': scope, 'source': 'eastmoney',
                'degraded': False,
                'resolve': lambda s: by_name.get(s.get('industry')) if s.get('industry') else None}

    def sina():
        rows, total, scope = sina_sectors.fetch_sectors(http)
        membership = sina_sectors.load_map(sector_dir)
        if not membership:
            raise sina_sectors.SinaError('个股→行业映射表不存在或已过期，无法用新浪兜底')
        return rows, total, scope, membership

    got = _with_cache('sectors_sina', sina, now_ts, errors, stale)
    if not got:
        return None
    rows, total, scope, membership = got
    by_name = {r['name']: r for r in rows}
    by_symbol = membership['by_symbol']
    return {'rows': rows, 'total': total, 'source': 'sina', 'degraded': True,
            'scope': '%s（兜底口径：仅覆盖 %d 只个股，科创板无归属）' % (scope, len(by_symbol)),
            'resolve': lambda s: by_name.get(by_symbol.get(s['symbol']))}


def scan(now=None, http=None, quote_fn=None, now_ts=None, thresholds=None, sector_dir=None):
    """跑一次扫描。永不抛出——定时任务里一次失败不能影响下一次。"""
    now = now or datetime.now(CST)
    now_ts = _time.time() if now_ts is None else now_ts
    quote_fn = quote_fn or live_quote.snapshot
    errors, stale = {}, []

    # 板块优先走新浪：48 个行业一次拿全（东财一页只回 100/496，永远是残缺的），
    # 而且新浪是另一台主机、当天实测零限流。取不到才退回东财。
    sectors = _sector_data(http, now_ts, errors, stale, sector_dir)

    # retries=0：东财按出口 IP 限流，请求越密封得越久。失败时再重试只会把封禁拖长，
    # 而 stale 回退 + 5 分钟后的下一轮本来就兜得住。
    by_amount = _with_cache('inflow_amount',
                            lambda: market_rankings.fetch_stocks('inflow', http, size=PAGE,
                                                                 fid='f62', pz=PAGE, retries=0),
                            now_ts, errors, stale)
    by_ratio = _with_cache('inflow_ratio',
                           lambda: market_rankings.fetch_stocks('inflow', http, size=PAGE,
                                                                fid='f184', pz=PAGE, retries=0),
                           now_ts, errors, stale)
    quotes = _with_cache('indices', lambda: quote_fn(list(BENCHMARKS)), now_ts, errors, stale)

    merged = {}
    for s in (by_amount or []) + (by_ratio or []):
        merged.setdefault(s['symbol'], s)
    changes = index_changes(quotes)

    resolve = sectors['resolve'] if sectors else (lambda _s: None)
    rows = build_rows(list(merged.values()), resolve, changes,
                      sector_source=sectors['source'] if sectors else None)
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
                     'sectors_fetched': len(sectors['rows']) if sectors else 0,
                     'sectors_total': sectors['total'] if sectors else None,
                     'sector_scope': sectors['scope'] if sectors else '',
                     'sector_source': sectors['source'] if sectors else None,
                     'sector_degraded': bool(sectors and sectors.get('degraded')),
                     # 板块那一次请求失败时，所有行的 sector_matched 都是 False，命中数会变成 0。
                     # 那是"没法判断"，不是"没有符合条件的票"——界面必须能把两者分开说。
                     'sector_data_available': bool(sectors),
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


# --- 实盘自检 -----------------------------------------------------------------

def verify(now=None, http=None, sector_dir=None):
    """跑一轮真实扫描，逐条核对那些**只能在交易时段用真数据验证**的假设。

    2026-09-24 上线时有三件事没验成（东财当天把开发机限流封了一下午），这个函数把
    "当时要验什么"固化下来，免得靠人记：

      A. 东财的 f100（个股所属行业）与板块清单的名字**精确匹配率**——漏斗最后一层全靠它。
         当天只有一轮 16/16 命中全匹配的旁证，不是全量统计。
      B. 一字涨停与 ST **确实不会进命中列表**（代码有测试，但没在真数据上跑过）。
      C. 服务器的限流表现与开发机是不是一回事（ROADMAP 第 12 项：境外机房可能不同）。

    每条给 pass / fail / skip 和一句人话。**必须在交易时段跑**：收盘后板块与个股的
    涨跌幅都定格，一字板判据仍有效，但"此刻资金流"和限流表现都不具代表性。
    """
    now = now or datetime.now(CST)
    session = live_quote.clock_session(now)
    in_session = session in ('morning', 'afternoon')
    out = scan(now, http=http, sector_dir=sector_dir)
    rows, hits, u = out['rows'], out['hits'], out['universe']
    checks = []

    def add(key, status, note):
        checks.append({'check': key, 'status': status, 'note': note})

    if u['sector_source'] != 'eastmoney':
        add('eastmoney_reachable', 'fail',
            '东财板块没取到，本轮走的是 %s 兜底：%s' % (u['sector_source'],
                                                out['errors'].get('sectors_eastmoney', '')))
    else:
        add('eastmoney_reachable', 'pass', '东财板块与个股榜单都取到了')

    if not rows:
        add('sector_match_rate', 'skip', '一只股票都没取到，无法统计（多半是东财被限流）')
        add('exclusions', 'skip', '同上')
    else:
        matched = sum(r['sector_matched'] for r in rows)
        rate = matched / len(rows) * 100
        unmatched = sorted({r['industry'] for r in rows if not r['sector_matched'] and r['industry']})
        add('sector_match_rate', 'pass' if rate >= 90 else 'fail',
            '%d/%d = %.1f%% 的个股匹配到板块%s' % (
                matched, len(rows), rate,
                '；未匹配的行业名示例：' + '、'.join(unmatched[:8]) if unmatched else ''))

        st_rows = [r for r in rows if r['is_st']]
        ow_rows = [r for r in rows if r['one_word_limit']]
        bad = [r['name'] for r in hits if r['is_st'] or r['one_word_limit']]
        add('exclusions', 'fail' if bad else 'pass',
            '留档里 ST %d 只、一字板 %d 只；命中里 %s' % (
                len(st_rows), len(ow_rows),
                '混进了 ' + '、'.join(bad) if bad else '两者都是 0（符合预期）'))

    add('trading_session', 'pass' if in_session else 'skip',
        '当前时段 %s%s' % (session, '' if in_session else '——限流表现与资金流不具代表性，结论仅供参考'))
    return {'generated_at': now.isoformat(), 'session': session, 'checks': checks,
            'universe': u, 'errors': out['errors'], 'stale': out['stale'],
            'ok': all(c['status'] != 'fail' for c in checks)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, default=Path(os.environ.get('HISTORY_DIR', '.history')))
    p.add_argument('--force-session', action='store_true', help='忽略时段闸门（本地调试用）')
    p.add_argument('--no-record', action='store_true', help='只扫描不留档')
    p.add_argument('--verify', action='store_true',
                   help='实盘自检：核对板块匹配率、一字板/ST 排除、限流表现（须在交易时段跑）')
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    if a.verify:
        result = verify()
        if a.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            mark = {'pass': 'PASS', 'fail': 'FAIL', 'skip': 'SKIP'}
            print('实盘自检 %s · 时段 %s' % (result['generated_at'], result['session']))
            for c in result['checks']:
                print('  [%s] %-20s %s' % (mark[c['status']], c['check'], c['note']))
            print('\n总体：%s' % ('通过' if result['ok'] else '有未通过项，见上'))
        return 0 if result['ok'] else 1
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
