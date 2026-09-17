"""Short-term (1-3 session hold) breakout / pullback screens.

New version, replacing alpha-shadow-0.2-observed's T+10 target going forward
(old T+10 predictions/outcomes stay frozen and readable under their own
VERSION string -- nothing about them changes). Two independent, separately
tagged and separately calibrated selection rules live here:

  - 'breakout': a name just starting to move -- fresh volume expansion TODAY
    (not yesterday), near a 20-session high, modest prior 20-day excess.
    This is the opposite selection direction from the old medium-term
    strategy, which explicitly preferred the MOST extended names -- exactly
    the shape that produced today's 海洋王-style reversal.
  - 'pullback': a name already in a confirmed medium-term uptrend (positive
    20-day excess, strong industry) that paused on light volume over the
    last two sessions without breaking MA20, entered on the first green
    close.

Both reuse alpha_model.features() for the already-validated 21-session
window (MA20 deviation, 20d/5d return, volume ratio, liquidity proxy) and
alpha_model.screen()'s industry-strength computation (recomputed from its
own 'industries' output so the two modules never disagree about which
industries currently qualify). Every candidate carries 'strategy_type';
calibration and review_pipeline.py grouping must split on it -- the two
tracks are validated independently and are never pooled into one win rate.
"""
import statistics
from collections import Counter

from alpha_model import POLICY, TARGET as MID_TARGET, features, base_reason, percentiles, clip, label, estimate
from alpha_model import screen as screen_mid

VERSION = 'alpha-shadow-0.3-shortterm'
TARGET = '下一可验证交易日开盘至第3个交易间隔收盘（1/2日辅助验收），扣0.5%假设往返成本后盈利且跑赢同期沪深300价格指数'

# Only what differs from the medium-term POLICY; everything else (capital,
# max_positions, max_weight, risk_per_trade, drawdown_pause/hard_stop, cost
# assumptions) is untouched pending a separate portfolio-construction decision.
SHORT_POLICY = {**POLICY, 'hold_sessions': 3, 'stop_pct': 0.03, 'target_pct': 0.05}

# Starting thresholds, not yet validated against a real walk-forward run.
BREAKOUT = {'return3_min': 3.0, 'volume_ratio_5d_min': 1.8, 'excess20_max': 15.0, 'ma5_deviation_max': 6.0}
PULLBACK = {'return2_max': 0.0, 'volume_ratio_20d_max': 1.0, 'deviation_max': 8.0}

# Bounded additive nudge from same-day hot-money/limit-board activity
# (tushare_data/hm_detail, tushare_data/limit_list_d via hotmoney_features.py).
# Confirmation only, never a gate: absence of data (signal is None/empty, the
# normal case while these two interfaces are only rolling-synced a handful of
# days back via manual workflow_dispatch) must score exactly like "no
# hot-money activity" -- zero adjustment, not an exclusion or a penalty.
# Not yet validated against real outcomes; revisit once walk_forward_short()
# has enough real hm_detail/limit_list_d coverage to check whether it helps.
HOTMONEY = {'net_amount_scale': 1e7, 'net_amount_cap': 6.0, 'limit_up_bonus': 4.0}


def hotmoney_adjustment(signal, include_limit_bonus):
    if not signal:
        return 0.0, []
    adjustment, reasons = 0.0, []
    net = signal.get('hm_net_amount')
    if net:
        bump = clip(net / HOTMONEY['net_amount_scale'], -HOTMONEY['net_amount_cap'], HOTMONEY['net_amount_cap'])
        adjustment += bump
        if bump > 0:
            reasons.append(f'游资席位净买入约{net/1e4:.0f}万元')
        elif bump < 0:
            reasons.append(f'游资席位净卖出约{-net/1e4:.0f}万元')
    if include_limit_bonus and signal.get('limit_status') == 'U':
        adjustment += HOTMONEY['limit_up_bonus']
        reasons.append('当日涨停收盘')
    return adjustment, reasons


def strong_industries(industry_rows):
    """Same top-10%/positive-median/>=50%-breadth rule alpha_model.screen()
    uses, recomputed here from its already-computed industry_rows."""
    if not industry_rows:
        return {}
    ranked = sorted(industry_rows, key=lambda r: (-r['score'], r['name']))
    top_n = max(1, -(-len(ranked) * 10 // 100))  # ceil(10%)
    return {r['name']: r for r in ranked[:top_n] if r['median_excess20_pp'] > 0 and r['positive5_pct'] >= 50}


def short_window(bars, dates):
    """21 aligned dates ending at the window's last date; returns None if the
    window can't be built (missing day, zero close/volume)."""
    by_date = {b['date']: b for b in bars}
    if len(dates) != 21 or any(d not in by_date for d in dates):
        return None
    closes = [float(by_date[d]['close']) for d in dates]
    opens = [float(by_date[d]['open']) for d in dates]
    volumes = [float(by_date[d]['volume_raw']) for d in dates]
    if min(closes) <= 0 or min(volumes[-6:]) <= 0:
        return None
    return {
        'return1_pct': (closes[-1] / closes[-2] - 1) * 100,
        'return2_pct': (closes[-1] / closes[-3] - 1) * 100,
        'return3_pct': (closes[-1] / closes[-4] - 1) * 100,
        'ma5_deviation_pct': (closes[-1] / statistics.mean(closes[-5:]) - 1) * 100,
        'volume_ratio_5d': volumes[-1] / statistics.mean(volumes[-6:-1]),
        'high20_ratio': closes[-1] / max(closes[-21:-1]),
        'is_green': closes[-1] > opens[-1],
    }


def stock_features(stocks, series, benchmark, cutoff):
    """Base features() plus short-horizon fields, keyed by symbol. Freshness
    for the breakout track needs YESTERDAY's volume_ratio_5d too, computed
    the same way one trading day earlier -- that's what tells 'volume just
    started expanding today' apart from 'has been elevated for days'."""
    dates = [b['date'] for b in benchmark if b['date'] <= cutoff]
    if len(dates) < 22:
        return {}
    today_dates, prior_dates = dates[-21:], dates[-22:-1]
    out = {}
    for s in stocks:
        code = s['ts_code'][-2:].lower() + s['ts_code'][:6]
        if base_reason(s, cutoff):
            continue
        bars = series.get(code, [])
        try:
            f = features(bars, benchmark, cutoff)
        except (ValueError, KeyError, ArithmeticError):
            continue
        today = short_window(bars, today_dates)
        yesterday = short_window(bars, prior_dates)
        if today is None or yesterday is None:
            continue
        out[code] = {'symbol': code, 'name': s['name'], 'industry': s['industry'],
                      **f, **today, 'prior_volume_ratio_5d': yesterday['volume_ratio_5d']}
    return out


def screen_breakout(stocks, series, benchmark, cutoff, industry_rows, hotmoney=None):
    strong = strong_industries(industry_rows)
    candidates, excluded = [], {}
    for code, r in stock_features(stocks, series, benchmark, cutoff).items():
        reason = None
        if r['industry'] not in strong:
            reason = '行业未进入强势前10%'
        elif r['excess20_pp'] > BREAKOUT['excess20_max']:
            reason = f"20日超额已超过{BREAKOUT['excess20_max']}pp，不算新鲜突破"
        elif r['return3_pct'] < BREAKOUT['return3_min']:
            reason = '近3日涨幅不足'
        elif r['volume_ratio_5d'] < BREAKOUT['volume_ratio_5d_min']:
            reason = '今日成交量未明显放大'
        elif r['prior_volume_ratio_5d'] >= BREAKOUT['volume_ratio_5d_min']:
            reason = '昨日已放量，非今日首次放量'
        elif r['high20_ratio'] < 0.99:
            reason = '未接近20日新高'
        elif not 0 <= r['ma5_deviation_pct'] <= BREAKOUT['ma5_deviation_max']:
            reason = 'MA5偏离超出0-6%区间'
        if reason:
            excluded[code] = reason
            continue
        signal = (hotmoney or {}).get(code)
        bonus, hm_reasons = hotmoney_adjustment(signal, include_limit_bonus=True)
        score = round(clip(50 + r['return3_pct'] * 5 + (r['volume_ratio_5d'] - 1) * 10 + bonus), 2)
        candidates.append({**r, 'strategy_type': 'breakout', 'score': score, 'hotmoney': signal,
                            'reasons': ['行业强度前10%', f"近3日涨幅≥{BREAKOUT['return3_min']}%",
                                        '今日首次放量', '接近20日新高', 'MA5偏离受控', *hm_reasons]})
    candidates.sort(key=lambda r: (-r['score'], r['symbol']))
    return {'candidates': candidates, 'excluded': excluded, 'exclusion_counts': dict(Counter(excluded.values()))}


def screen_pullback(stocks, series, benchmark, cutoff, industry_rows, hotmoney=None):
    strong = strong_industries(industry_rows)
    candidates, excluded = [], {}
    for code, r in stock_features(stocks, series, benchmark, cutoff).items():
        reason = None
        if r['industry'] not in strong:
            reason = '行业未进入强势前10%'
        elif r['excess20_pp'] <= 0:
            reason = '20日超额未转正，非确认中期强势'
        elif r['return2_pct'] > PULLBACK['return2_max']:
            reason = '近2日未回调'
        elif r['volume_ratio'] > PULLBACK['volume_ratio_20d_max']:
            reason = '回调未缩量'
        elif not 0 <= r['deviation_pct'] <= PULLBACK['deviation_max']:
            reason = '偏离MA20超出0-8%区间'
        elif not r['is_green']:
            reason = '当日未收阳，无反转确认'
        if reason:
            excluded[code] = reason
            continue
        signal = (hotmoney or {}).get(code)
        # No limit_up_bonus here: a pullback candidate closing at the daily
        # limit would already fail return2_pct/volume_ratio above in
        # practice, and 'confirmed by closing at the limit' isn't what this
        # track is screening for -- only the net-buying signal applies.
        bonus, hm_reasons = hotmoney_adjustment(signal, include_limit_bonus=False)
        score = round(clip(50 + r['excess20_pp'] * 1.5 - abs(r['return2_pct']) * 3 + bonus), 2)
        candidates.append({**r, 'strategy_type': 'pullback', 'score': score, 'hotmoney': signal,
                            'reasons': ['行业强度前10%', '20日超额为正', '近2日缩量回调',
                                        '未跌破MA20区间', '当日收阳反转确认', *hm_reasons]})
    candidates.sort(key=lambda r: (-r['score'], r['symbol']))
    return {'candidates': candidates, 'excluded': excluded, 'exclusion_counts': dict(Counter(excluded.values()))}


def screen_short(stocks, series, benchmark, cutoff, tuning=None, hotmoney=None):
    """Orchestrator: reuses alpha_model.screen() once for industry strength
    and market_score/coverage gating (both tracks share the same market-wide
    gate the mid-term strategy uses), then runs the two independent tracks.
    `hotmoney` is the same-day signal dict from hotmoney_features.load()
    (keyed by symbol); None/missing entries score as no signal, never as a
    penalty -- see hotmoney_adjustment()'s docstring."""
    mid = screen_mid(stocks, series, benchmark, cutoff, tuning)
    breakout = screen_breakout(stocks, series, benchmark, cutoff, mid['industries'], hotmoney)
    pullback = screen_pullback(stocks, series, benchmark, cutoff, mid['industries'], hotmoney)
    # Flat, merged exclusion_counts for callers (e.g. dashboard_export.py) that
    # still expect the single-track shape alpha_model.screen() used to return.
    # Per-track breakdowns remain available under breakout/pullback above --
    # this merge is purely for backward-compatible display, not new logic.
    merged_exclusions = dict(Counter(breakout['exclusion_counts']) + Counter(pullback['exclusion_counts']))
    return {'cutoff': cutoff, 'listed': mid['listed'], 'eligible': mid['eligible'], 'valid': mid['valid'],
            'complete': mid['complete'], 'coverage_pct': mid['coverage_pct'],
            'market_score': mid['market_score'], 'regime': mid['regime'],
            'market_score_pause': mid['tuning']['market_score_pause'],
            'exclusion_counts': merged_exclusions,
            'breakout': breakout, 'pullback': pullback}


def select_candidates(screened, max_n=None):
    """Pool breakout + pullback candidates into ONE ranked list by raw score,
    strategy_type unweighted -- per explicit instruction, not split evenly
    across tracks. NOTE the two score formulas are not on a normalized scale
    (breakout: 50 + return3_pct*5 + (volume_ratio_5d-1)*10; pullback:
    50 + excess20_pp*1.5 - abs(return2_pct)*3), so pooling raw scores means
    whichever formula tends to run hotter will structurally win more slots --
    that's a real distortion, not just a tie-breaking detail, and it can only
    be judged once actual score distributions from real data are in hand. If
    one track ends up dominating every day's picks, the fix is standardizing
    both scores (e.g. percentile rank within each track) before pooling, not
    a track quota -- flagging this rather than silently deciding it."""
    max_n = max_n or POLICY['max_positions']
    pooled = screened['breakout']['candidates'] + screened['pullback']['candidates']
    pooled.sort(key=lambda r: (-r['score'], r['symbol']))
    return pooled[:max_n]


def walk_forward_short(stocks, series, benchmark, cutoff, tuning=None, hotmoney_loader=None):
    """Non-overlapping windows sized to the short hold (3 sessions + 1-session
    purge gap = stride 4), one screen_short() per historical date. Reuses
    alpha_model.label()/estimate() unchanged -- samples are tagged with
    bucket=strategy_type so estimate(samples, cutoff, bucket='breakout')
    calibrates each track independently without any new math.

    `hotmoney_loader(day) -> signals_dict` is optional and called once per
    historical date so calibration samples are scored with the SAME formula
    live picks use -- without it, live scores would include the hot-money
    bonus while the calibration backing their probabilities didn't, which
    would silently invalidate the calibration. Omit it (None) only when no
    hot-money history is available at all; every date it can't cover simply
    screens with no signal for that day, same as a missing checkpoint does
    in live scoring."""
    dates = [b['date'] for b in benchmark if b['date'] <= cutoff]
    hold = SHORT_POLICY['hold_sessions']
    step = hold + 1
    samples, predictions = [], []
    for i in range(22, len(dates) - step, step):
        day = dates[i]
        signal = hotmoney_loader(day) if hotmoney_loader else None
        screened = screen_short(stocks, series, benchmark, day, tuning, signal)
        if not screened['complete'] or screened['market_score'] < screened['market_score_pause']:
            continue
        for track in ('breakout', 'pullback'):
            for c in screened[track]['candidates'][:3]:
                outcome = label(series[c['symbol']], benchmark, dates[i + 1], hold)
                if outcome is None:
                    continue
                prior = estimate(samples, day, bucket=track)
                if prior['probability'] is not None:
                    predictions.append({'p': prior['probability'] / 100, 'y': int(outcome['win'])})
                samples.append({**outcome, 'symbol': c['symbol'], 'signal_day': day,
                                 'bucket': track, 'regime': screened['regime']})
    brier = statistics.mean((p['p'] - p['y']) ** 2 for p in predictions) if predictions else None
    return {'samples': samples, 'walk_forward_n': len(predictions),
            'brier': round(brier, 4) if brier is not None else None,
            'limitations': ['短线(1-3日)回看同样存在当前名单与行业分类的幸存者/回看偏差',
                             '突破/回调两条track各自独立统计校准，不与对方或原T+10版本样本混用',
                             '1-3日窗口下单边成本假设占比更高，历史频率不代表已覆盖成本后的可交易胜率',
                             'MA5/近3日涨幅/放量阈值是初始设定值，尚未用本函数的回看结果验证过合理性']}
