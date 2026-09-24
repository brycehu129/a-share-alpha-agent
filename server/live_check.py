"""用实时价重算策略口径的**纯规则**复核层。这里没有任何 AI。

为什么要单独一层：alpha_engine 的候选是基于**上一个已收盘交易日**算出来的，
而你看盘的时候价格已经变了。一只昨天刚好卡在"MA5偏离 5.8%（策略允许 0–6%）"
的突破候选，今天再涨 3% 就已经出界了——这件事必须用规则算清楚，而不是让模型
去猜。AI 只负责解读这一层算出来的事实。

三条口径约束：

1. **不把盘中价当收盘数据。** 本模块算出来的东西只进分析报告，绝不回流到
   predictions/ outcomes/ 或虚拟账户——那些仍然只认已完成的日线（见
   alpha_data.completed_day 的 15:10 规则和 STRATEGY.md）。
2. **复权口径。** series 缓存是腾讯前复权序列（最新一根就是真实价格），实时价是
   未复权现价，最近这段两者同基准。万一缓存期间发生除权，比较"实时昨收"和
   "缓存里上一个交易日的收盘"就会对不上——对不上就标记出来，不静默按错误基准算。
3. **缺数据就说缺数据。** 任何一只股票算不出某个指标，就在 issues 里写明原因，
   不用默认值凑数。
"""
import argparse
import json
import statistics
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import book_levels
import book_state
from collect_quotes import CST
from dashboard_export import latest
from shortterm_model import BREAKOUT, PULLBACK
from tushare_sync import read

# 实时昨收和缓存里上一交易日收盘价的允许偏差。超过就认为缓存过期或发生了除权，
# 不能再拿这份缓存的均线和实时价混算。
ADJUST_TOLERANCE_PCT = 1.0


def _f(value):
    return float(value) if value not in (None, '') else None


def load_series(history, symbol):
    """优先用 tushare 桥接序列（和 alpha_engine 同源），退回腾讯抓取的缓存。"""
    for folder in ('tushare_series', 'series'):
        path = history / 'alpha_data' / folder / (symbol + '.json')
        if path.exists():
            data = read(path)
            return data.get('bars') or [], folder, data.get('fetched_at')
    return [], None, None


def price_facts(bars, quote):
    """用实时价替换当日那一根，重算均线类指标。

    关键点：series 里可能已经有今天的那一根（收盘后抓的），也可能没有（盘中）。
    所以统一取"日期早于本次报价日期"的历史收盘，再把实时价接在后面，两种情况
    结果一致，也不会把今天算两遍。"""
    facts, issues = {}, []
    quote_date = quote.get('quote_date')
    prior = [b for b in bars if b['date'] < quote_date]
    if len(prior) < 20:
        issues.append('历史日线不足20根（现有%d根），均线与20日高点无法计算' % len(prior))
        return facts, issues
    closes = [float(b['close']) for b in prior]
    last = _f(quote.get('last'))
    previous = _f(quote.get('previous_close'))

    if previous and closes:
        drift = abs(previous / closes[-1] - 1) * 100
        facts['adjustment_drift_pct'] = round(drift, 4)
        if drift > ADJUST_TOLERANCE_PCT:
            issues.append('实时昨收(%s)与缓存上一交易日收盘(%s)相差%.2f%%，'
                          '可能发生除权或缓存过期，均线结果不可信'
                          % (previous, closes[-1], drift))
    window = closes + [last]
    facts['ma5'] = round(statistics.mean(window[-5:]), 4)
    facts['ma20'] = round(statistics.mean(window[-20:]), 4)
    if len(window) >= 60:                 # 不足60根就不给 MA60，而不是拿短窗口的均值冒充
        facts['ma60'] = round(statistics.mean(window[-60:]), 4)
        facts['ma60_deviation_pct'] = round((last / facts['ma60'] - 1) * 100, 4)
    facts['ma5_deviation_pct'] = round((last / facts['ma5'] - 1) * 100, 4)
    facts['ma20_deviation_pct'] = round((last / facts['ma20'] - 1) * 100, 4)
    high20 = max(closes[-20:])
    facts['low20_close'] = round(min(closes[-20:]), 4)
    facts['high20_close'] = round(high20, 4)
    facts['high20_ratio'] = round(last / high20, 4)
    facts['distance_to_high20_pct'] = round((last / high20 - 1) * 100, 4)
    facts['return3_pct'] = round((last / closes[-3] - 1) * 100, 4) if len(closes) >= 3 else None
    facts['return2_pct'] = round((last / closes[-2] - 1) * 100, 4) if len(closes) >= 2 else None
    facts['prior_close_date'] = prior[-1]['date']
    facts['prior_sessions'] = len(prior)
    return facts, issues


def limit_facts(quote):
    """距涨停/跌停还有多少。指数没有涨跌停，返回空。"""
    last, up, down = _f(quote.get('last')), _f(quote.get('limit_up')), _f(quote.get('limit_down'))
    if not last or quote.get('is_index'):
        return {}
    facts = {}
    if up:
        facts['limit_up'] = up
        facts['to_limit_up_pct'] = round((up / last - 1) * 100, 4)
        facts['at_limit_up'] = facts['to_limit_up_pct'] <= 0.05
    if down:
        # 两个方向都表示"现价还需要涨/跌百分之几才到限价"，分母统一是现价。
        # 用 (last/down - 1) 算跌停会得到"跌停价涨多少到现价"，数值偏大，
        # 看起来离跌停比实际更远。
        facts['limit_down'] = down
        facts['to_limit_down_pct'] = round((1 - down / last) * 100, 4)
        facts['at_limit_down'] = facts['to_limit_down_pct'] <= 0.05
    return facts


def breakout_gates(facts, quote):
    """突破track的门槛，用实时价重新判一遍。返回 {门槛名: (是否通过, 说明)}。"""
    ratio = _f(quote.get('volume_ratio'))
    gates = {}
    if facts.get('return3_pct') is not None:
        ok = facts['return3_pct'] >= BREAKOUT['return3_min']
        gates['近3日涨幅'] = (ok, '%.2f%% (要求≥%.1f%%)' % (facts['return3_pct'], BREAKOUT['return3_min']))
    if facts.get('high20_ratio') is not None:
        ok = facts['high20_ratio'] >= 0.99
        gates['接近20日新高'] = (ok, '现价/20日最高收盘=%.3f (要求≥0.990)' % facts['high20_ratio'])
    if facts.get('ma5_deviation_pct') is not None:
        ok = 0 <= facts['ma5_deviation_pct'] <= BREAKOUT['ma5_deviation_max']
        gates['MA5偏离'] = (ok, '%.2f%% (要求0–%.0f%%)' % (facts['ma5_deviation_pct'], BREAKOUT['ma5_deviation_max']))
    if ratio is not None:
        # 腾讯的量比是"当前成交量 / 过去5日同时段均量"，和策略里用收盘全日量算的
        # volume_ratio_5d 口径不同，盘中会偏小。只作参考，不作判定。
        gates['量比(参考)'] = (None, '%.2f (腾讯口径，与策略的日量比不可直接比较)' % ratio)
    return gates


def pullback_gates(facts, quote):
    gates = {}
    if facts.get('return2_pct') is not None:
        ok = facts['return2_pct'] <= PULLBACK['return2_max']
        gates['近2日涨跌'] = (ok, '%.2f%% (要求≤%.1f%%)' % (facts['return2_pct'], PULLBACK['return2_max']))
    if facts.get('ma20_deviation_pct') is not None:
        ok = 0 <= facts['ma20_deviation_pct'] <= PULLBACK['deviation_max']
        gates['MA20偏离'] = (ok, '%.2f%% (要求0–%.0f%%)' % (facts['ma20_deviation_pct'], PULLBACK['deviation_max']))
    last, opening = _f(quote.get('last')), _f(quote.get('open'))
    if last and opening:
        gates['当日收阳'] = (last > opening, '现价%.2f vs 今开%.2f' % (last, opening))
    return gates


def entry_band(forecast, quote):
    """冻结计划的入场带：现价相对参考价的偏离是否还在 ±3% 内。"""
    if not forecast or not forecast.get('reference_price'):
        return {}
    policy = forecast.get('policy') or {}
    low, high = policy.get('entry_gap_min', -0.03), policy.get('entry_gap_max', 0.03)
    reference = float(forecast['reference_price'])
    last = _f(quote.get('last'))
    gap = last / reference - 1
    return {'reference_price': reference, 'entry_low': round(reference * (1 + low), 4),
            'entry_high': round(reference * (1 + high), 4),
            'gap_pct': round(gap * 100, 4), 'in_band': low <= gap <= high,
            'eligible_from': forecast.get('eligible_from'), 'as_of': forecast.get('as_of')}


def holding_facts(holding, quote, trades=None):
    """持仓事实。holding 应是 book_levels.enrich 之后的行：止损/止盈位是系统按策略（ATR）从成本价算的，
    做T底仓是今天可卖的老仓——都不是用户声明的，下游据此措辞。没 enrich 过就是 None，不代填默认值。

    trades 传了才会算综合盈亏（浮动+已实现）与等效成本（book_pnl.py）——旧调用方不传就还是原来的
    浮动盈亏口径，不强制所有调用方都先取一遍成交流水。"""
    last = _f(quote.get('last'))
    cost, shares = float(holding['cost_price']), int(holding['shares'])
    value = round(last * shares, 2)
    pnl = round((last - cost) * shares, 2)
    stop, target = holding.get('stop_price'), holding.get('target_price')
    out = {'shares': shares, 'cost_price': cost, 'market_value': value,
           'unrealized_pnl': pnl, 'unrealized_pct': round((last / cost - 1) * 100, 4),
           'opened_on': holding.get('opened_on'), 'note': holding.get('note') or None,
           'stop_price': stop, 'target_price': target,
           't_base_shares': holding.get('t_base_shares') or 0,
           'to_stop_pct': round((last / stop - 1) * 100, 4) if stop else None,
           'to_target_pct': round((target / last - 1) * 100, 4) if target else None}
    if trades is not None:
        import book_pnl
        realized = book_pnl.realized_pnl(trades, holding['symbol'])
        out.update(book_pnl.combined(holding, quote, realized))
    return out


def check(history, quotes, holdings=None, watchlist=None, agent=None):
    """把实时报价和归档里的策略结论拼成结构化事实。

    agent 传 None 时自动读最新一份 agent/<run>.json；传 {} 表示明确不使用候选池
    （比如只想看自选股）。"""
    holdings = {h['symbol']: h for h in (holdings or [])}
    watchlist = {w['symbol']: w for w in (watchlist or [])}
    if agent is None:
        _, agent = latest(history, 'agent')
    agent = agent or {}
    candidates = {c['symbol']: c for c in agent.get('candidates', [])}
    forecasts = {}
    for f in sorted(agent.get('forecasts', []), key=lambda f: f['created_at']):
        forecasts[f['symbol']] = f  # 同一只股票保留最新一条冻结计划
    industries = {}
    screen = agent.get('screen') or {}

    rows = []
    for quote in quotes:
        symbol = quote['symbol']
        if quote.get('is_index') or quote['market'] != 'cn':
            continue
        bars, source, fetched_at = load_series(history, symbol)
        facts, issues = ({}, ['本地没有该股票的日线缓存，无法计算均线与20日高点']) if not bars \
            else price_facts(bars, quote)
        candidate = candidates.get(symbol)
        forecast = forecasts.get(symbol)
        track = (candidate or forecast or {}).get('strategy_type')
        row = {
            'symbol': symbol, 'name': quote['name'] or (candidate or forecast or {}).get('name'),
            'roles': [r for r, ok in (('holding', symbol in holdings), ('watchlist', symbol in watchlist),
                                      ('candidate', candidate is not None),
                                      ('forecast', forecast is not None)) if ok],
            'quote': {k: quote.get(k) for k in (
                'last', 'previous_close', 'open', 'high', 'low', 'change_pct', 'volume_ratio',
                'turnover_pct', 'amplitude_pct', 'amount_wan', 'quote_at', 'age_seconds')},
            'price_facts': facts, 'limit_facts': limit_facts(quote),
            'series_source': source, 'series_fetched_at': fetched_at, 'issues': issues,
        }
        if candidate:
            row['strategy'] = {
                'track': track, 'score': candidate.get('score'),
                'industry': candidate.get('industry'),
                'probability': candidate.get('probability'),
                'reasons': candidate.get('reasons', []),
                'as_of_features': {k: candidate.get(k) for k in (
                    'return20_pct', 'return5_pct', 'excess20_pp', 'deviation_pct',
                    'volume_ratio', 'volume_ratio_5d', 'high20_ratio', 'ma5_deviation_pct')},
                'hotmoney': candidate.get('hotmoney'),
            }
            industries.setdefault(candidate.get('industry'), []).append(symbol)
        if facts and track == 'breakout':
            row['live_gates'] = breakout_gates(facts, quote)
        elif facts and track == 'pullback':
            row['live_gates'] = pullback_gates(facts, quote)
        if forecast:
            row['plan'] = entry_band(forecast, quote)
            row['plan']['paper_eligible'] = forecast.get('paper_eligible')
            row['plan']['plan_reasons'] = forecast.get('plan_reasons', [])
        if symbol in holdings:
            peak = book_state.touch_peak(symbol, _f(quote.get('last')), datetime.now(CST), bars,
                                         holdings[symbol].get('opened_on'))
            row['holding'] = holding_facts(
                book_levels.enrich(holdings[symbol], bars, quote.get('quote_date'), peak_price=peak), quote)
        if symbol in watchlist:
            row['watch'] = {'note': watchlist[symbol].get('note') or None}
        rows.append(row)

    rows.sort(key=lambda r: (0 if 'holding' in r['roles'] else 1, r['symbol']))
    return {
        'generated_at': datetime.now(CST).isoformat(),
        'rows': rows,
        'strategy_context': {
            'cutoff': screen.get('cutoff'), 'market_score': screen.get('market_score'),
            'market_score_pause': screen.get('market_score_pause'),
            'regime': screen.get('regime'), 'coverage_pct': screen.get('coverage_pct'),
            'complete': screen.get('complete'),
            'agent_generated_at': agent.get('generated_at'), 'agent_status': agent.get('status'),
            'agent_version': agent.get('version'), 'agent_issues': agent.get('issues', []),
        },
        'caveats': [
            '本层用实时价重算的均线、20日高点、涨跌幅只用于分析展示；'
            '策略的冻结预测与虚拟账户仍然只认已完成的日线，不受这里影响。',
            '腾讯量比是"当前成交量/过去5日同时段均量"，与策略里按全日成交量算的量比'
            '口径不同，盘中会系统性偏小，不能直接拿来判定策略门槛。',
            '涨跌停价取自行情源，未经交易所逐股核验；ST、停牌、特殊标的可能有例外。',
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--symbols', help='逗号分隔；不给就用持仓+自选+候选池')
    p.add_argument('--json', action='store_true')
    a = p.parse_args()

    import live_quote
    import portfolio_book
    holdings = portfolio_book.load('holdings')
    watchlist = portfolio_book.load('watchlist')
    if a.symbols:
        symbols = [s.strip() for s in a.symbols.split(',') if s.strip()]
        agent = None
    else:
        _, agent = latest(a.history, 'agent')
        symbols = sorted({h['symbol'] for h in holdings} | {w['symbol'] for w in watchlist} |
                         {c['symbol'] for c in (agent or {}).get('candidates', [])})
    if not symbols:
        print('没有需要检查的股票：持仓、自选股和候选池都是空的。')
        return 1
    snap = live_quote.snapshot(symbols)
    result = check(a.history, snap['quotes'], holdings, watchlist, agent)
    if a.json:
        print(json.dumps({'snapshot': snap, 'check': result}, ensure_ascii=False, indent=2))
        return 0
    ctx = result['strategy_context']
    print('复核时间 %s · 候选池截至 %s · 市场评分 %s'
          % (result['generated_at'], ctx.get('cutoff'), ctx.get('market_score')))
    for row in result['rows']:
        pf = row['price_facts']
        print('\n%s %s  [%s]' % (row['symbol'], row['name'], '/'.join(row['roles']) or '—'))
        print('  现价 %s  涨跌 %s%%  MA5偏离 %s  MA20偏离 %s  距20日高点 %s%%' % (
            row['quote']['last'], row['quote']['change_pct'],
            pf.get('ma5_deviation_pct'), pf.get('ma20_deviation_pct'),
            pf.get('distance_to_high20_pct')))
        for name, (ok, detail) in (row.get('live_gates') or {}).items():
            mark = '参考' if ok is None else ('通过' if ok else '不通过')
            print('  门槛 %s: %s — %s' % (name, mark, detail))
        if row.get('plan'):
            print('  计划 参考价 %s 入场带 %s–%s 当前偏离 %s%% 在带内=%s' % (
                row['plan'].get('reference_price'), row['plan'].get('entry_low'),
                row['plan'].get('entry_high'), row['plan'].get('gap_pct'), row['plan'].get('in_band')))
        if row.get('holding'):
            h = row['holding']
            print('  持仓 %d股 成本 %s 浮动 %s (%s%%)' % (
                h['shares'], h['cost_price'], h['unrealized_pnl'], h['unrealized_pct']))
        for issue in row['issues']:
            print('  ! ' + issue)
    for f in snap['failures']:
        print('取价失败 %s: %s' % (f['symbol'], f['reason']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
