"""select-rev-0.1（超跌反弹）筛选闸门的测试，风格对齐 test_shortterm_model.py：直接给
screen_reversal 喂手工构造的 industry_rows，不必真的凑齐 5 只股票去让 alpha_model.screen()
自己算出一个"最弱行业"。"""
import unittest
from datetime import date, timedelta

from strategies.reversal_0_1 import (REVERSAL, STRATEGY_ID, TRADABLE, rank, screen,
                                      screen_reversal, track_of, weak_industries)


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


STOCK = {'ts_code': '000001.SZ', 'name': '目标股', 'industry': 'IND', 'list_status': 'L', 'list_date': '20100101'}
WEAK_INDUSTRY_ROWS = [{'name': 'IND', 'score': 1, 'median_excess20_pp': -5, 'positive5_pct': 30}]
STRONG_INDUSTRY_ROWS = [{'name': 'IND', 'score': 100, 'median_excess20_pp': 5, 'positive5_pct': 60}]


def reversal_fixture():
    """一只股票：所属行业最弱、过去持续跑输（超跌），今天温和放量收阳（反转确认）。
    benchmark/stock 用同一套 bars() 日期生成逻辑（同 start、同 n、同跳过周末），日期天然对齐。"""
    benchmark = bars()
    stock = bars(growth=-0.012)
    stock[-1]['open'] = stock[-2]['close']
    stock[-1]['close'] = str(round(float(stock[-2]['close']) * 1.02, 6))
    stock[-1]['volume_raw'] = str(int(float(stock[-2]['volume_raw']) * 1.5))
    cutoff = benchmark[-1]['date']
    return [STOCK], {'sz000001': stock}, benchmark, cutoff


class WeakIndustriesTests(unittest.TestCase):
    def test_negative_median_and_minority_up_qualifies(self):
        self.assertIn('IND', weak_industries(WEAK_INDUSTRY_ROWS))

    def test_strong_industry_is_excluded(self):
        self.assertNotIn('IND', weak_industries(STRONG_INDUSTRY_ROWS))

    def test_positive_median_excludes_even_if_ranked_last(self):
        """只是分数最低，不代表真的在跌——中位数超额必须为负。"""
        rows = [{'name': 'IND', 'score': 1, 'median_excess20_pp': 0.5, 'positive5_pct': 30}]
        self.assertNotIn('IND', weak_industries(rows))

    def test_majority_still_rising_excludes(self):
        rows = [{'name': 'IND', 'score': 1, 'median_excess20_pp': -1, 'positive5_pct': 60}]
        self.assertNotIn('IND', weak_industries(rows))

    def test_empty_input(self):
        self.assertEqual(weak_industries([]), {})


class ScreenReversalTests(unittest.TestCase):
    def test_oversold_green_close_is_selected(self):
        stocks, series, benchmark, cutoff = reversal_fixture()
        result = screen_reversal(stocks, series, benchmark, cutoff, WEAK_INDUSTRY_ROWS)
        self.assertEqual([c['symbol'] for c in result['candidates']], ['sz000001'])
        c = result['candidates'][0]
        self.assertEqual(c['strategy_type'], 'reversal')
        self.assertLessEqual(c['deviation_pct'], REVERSAL['deviation_max'])
        self.assertLessEqual(c['excess20_pp'], REVERSAL['excess20_max'])
        self.assertTrue(c['is_green'])
        self.assertGreaterEqual(c['volume_ratio_5d'], REVERSAL['volume_ratio_5d_min'])

    def test_strong_industry_stock_is_excluded(self):
        stocks, series, benchmark, cutoff = reversal_fixture()
        result = screen_reversal(stocks, series, benchmark, cutoff, STRONG_INDUSTRY_ROWS)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['excluded']['sz000001'], '行业未进入最弱10%')

    def test_flat_close_is_not_a_reversal_confirmation(self):
        stocks, series, benchmark, cutoff = reversal_fixture()
        series['sz000001'][-1]['close'] = series['sz000001'][-1]['open']
        result = screen_reversal(stocks, series, benchmark, cutoff, WEAK_INDUSTRY_ROWS)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['excluded']['sz000001'], '当日未收阳，无反转确认')

    def test_panic_volume_is_excluded_not_treated_as_a_stronger_signal(self):
        """量比上限存在的意义：恐慌性放量抛售不是反弹信号，别让它反而打高分。"""
        stocks, series, benchmark, cutoff = reversal_fixture()
        series['sz000001'][-1]['volume_raw'] = str(int(float(series['sz000001'][-2]['volume_raw']) * 6))
        result = screen_reversal(stocks, series, benchmark, cutoff, WEAK_INDUSTRY_ROWS)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['excluded']['sz000001'], '今日量比不在企稳区间')

    def test_not_yet_oversold_is_excluded(self):
        stocks, series, benchmark, cutoff = reversal_fixture()
        # 只有轻微偏离，没跌到位。
        for bar in series['sz000001']:
            bar['close'] = bar['open'] = str(round(float(bar['close']), 6))
        flat = bars(growth=-0.0005)
        flat[-1]['open'] = flat[-2]['close']
        flat[-1]['close'] = str(round(float(flat[-2]['close']) * 1.02, 6))
        flat[-1]['volume_raw'] = str(int(float(flat[-2]['volume_raw']) * 1.5))
        result = screen_reversal(stocks, {'sz000001': flat}, benchmark, cutoff, WEAK_INDUSTRY_ROWS)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['excluded']['sz000001'], '偏离MA20不足，尚未超跌')


MID_SHAPE = {'listed': 1, 'eligible': 1, 'valid': 1, 'complete': True, 'coverage_pct': 100.0,
             'market_score': 60, 'regime': '趋势', 'tuning': {'market_score_pause': 40}}


class StrategyInterfaceTests(unittest.TestCase):
    """server/strategies/__init__.py 要求的接口形状：screen(world, tuning, hotmoney)/rank/track_of。"""

    def test_strategy_id_and_tradable_flag(self):
        self.assertEqual(STRATEGY_ID, 'select-rev-0.1')
        self.assertFalse(TRADABLE)

    def test_track_of_returns_strategy_type(self):
        self.assertEqual(track_of({'strategy_type': 'reversal'}), 'reversal')

    def test_screen_and_rank_round_trip_through_world_wrapper(self):
        stocks, series, benchmark, cutoff = reversal_fixture()
        world = {'stocks': stocks, 'series': series, 'benchmark': benchmark, 'cutoff': cutoff,
                 'mid': {**MID_SHAPE, 'industries': WEAK_INDUSTRY_ROWS}}
        screened = screen(world, tuning=None, hotmoney=None)
        self.assertEqual(screened['cutoff'], cutoff)
        ranked = rank(screened, max_n=10)
        self.assertEqual([c['symbol'] for c in ranked], ['sz000001'])
        self.assertIn('rank_pct', ranked[0])

    def test_rank_respects_max_n_and_handles_empty(self):
        self.assertEqual(rank({'reversal': {'candidates': []}}, max_n=10), [])
