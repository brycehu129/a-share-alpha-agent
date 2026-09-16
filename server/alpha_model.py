"""Versioned deterministic research strategy and purged historical calibration.

No fitted thresholds, future prices in features, or probability from score/100.
Historical estimates use today's membership and must remain labelled exploratory.
"""
import math
import statistics
from collections import Counter, defaultdict
from datetime import date

VERSION = 'alpha-shadow-0.2-observed'
POLICY = {'capital': 100000, 'max_positions': 3, 'max_weight': 0.30,
          'risk_per_trade': 0.02, 'stop_pct': 0.06, 'target_pct': 0.12,
          'hold_sessions': 10, 'entry_gap_min': -0.03, 'entry_gap_max': 0.03,
          'slippage': 0.001, 'commission': 0.0003, 'minimum_commission': 5,
          'transfer_fee': 0.00001, 'sell_tax': 0.0005,
          'drawdown_pause': 0.20, 'drawdown_hard_stop': 0.50,
          'label_cost_pct': 0.5, 'coverage_required': 0.95}
TARGET = '下一可验证交易日开盘至第10个交易间隔收盘，扣0.5%假设往返成本后盈利且跑赢沪深300价格指数'


def clip(value, low=0, high=100):
    return max(low, min(high, value))


def percentiles(values):
    ordered = sorted(values.values())
    n = len(ordered)
    # Tie-aware midpoint ranks; no arbitrary ordering among equal observations.
    positions, below = {}, 0
    for v, count in sorted(Counter(ordered).items()):
        positions[v] = (below + count / 2) / n * 100
        below += count
    return {k: positions[v] for k, v in values.items()}


def base_reason(stock, cutoff):
    if stock.get('list_status') != 'L':
        return '非在市股票'
    name = stock.get('name', '').upper().replace(' ', '')
    if 'ST' in name or '退' in name:
        return 'ST或退市名称标记'
    try:
        listed = date.fromisoformat(stock['list_date'][:4] + '-' + stock['list_date'][4:6] + '-' + stock['list_date'][6:8])
        if (date.fromisoformat(cutoff) - listed).days < 180:
            return '上市未满180自然日'
    except (KeyError, TypeError, ValueError):
        return '上市日期缺失'
    if not stock.get('industry') or stock['industry'] in ('-', '--'):
        return '行业缺失'
    return None


def features(bars, benchmark, cutoff):
    dates = [b['date'] for b in benchmark if b['date'] <= cutoff][-21:]
    if len(dates) != 21 or dates[-1] != cutoff:
        raise ValueError('基准窗口不足')
    by_date = {b['date']: b for b in bars}
    if any(d not in by_date for d in dates):
        raise ValueError('21交易日窗口缺数或停牌')
    closes = [float(by_date[d]['close']) for d in dates]
    if by_date[cutoff].get('is_st') == '1':
        raise ValueError('源日线风险警示标记')
    volumes = [float(by_date[d]['volume_raw']) for d in dates]
    if min(closes) <= 0 or min(volumes[-5:]) <= 0:
        raise ValueError('价格或最近成交量无效')
    b = {b['date']: float(b['close']) for b in benchmark}
    ret = (closes[-1] / closes[0] - 1) * 100
    ret5 = (closes[-1] / closes[-6] - 1) * 100
    bench = (b[dates[-1]] / b[dates[0]] - 1) * 100
    return {'return20_pct': ret, 'return5_pct': ret5, 'excess20_pp': ret-bench,
            'deviation_pct': (closes[-1] / statistics.mean(closes[-20:]) - 1) * 100,
            'volume_ratio': volumes[-1] / statistics.mean(volumes[-20:]),
            'liquidity_proxy': statistics.mean(c*v for c, v in zip(closes[-20:], volumes[-20:])),
            'reference_adjusted': closes[-1]}


def screen(stocks, series, benchmark, cutoff):
    excluded, rows, counts = {}, [], Counter()
    for s in stocks:
        code = s['ts_code'][-2:].lower() + s['ts_code'][:6]
        reason = base_reason(s, cutoff)
        if reason:
            excluded[code] = reason
            continue
        counts[s['industry']] += 1
        try:
            f = features(series.get(code, []), benchmark, cutoff)
            rows.append({'symbol': code, 'name': s['name'], 'industry': s['industry'], **f})
        except (ValueError, KeyError, ArithmeticError):
            excluded[code] = '日线窗口缺失或无有效成交'
    eligible = sum(counts.values())
    coverage = len(rows) / eligible if eligible else 0
    sectors = defaultdict(list)
    for r in rows:
        sectors[r['industry']].append(r)
    industry_rows = []
    for name, rs in sectors.items():
        if len(rs) >= 5 and len(rs) / counts[name] >= 0.9:
            median = statistics.median(r['excess20_pp'] for r in rs)
            breadth = sum(r['return5_pct'] > 0 for r in rs) / len(rs) * 100
            industry_rows.append({'name': name, 'count': len(rs), 'total': counts[name],
                                  'median_excess20_pp': median, 'positive5_pct': breadth})
    ip = percentiles({r['name']: r['median_excess20_pp'] for r in industry_rows}) if industry_rows else {}
    for r in industry_rows:
        r['score'] = round(0.7 * ip[r['name']] + 0.3 * r['positive5_pct'], 2)
    industry_rows.sort(key=lambda r: (-r['score'], r['name']))
    strong = {r['name']: r for r in industry_rows[:max(1, math.ceil(len(industry_rows)*0.1))]
              if r['median_excess20_pp'] > 0 and r['positive5_pct'] >= 50}
    lp = percentiles({r['symbol']: r['liquidity_proxy'] for r in rows}) if rows else {}
    rp = percentiles({r['symbol']: r['excess20_pp'] for r in rows}) if rows else {}
    b = [x for x in benchmark if x['date'] <= cutoff]
    market_score = clip(50 + (float(b[-1]['close'])/float(b[-21]['close'])-1)*200) if len(b) >= 21 else 0
    regime = '趋势' if market_score >= 50 else '偏弱'
    candidates = []
    for r in rows:
        reason = None
        if r['industry'] not in strong:
            reason = '行业未进入强势前10%'
        elif lp[r['symbol']] < 20:
            reason = '相对流动性后20%'
        elif r['excess20_pp'] <= 0 or r['return20_pct'] <= 0 or r['return5_pct'] <= 0:
            reason = '相对强度或短期趋势不足'
        elif not 0 <= r['deviation_pct'] <= 12:
            reason = '低于均线或偏离均线超过12%'
        elif r['volume_ratio'] > 3:
            reason = '成交量超过20日均值3倍'
        if reason:
            excluded[r['symbol']] = reason
            continue
        penalty = max(0, r['deviation_pct']-5)*2
        r['theme_score'] = strong[r['industry']]['score']
        r['leader_score'] = rp[r['symbol']]
        r['score'] = round(clip(r['theme_score']*0.4+r['leader_score']*0.6-penalty), 2)
        r['bucket'] = '80+' if r['score'] >= 80 else '<80'
        r['regime'] = regime
        r['reasons'] = ['行业强度前10%', '20日跑赢沪深300', '近5日上涨', 'MA20偏离不超过12%']
        candidates.append(r)
    candidates.sort(key=lambda r: (-r['score'], r['symbol']))
    return {'cutoff': cutoff, 'listed': len(stocks), 'eligible': eligible, 'valid': len(rows),
            'coverage_pct': round(coverage*100, 2), 'complete': coverage >= POLICY['coverage_required'],
            'market_score': round(market_score, 2), 'regime': regime, 'industries': industry_rows,
            'candidates': candidates, 'excluded': excluded, 'exclusion_counts': dict(Counter(excluded.values()))}


def label(bars, benchmark, entry_day, horizon):
    dates = [b['date'] for b in benchmark]
    if entry_day not in dates or dates.index(entry_day)+horizon >= len(dates):
        return None
    start = dates.index(entry_day)
    window = dates[start:start+horizon+1]
    ss, bb = {b['date']: b for b in bars}, {b['date']: b for b in benchmark}
    if any(d not in ss for d in window):
        return None
    entry = float(ss[entry_day]['open'])
    ret = (float(ss[window[-1]]['close'])/entry-1)*100 - POLICY['label_cost_pct']
    br = (float(bb[window[-1]]['close'])/float(bb[entry_day]['open'])-1)*100
    return {'horizon': horizon, 'entry_day': entry_day, 'end_day': window[-1],
            'return_pct': round(ret, 4), 'benchmark_pct': round(br, 4), 'excess_pp': round(ret-br, 4),
            'win': ret > 0 and ret > br,
            'mfe_pct': round((max(float(ss[d]['high']) for d in window)/entry-1)*100, 4),
            'mae_pct': round((min(float(ss[d]['low']) for d in window)/entry-1)*100, 4)}


def estimate(samples, cutoff, bucket=None, regime=None):
    # Labels must have matured STRICTLY BEFORE this decision; no overlapping
    # future labels in the training side of walk-forward scoring.
    selected = [s for s in samples if s['end_day'] < cutoff and
                (bucket is None or s['bucket'] == bucket) and (regime is None or s['regime'] == regime)]
    dates = len({s['entry_day'] for s in selected})
    n = len(selected)
    wins = sum(s['win'] for s in selected)
    out = {'n': n, 'cohorts': dates, 'wins': wins, 'probability': None, 'low': None, 'high': None,
           'mean_return_pct': None, 'status': 'insufficient'}
    if n < 30 or dates < 15:
        return out
    # A beta(1,1) posterior mean. Approx interval is deliberately widened by
    # treating date cohorts as effective observations, not independent stocks.
    p = (wins+1)/(n+2)
    z, effective = 1.96, dates
    center = (p+z*z/(2*effective))/(1+z*z/effective)
    radius = z*math.sqrt(p*(1-p)/effective+z*z/(4*effective**2))/(1+z*z/effective)
    return {**out, 'probability': round(p*100, 2), 'low': round((center-radius)*100, 2),
            'high': round((center+radius)*100, 2), 'mean_return_pct': round(statistics.mean(s['return_pct'] for s in selected), 2),
            'status': 'historical_exploratory'}


def walk_forward(stocks, series, benchmark, cutoff):
    dates = [b['date'] for b in benchmark if b['date'] <= cutoff]
    samples, predictions = [], []
    # Non-overlapping 10-session outcomes with a one-session purge gap.
    for i in range(20, len(dates)-11, 11):
        day = dates[i]
        screened = screen(stocks, series, benchmark, day)
        if not screened['complete'] or screened['market_score'] < 40:
            continue
        for c in screened['candidates'][:3]:
            outcome = label(series[c['symbol']], benchmark, dates[i+1], 10)
            if outcome is None:
                continue
            prior = estimate(samples, day, c['bucket'], c['regime'])
            if prior['probability'] is not None:
                predictions.append({'p': prior['probability']/100, 'y': int(outcome['win']),
                                    'signal_day': day, 'training_n': prior['n']})
            samples.append({**outcome, 'symbol': c['symbol'], 'signal_day': day,
                            'bucket': c['bucket'], 'regime': c['regime']})
    brier = statistics.mean((p['p']-p['y'])**2 for p in predictions) if predictions else None
    return {'samples': samples, 'walk_forward_n': len(predictions), 'brier': round(brier, 4) if brier is not None else None,
            'limitations': ['当前在市名单与当前行业分类回看历史，存在幸存者及分类回看偏差',
                '历史标签按次日开盘估计，未复现停牌/涨跌停成交、公告与历史风险标记',
                '历史频率仅为研究估计，不能视为已验证的个股真实胜率；分数不是概率',
                '同日股票相关，区间按日期样本量保守放宽，仍不是严格校准保证']}
