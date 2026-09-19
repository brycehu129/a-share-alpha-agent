import copy
import unittest
from datetime import date, timedelta

from shortterm_model import (HOTMONEY, hotmoney_adjustment, screen_breakout,
                              screen_pullback, screen_short)


def bars(n=70, start='2026-01-05', growth=0.0, volume=100000):
    day = date.fromisoformat(start)
    rows = []
    while len(rows) < n:
        if day.weekday() < 5:
            p = 10 * (1 + growth) ** len(rows)
            rows.append({'date': day.isoformat(), 'open': str(p), 'close': str(p),
                         'high': str(p * 1.02), 'low': str(p * 0.98), 'volume_raw': str(volume)})
        day += timedelta(days=1)
    return rows


STOCK = {'ts_code': '000001.SZ', 'name': '目标股', 'industry': 'IND',
         'list_status': 'L', 'list_date': '20100101'}

# A single-row industries list is enough to make 'IND' the (only, therefore
# top-10%) strong industry -- screen_breakout/screen_pullback take
# industry_rows directly and never recompute it from scratch themselves.
INDUSTRY_ROWS = [{'name': 'IND', 'score': 100, 'median_excess20_pp': 5, 'positive5_pct': 60}]


def breakout_fixture():
    """One stock whose last 3 sessions satisfy every screen_breakout gate:
    fresh (today-only) volume expansion, near-20d-high, controlled MA5
    deviation, modest prior 20-day excess."""
    benchmark = bars()
    stock = copy.deepcopy(benchmark)
    # closes: flat at 10.0 up to index -3, then a 3-session breakout move
    stock[-3]['close'] = stock[-3]['open'] = '10.0'
    stock[-2]['close'] = stock[-2]['open'] = '10.1'
    stock[-1]['close'] = '10.5'
    stock[-1]['open'] = '10.3'
    # volume: flat, spike only on the cutoff day (today, not yesterday)
    stock[-1]['volume_raw'] = '250000'
    cutoff = benchmark[-1]['date']
    return [STOCK], {'sz000001': stock}, benchmark, cutoff


def pullback_fixture():
    """One stock in a confirmed uptrend (positive 20d excess) pausing on
    light volume for 2 sessions, entering on today's green close."""
    benchmark = bars()
    stock = copy.deepcopy(benchmark)
    stock[-3]['close'] = stock[-3]['open'] = '10.3'
    stock[-2]['close'] = stock[-2]['open'] = '10.25'
    stock[-2]['volume_raw'] = '80000'
    stock[-1]['open'] = '10.1'
    stock[-1]['close'] = '10.2'
    stock[-1]['volume_raw'] = '70000'
    cutoff = benchmark[-1]['date']
    return [STOCK], {'sz000001': stock}, benchmark, cutoff


class HotmoneyAdjustmentTests(unittest.TestCase):
    def test_no_signal_is_zero_adjustment(self):
        self.assertEqual(hotmoney_adjustment(None, True), (0.0, []))
        self.assertEqual(hotmoney_adjustment({}, True), (0.0, []))
        self.assertEqual(hotmoney_adjustment({'hm_net_amount': None, 'limit_status': None}, True), (0.0, []))

    def test_net_buying_is_capped(self):
        huge = {'hm_net_amount': HOTMONEY['net_amount_scale'] * 999}
        bump, reasons = hotmoney_adjustment(huge, include_limit_bonus=False)
        self.assertEqual(bump, HOTMONEY['net_amount_cap'])
        self.assertIn('净买入', reasons[0])

    def test_net_selling_is_negative_and_capped(self):
        huge_sell = {'hm_net_amount': -HOTMONEY['net_amount_scale'] * 999}
        bump, reasons = hotmoney_adjustment(huge_sell, include_limit_bonus=False)
        self.assertEqual(bump, -HOTMONEY['net_amount_cap'])
        self.assertIn('净卖出', reasons[0])

    def test_limit_bonus_only_applied_when_requested_and_up(self):
        up = {'hm_net_amount': None, 'limit_status': 'U'}
        self.assertEqual(hotmoney_adjustment(up, include_limit_bonus=False), (0.0, []))
        bump, reasons = hotmoney_adjustment(up, include_limit_bonus=True)
        self.assertEqual(bump, HOTMONEY['limit_up_bonus'])
        self.assertIn('涨停', reasons[0])
        down = {'hm_net_amount': None, 'limit_status': 'D'}
        self.assertEqual(hotmoney_adjustment(down, include_limit_bonus=True), (0.0, []))


class ScreenBreakoutHotmoneyTests(unittest.TestCase):
    def test_candidate_found_without_hotmoney(self):
        stocks, series, benchmark, cutoff = breakout_fixture()
        screened = screen_breakout(stocks, series, benchmark, cutoff, INDUSTRY_ROWS)
        self.assertEqual(len(screened['candidates']), 1, screened['excluded'])
        c = screened['candidates'][0]
        self.assertEqual(c['strategy_type'], 'breakout')
        self.assertIsNone(c['hotmoney'])

    def test_hotmoney_bonus_raises_score_and_is_attached(self):
        stocks, series, benchmark, cutoff = breakout_fixture()
        base = screen_breakout(stocks, series, benchmark, cutoff, INDUSTRY_ROWS)['candidates'][0]
        signal = {'sz000001': {'hm_net_amount': 3e7, 'hm_desks': 2, 'limit_status': 'U', 'limit_times': 1}}
        boosted = screen_breakout(stocks, series, benchmark, cutoff, INDUSTRY_ROWS, signal)['candidates'][0]
        self.assertEqual(boosted['hotmoney'], signal['sz000001'])
        # net_amount bump (capped at 6) + limit_up_bonus (4) = 7
        self.assertAlmostEqual(boosted['score'] - base['score'], 7.0, places=1)
        self.assertTrue(any('游资' in r for r in boosted['reasons']))
        self.assertTrue(any('涨停' in r for r in boosted['reasons']))

    def test_missing_entry_for_symbol_scores_as_no_signal(self):
        stocks, series, benchmark, cutoff = breakout_fixture()
        base = screen_breakout(stocks, series, benchmark, cutoff, INDUSTRY_ROWS)['candidates'][0]
        # hotmoney dict present but has no entry for this symbol -- must be
        # indistinguishable from hotmoney=None, not treated as a penalty.
        unaffected = screen_breakout(stocks, series, benchmark, cutoff, INDUSTRY_ROWS, {})['candidates'][0]
        self.assertEqual(unaffected['score'], base['score'])
        self.assertIsNone(unaffected['hotmoney'])


class ScreenPullbackHotmoneyTests(unittest.TestCase):
    def test_candidate_found_without_hotmoney(self):
        stocks, series, benchmark, cutoff = pullback_fixture()
        screened = screen_pullback(stocks, series, benchmark, cutoff, INDUSTRY_ROWS)
        self.assertEqual(len(screened['candidates']), 1, screened['excluded'])
        self.assertEqual(screened['candidates'][0]['strategy_type'], 'pullback')

    def test_limit_bonus_never_applies_to_pullback_even_when_limit_up(self):
        stocks, series, benchmark, cutoff = pullback_fixture()
        base = screen_pullback(stocks, series, benchmark, cutoff, INDUSTRY_ROWS)['candidates'][0]
        # limit_status='U' present, but pullback must never add limit_up_bonus
        # -- only the net-buying component should move the score.
        signal = {'sz000001': {'hm_net_amount': 2e7, 'hm_desks': 1, 'limit_status': 'U', 'limit_times': 1}}
        boosted = screen_pullback(stocks, series, benchmark, cutoff, INDUSTRY_ROWS, signal)['candidates'][0]
        self.assertAlmostEqual(boosted['score'] - base['score'], 2.0, places=1)
        self.assertFalse(any('涨停' in r for r in boosted['reasons']))
        self.assertTrue(any('游资' in r for r in boosted['reasons']))


def screen_short_fixture():
    """screen_short() recomputes industry strength itself via alpha_model's
    own screen(), which only forms an 'IND' industry row once >=5 stocks in
    it have valid features (and >=90% coverage) -- unlike screen_breakout/
    screen_pullback's unit tests above, which bypass that by taking
    industry_rows directly. So this fixture needs the breakout stock plus 4
    mildly-uptrending fillers in the same industry to make 'IND' a real,
    positive-median strong industry under screen_mid's own rules."""
    stocks, series, benchmark, cutoff = breakout_fixture()
    # 目标股价格恒为 10、填充股缓慢上涨，不放大的话它成交额是 5 只里最低的，会被
    # 流动性闸门（后20%）正确剔除——这个测试要测的是游资信号的传递，与流动性无关。
    # 各日成交量同乘一个常数，量比不变，突破形态照旧成立。
    for b in series['sz000001']:
        b['volume_raw'] = str(float(b['volume_raw']) * 10)
    for i in range(2, 6):
        code = f'sz00000{i}'
        filler = bars(growth=0.0015)
        stocks.append({'ts_code': f'00000{i}.SZ', 'name': f'填充股{i}', 'industry': 'IND',
                        'list_status': 'L', 'list_date': '20100101'})
        series[code] = filler
    return stocks, series, benchmark, cutoff


class ScreenShortHotmoneyThreadingTests(unittest.TestCase):
    def test_hotmoney_threaded_into_both_tracks(self):
        stocks, series, benchmark, cutoff = screen_short_fixture()
        signal = {'sz000001': {'hm_net_amount': 1e7, 'hm_desks': 1, 'limit_status': None, 'limit_times': None}}
        screened = screen_short(stocks, series, benchmark, cutoff, hotmoney=signal)
        breakout_candidates = screened['breakout']['candidates']
        self.assertEqual(len(breakout_candidates), 1, screened['breakout']['excluded'])
        self.assertEqual(breakout_candidates[0]['hotmoney'], signal['sz000001'])


# --- 0.4：流动性闸门 / 原始分排序 / 版本拆分 --------------------------------

from shortterm_model import (ARCHIVE_SIZE, EXECUTION_VERSION, LIQUIDITY_MIN_PCT,
                              SELECTION_VERSION, select_candidates, stock_features)


def universe(n, target_volume_scale):
    """n 只同行业股票：目标股 + (n-1) 只填充股。target_volume_scale 控制目标股相对
    填充股的流动性（<1 就是全池最薄的那只）。"""
    stocks, series, benchmark, cutoff = breakout_fixture()
    for b in series['sz000001']:
        b['volume_raw'] = str(float(b['volume_raw']) * target_volume_scale)
    for i in range(2, n + 1):
        code = 'sz%06d' % i
        stocks.append({'ts_code': '%06d.SZ' % i, 'name': '填充%d' % i, 'industry': 'IND',
                        'list_status': 'L', 'list_date': '20100101'})
        series[code] = bars(growth=0.0015)
    return stocks, series, benchmark, cutoff


class LiquidityGateTests(unittest.TestCase):
    def test_thinnest_stock_is_excluded_from_both_tracks(self):
        """0.3 重写短线 track 时把这道闸门弄丢了：全市场最薄的票也能进候选。"""
        stocks, series, benchmark, cutoff = universe(10, target_volume_scale=0.5)
        feats = stock_features(stocks, series, benchmark, cutoff)
        self.assertLess(feats['sz000001']['liquidity_pct'], LIQUIDITY_MIN_PCT)
        for screen in (screen_breakout, screen_pullback):
            result = screen(stocks, series, benchmark, cutoff, INDUSTRY_ROWS)
            self.assertNotIn('sz000001', [c['symbol'] for c in result['candidates']])
            self.assertEqual(result['excluded'].get('sz000001'), '相对流动性后20%')

    def test_liquid_stock_passes(self):
        stocks, series, benchmark, cutoff = universe(10, target_volume_scale=10)
        result = screen_breakout(stocks, series, benchmark, cutoff, INDUSTRY_ROWS)
        self.assertEqual([c['symbol'] for c in result['candidates']], ['sz000001'])
        self.assertGreaterEqual(result['candidates'][0]['liquidity_pct'], LIQUIDITY_MIN_PCT)

    def test_percentile_is_over_the_whole_valid_universe_not_just_candidates(self):
        """只在候选里排名永远是"相对候选"的位置，起不到排除全市场最薄票的作用。"""
        stocks, series, benchmark, cutoff = universe(10, target_volume_scale=10)
        feats = stock_features(stocks, series, benchmark, cutoff)
        self.assertEqual(len(feats), 10)
        self.assertEqual(max(f['liquidity_pct'] for f in feats.values()), 95.0)  # (9+.5)/10


def cand(symbol, track, raw, liquidity=50.0):
    return {'symbol': symbol, 'strategy_type': track, 'raw_score': raw,
            'score': min(100.0, raw), 'liquidity_pct': liquidity}


def screened_of(breakout, pullback):
    return {'breakout': {'candidates': breakout}, 'pullback': {'candidates': pullback}}


class SelectionOrderTests(unittest.TestCase):
    def test_saturated_scores_are_ordered_by_raw_score_not_by_symbol(self):
        """真实事故：一轮里 5 只票并列 score=100，(-score, symbol) 退化成代码字母序，
        前3名恰好是 sh600520/sh600639/sh688683——原始分其实是 103.89/104.38/103.92，
        而 sz000797 是 108.51。字母序在这里等于抓阄。"""
        breakout = [cand('sh600520', 'breakout', 103.89), cand('sh600639', 'breakout', 104.38),
                    cand('sh688683', 'breakout', 103.92), cand('sz000797', 'breakout', 108.51),
                    cand('sz300787', 'breakout', 106.85)]
        picked = [c['symbol'] for c in select_candidates(screened_of(breakout, []), 3)]
        self.assertEqual(picked, ['sz000797', 'sz300787', 'sh600639'])
        self.assertNotEqual(picked, ['sh600520', 'sh600639', 'sh688683'])  # 字母序

    def test_all_display_scores_tie_at_100_yet_order_is_strict(self):
        breakout = [cand('a', 'breakout', 101), cand('b', 'breakout', 102), cand('c', 'breakout', 103)]
        self.assertEqual({c['score'] for c in breakout}, {100.0})
        picked = [c['symbol'] for c in select_candidates(screened_of(breakout, []), 3)]
        self.assertEqual(picked, ['c', 'b', 'a'])

    def test_pooling_is_not_dominated_by_the_hotter_formula(self):
        """突破公式天然跑得更热（原始分动辄 100+），回调公式常在 60 上下。按原始分混排，
        突破会永远占满名额；按 track 内百分位排名，两条 track 各自的第一名机会均等。"""
        breakout = [cand('b1', 'breakout', 108), cand('b2', 'breakout', 106), cand('b3', 'breakout', 104)]
        pullback = [cand('p1', 'pullback', 66), cand('p2', 'pullback', 64), cand('p3', 'pullback', 62)]
        by_raw = sorted(breakout + pullback, key=lambda r: -r['raw_score'])[:3]
        self.assertEqual({c['strategy_type'] for c in by_raw}, {'breakout'})  # 旧行为：一边倒
        picked = select_candidates(screened_of(breakout, pullback), 3)
        self.assertEqual({c['strategy_type'] for c in picked}, {'breakout', 'pullback'})

    def test_cross_track_tie_is_broken_by_liquidity_not_symbol(self):
        breakout = [cand('sh600001', 'breakout', 105, liquidity=30)]
        pullback = [cand('sz000001', 'pullback', 65, liquidity=80)]
        picked = select_candidates(screened_of(breakout, pullback), 2)
        # 两条 track 各只有一个候选，百分位相同；字母序会把 sh600001 排前面
        self.assertEqual(picked[0]['symbol'], 'sz000001')

    def test_selection_is_idempotent_and_respects_max_n(self):
        breakout = [cand('b%d' % i, 'breakout', 100 + i) for i in range(6)]
        pullback = [cand('p%d' % i, 'pullback', 60 + i) for i in range(6)]
        first = [c['symbol'] for c in select_candidates(screened_of(breakout, pullback), ARCHIVE_SIZE)]
        second = [c['symbol'] for c in select_candidates(screened_of(breakout, pullback), ARCHIVE_SIZE)]
        self.assertEqual(first, second)
        self.assertEqual(len(first), ARCHIVE_SIZE)
        self.assertEqual(len(select_candidates(screened_of(breakout, pullback))), 3)  # 默认 max_positions

    def test_empty_tracks(self):
        self.assertEqual(select_candidates(screened_of([], []), 3), [])


class RealScoreFieldTests(unittest.TestCase):
    def test_candidates_carry_unclipped_raw_score(self):
        stocks, series, benchmark, cutoff = universe(10, target_volume_scale=10)
        c = screen_breakout(stocks, series, benchmark, cutoff, INDUSTRY_ROWS)['candidates'][0]
        self.assertIn('raw_score', c)
        self.assertEqual(c['score'], round(max(0.0, min(100.0, c['raw_score'])), 2))


class VersionSplitTests(unittest.TestCase):
    def test_selection_and_execution_versions_are_independent_labels(self):
        self.assertNotEqual(SELECTION_VERSION, EXECUTION_VERSION)
        self.assertTrue(SELECTION_VERSION.startswith('select-'))
        self.assertTrue(EXECUTION_VERSION.startswith('exec-'))

    def test_legacy_version_alias_tracks_selection_version(self):
        import shortterm_model
        self.assertEqual(shortterm_model.VERSION, SELECTION_VERSION)


if __name__ == '__main__':
    unittest.main()
