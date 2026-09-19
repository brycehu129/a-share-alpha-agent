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

# 版本号拆成两个，因为它们回答的是两个不同的问题：
#
#   SELECTION_VERSION  选股规则（筛选闸门、打分、排序）。固定研究标签——次日开盘到
#                      第N个收盘——只取决于"选了哪些票"，和之后怎么买卖无关
#                      （label() 的 entry_day 由冻结时间决定，不由入场规则决定），
#                      所以固定标签的样本池按它分组。
#   EXECUTION_VERSION  入场/退出规则。实际（模拟）成交结果按它分组。
#
# 拆开的意义：以后只改执行规则（比如止损位）时，选股层已经攒下的证据不会被清零。
# 合在一个版本号里，每改一次止损就得把选股样本一起作废重来。
#
# 只在对应层的规则真的变了才 bump 对应的号。变了之后旧版本的预测与验收记录原样
# 冻结留档、不迁移，新样本从零开始计数——所以任何一个号都不要随手改。
# select-0.5：选股改在盘前（08:40 用昨收数据 + 晚间公告）冻结；夜间公告命中高风险关键词的直接剔除、不占名额；
#             收盘涨停的候选次日大概率买不到，只留档不成交。选股的打分与门槛本身没变。
SELECTION_VERSION = 'select-0.5'
# exec-0.1：09:30–09:35 观察窗口按报价成交（统一 ±3% 入场带、统一 −3%/+5%），由 opening_observer 执行，
#           只承载 0.3 版留下的 16 条旧计划，已冻结。
# exec-0.2：条件触发入场 + 按 track 分化的止损止盈 + 盘中止损 + 保本止损，由 conditional_exec 在
#           盘中引擎上执行，规格见 exec_spec.py。新计划一律走这个。
# exec-0.3：止损/止盈/追高上限按每只股票自己的 ATR 定（不再是固定 −2.5%），最长持有 5 个交易日，入场时段延到 14:30。
from exec_spec import BASE_EXECUTION_VERSION as EXECUTION_VERSION  # noqa: E402

# 兼容旧代码里的 `VERSION`（沿用它的地方多为"给记录打版本标签"，现在等价于选股版本）。
VERSION = SELECTION_VERSION

TARGET = '下一可验证交易日开盘至第3个交易间隔收盘（1/2日辅助验收），扣0.5%假设往返成本后盈利且跑赢同期沪深300价格指数'

# 每个截止日留档多少条冻结计划，其中前 max_positions 条才允许模拟成交，其余只做研究。
# 研究记录不占资金，却让证据积累速度从每周 3–4 条提到约 50 条；同时能回答一个只交易
# 前3永远答不出的问题：排名到底有没有区分度（第1–3名和第8–10名胜率无差别的话，
# 综合分就是噪声）。
ARCHIVE_SIZE = 10

# 流动性闸门：和 alpha_model.screen() 同口径（同一个 liquidity_proxy、同一条 20 分位线）。
# 0.3 版重写短线 track 时丢了这道闸门，导致全市场最薄的票也能进候选。
LIQUIDITY_MIN_PCT = 20

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


def atr_pct(by_date, dates, n=14):
    """近 n 个交易日的平均真实波幅 / 最新收盘（小数，0.035 = 3.5%）。窗口缺一天返回 None——不用残缺窗口去定止损。
    真实波幅 = max(高-低, |高-昨收|, |低-昨收|)。比值与是否前复权无关。"""
    window = dates[-(n + 1):]
    if len(window) < n + 1 or any(d not in by_date for d in window):
        return None
    trs = []
    for prev, day in zip(window, window[1:]):
        high, low, prev_close = float(by_date[day]['high']), float(by_date[day]['low']), float(by_date[prev]['close'])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    close = float(by_date[window[-1]]['close'])
    return statistics.mean(trs) / close if close > 0 else None


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
        by_date = {b['date']: b for b in bars}
        out[code] = {'symbol': code, 'name': s['name'], 'industry': s['industry'],
                      **f, **today, 'prior_volume_ratio_5d': yesterday['volume_ratio_5d'],
                      'atr14': atr_pct(by_date, today_dates)}
    # 流动性分位在整个"有效窗口"总体上算，而不是只在候选里算——后者永远是相对候选的
    # 排名，起不到"排除全市场最薄的票"的作用。总体比 alpha_model.screen() 的略小
    # （short_window 额外要求今昨两个窗口都完整），分位线本身同为 20。
    if out:
        pct = percentiles({code: r['liquidity_proxy'] for code, r in out.items()})
        for code, r in out.items():
            r['liquidity_pct'] = round(pct[code], 2)
    return out


def screen_breakout(stocks, series, benchmark, cutoff, industry_rows, hotmoney=None):
    strong = strong_industries(industry_rows)
    candidates, excluded = [], {}
    for code, r in stock_features(stocks, series, benchmark, cutoff).items():
        reason = None
        if r['industry'] not in strong:
            reason = '行业未进入强势前10%'
        elif r['liquidity_pct'] < LIQUIDITY_MIN_PCT:
            reason = '相对流动性后20%'
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
        raw = 50 + r['return3_pct'] * 5 + (r['volume_ratio_5d'] - 1) * 10 + bonus
        # score 截断在 0–100 用于展示；raw_score 是截断前的值，用于排序。
        # 早先只存截断后的分数，一轮里就出现过 5 只并列 100（原始分其实是 108.5/106.9/
        # 104.4/103.9/103.9），(-score, symbol) 的 tiebreak 于是退化成按代码首字母排。
        candidates.append({**r, 'strategy_type': 'breakout', 'score': round(clip(raw), 2),
                            'raw_score': round(raw, 4), 'hotmoney': signal,
                            'reasons': ['行业强度前10%', f"近3日涨幅≥{BREAKOUT['return3_min']}%",
                                        '今日首次放量', '接近20日新高', 'MA5偏离受控', *hm_reasons]})
    candidates.sort(key=lambda r: (-r['raw_score'], r['symbol']))
    return {'candidates': candidates, 'excluded': excluded, 'exclusion_counts': dict(Counter(excluded.values()))}


def screen_pullback(stocks, series, benchmark, cutoff, industry_rows, hotmoney=None):
    strong = strong_industries(industry_rows)
    candidates, excluded = [], {}
    for code, r in stock_features(stocks, series, benchmark, cutoff).items():
        reason = None
        if r['industry'] not in strong:
            reason = '行业未进入强势前10%'
        elif r['liquidity_pct'] < LIQUIDITY_MIN_PCT:
            reason = '相对流动性后20%'
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
        raw = 50 + r['excess20_pp'] * 1.5 - abs(r['return2_pct']) * 3 + bonus
        candidates.append({**r, 'strategy_type': 'pullback', 'score': round(clip(raw), 2),
                            'raw_score': round(raw, 4), 'hotmoney': signal,
                            'reasons': ['行业强度前10%', '20日超额为正', '近2日缩量回调',
                                        '未跌破MA20区间', '当日收阳反转确认', *hm_reasons]})
    candidates.sort(key=lambda r: (-r['raw_score'], r['symbol']))
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
    """把突破和回调两条 track 的候选合并成一个排好序的列表。

    两条打分公式的量纲不一样（突破：50 + 近3日涨幅*5 + (量比-1)*10；回调：
    50 + 20日超额*1.5 - |近2日|*3），直接拿原始分混排，跑得"更热"的那条公式会
    系统性地抢走名额——不是平局怎么破的小问题，是结构性偏差。本轮之前的真实数据里
    3 条冻结计划全是突破，正是这个预判。

    做法：各 track 内部先按**截断前的原始分**转成百分位（tie-aware 的中点秩，和
    alpha_model.percentiles 同一口径），再合并排序。这不是给每条 track 配额——
    一条 track 明显更强的那天，它照样能占多数名额，只是"强"必须体现在自己
    track 内的相对位置上，而不是公式恰好给了更大的数。

    为什么必须用 raw_score 而不是展示用的 score：score 被 clip 在 0–100，一轮里
    曾有 5 只票并列 100（原始分 108.5/106.9/104.4/103.9/103.9）。百分位排名会
    原样保留并列，(-score, symbol) 的 tiebreak 就退化成按代码首字母排，选股等于
    在满分票里抓阄。原始分不截断，区分度一直都在。

    跨 track 平局（比如两条 track 各自的第一名百分位相同）用流动性分位破：同等
    条件下选更容易成交的那只。绝不用代码字母序——那是"随机但看起来确定"，最糟。

    副作用：给候选加上 rank_pct 字段（幂等，重复调用结果一致）。
    """
    max_n = max_n or POLICY['max_positions']
    pooled = []
    for track in ('breakout', 'pullback'):
        rows = screened[track]['candidates']
        if not rows:
            continue
        pct = percentiles({r['symbol']: r['raw_score'] for r in rows})
        for r in rows:
            r['rank_pct'] = round(pct[r['symbol']], 2)
        pooled.extend(rows)
    pooled.sort(key=lambda r: (-r['rank_pct'], -r['liquidity_pct'], r['symbol']))
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
        # 和实盘留档同一套选取：两条 track 合并后的前 ARCHIVE_SIZE 名。早先这里是
        # "每条 track 各取前3"，而实盘现在留档合并后的前10，二者分布不同——校准样本
        # 必须来自和实盘被打概率的那些票相同的抽样方式，否则概率就是对着另一批票算的。
        for rank, c in enumerate(select_candidates(screened, ARCHIVE_SIZE), start=1):
            track = c['strategy_type']
            outcome = label(series[c['symbol']], benchmark, dates[i + 1], hold)
            if outcome is None:
                continue
            prior = estimate(samples, day, bucket=track)
            if prior['probability'] is not None:
                predictions.append({'p': prior['probability'] / 100, 'y': int(outcome['win'])})
            samples.append({**outcome, 'symbol': c['symbol'], 'signal_day': day,
                             'bucket': track, 'regime': screened['regime'], 'rank': rank})
    brier = statistics.mean((p['p'] - p['y']) ** 2 for p in predictions) if predictions else None
    return {'samples': samples, 'walk_forward_n': len(predictions),
            'brier': round(brier, 4) if brier is not None else None,
            'limitations': ['短线(1-3日)回看同样存在当前名单与行业分类的幸存者/回看偏差',
                             '突破/回调两条track各自独立统计校准，不与对方或原T+10版本样本混用',
                             '1-3日窗口下单边成本假设占比更高，历史频率不代表已覆盖成本后的可交易胜率',
                             'MA5/近3日涨幅/放量阈值是初始设定值，尚未用本函数的回看结果验证过合理性',
                             '回看样本取两条track合并后的前%d名（与实盘留档同口径）；每个样本带rank字段，'
                             '可用来检验排名有没有区分度' % ARCHIVE_SIZE]}
