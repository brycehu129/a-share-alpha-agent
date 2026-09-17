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


if __name__ == '__main__':
    unittest.main()
