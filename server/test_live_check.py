import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import live_check
from tushare_sync import save


def bars(closes, start_day=1):
    """构造连续日线；日期从 2026-08-xx 递增，足够覆盖 20 根窗口。"""
    out = []
    day = start_day
    month = 8
    for close in closes:
        if day > 28:
            day, month = 1, month + 1
        out.append({'date': '2026-%02d-%02d' % (month, day), 'open': str(close),
                    'high': str(close), 'low': str(close), 'close': str(close),
                    'volume_raw': '1000'})
        day += 1
    return out


def quote(last, **kw):
    base = {'symbol': 'sh600000', 'market': 'cn', 'name': '测试', 'last': str(last),
            'previous_close': '10', 'open': '10', 'high': str(last), 'low': '10',
            'change_pct': '0', 'quote_date': '2026-09-15', 'quote_at': '2026-09-15T15:00:00+08:00',
            'age_seconds': 60, 'is_index': False, 'volume_ratio': '1.5',
            'limit_up': '11', 'limit_down': '9', 'turnover_pct': '1.0'}
    base.update(kw)
    return base


class PriceFactsTests(unittest.TestCase):
    def test_live_price_replaces_todays_bar_instead_of_being_appended(self):
        """series 里可能已经有当天那根（收盘后抓的），也可能没有（盘中）。
        两种情况算出来的均线必须一致，否则今天会被算进去两次。"""
        history = bars([10] * 20)
        without_today = live_check.price_facts(history, quote(11))
        with_today = live_check.price_facts(
            history + [{'date': '2026-09-15', 'open': '11', 'high': '11',
                        'low': '11', 'close': '11', 'volume_raw': '1'}], quote(11))
        self.assertEqual(without_today[0]['ma5'], with_today[0]['ma5'])
        self.assertEqual(without_today[0]['ma20'], with_today[0]['ma20'])

    def test_deviation_and_high20_use_the_live_price(self):
        facts, issues = live_check.price_facts(bars([10] * 20), quote(11))
        self.assertEqual(issues, [])
        self.assertAlmostEqual(facts['ma5'], 10.2)          # (10*4 + 11)/5
        self.assertAlmostEqual(facts['ma20'], 10.05)        # (10*19 + 11)/20
        self.assertAlmostEqual(facts['ma5_deviation_pct'], (11 / 10.2 - 1) * 100, places=3)
        self.assertEqual(facts['high20_close'], 10.0)
        self.assertAlmostEqual(facts['distance_to_high20_pct'], 10.0, places=3)

    def test_short_history_refuses_to_guess(self):
        facts, issues = live_check.price_facts(bars([10] * 5), quote(11))
        self.assertEqual(facts, {})
        self.assertIn('历史日线不足20根', issues[0])

    def test_adjustment_drift_is_flagged(self):
        """实时昨收和缓存上一交易日收盘对不上，通常意味着除权或缓存过期——
        这时候再拿缓存均线和实时价混算就是错的，必须说出来。"""
        facts, issues = live_check.price_facts(bars([10] * 20), quote(11, previous_close='20'))
        self.assertTrue(any('除权' in i for i in issues))
        self.assertGreater(facts['adjustment_drift_pct'], live_check.ADJUST_TOLERANCE_PCT)

    def test_no_drift_warning_within_tolerance(self):
        _, issues = live_check.price_facts(bars([10] * 20), quote(11, previous_close='10.05'))
        self.assertEqual(issues, [])


class LimitTests(unittest.TestCase):
    def test_distance_to_limits(self):
        facts = live_check.limit_facts(quote(10, limit_up='11', limit_down='9'))
        self.assertAlmostEqual(facts['to_limit_up_pct'], 10.0)
        self.assertAlmostEqual(facts['to_limit_down_pct'], 10.0)
        self.assertFalse(facts['at_limit_up'])

    def test_at_limit_up_is_detected(self):
        facts = live_check.limit_facts(quote(11, limit_up='11'))
        self.assertTrue(facts['at_limit_up'])

    def test_index_has_no_limits(self):
        self.assertEqual(live_check.limit_facts(quote(10, is_index=True)), {})


class GateTests(unittest.TestCase):
    def test_breakout_gate_fails_when_price_ran_past_the_band(self):
        """候选是昨天选出来的；今天又涨一段就可能冲出 MA5 偏离 0–6% 的允许区间。
        这正是这一层存在的理由。"""
        facts, _ = live_check.price_facts(bars([10] * 20), quote(12))
        gates = live_check.breakout_gates(facts, quote(12))
        self.assertFalse(gates['MA5偏离'][0])
        self.assertTrue(gates['接近20日新高'][0])

    def test_breakout_gate_passes_in_band(self):
        facts, _ = live_check.price_facts(bars([10] * 20), quote(10.4))
        gates = live_check.breakout_gates(facts, quote(10.4))
        self.assertTrue(gates['MA5偏离'][0])

    def test_volume_ratio_is_reference_only_not_a_pass_fail(self):
        """腾讯量比和策略的日量比口径不同，不能拿来判定门槛。"""
        facts, _ = live_check.price_facts(bars([10] * 20), quote(10.4))
        self.assertIsNone(live_check.breakout_gates(facts, quote(10.4))['量比(参考)'][0])

    def test_pullback_gate_requires_a_green_close(self):
        facts, _ = live_check.price_facts(bars([10] * 20), quote(9.9))
        red = live_check.pullback_gates(facts, quote(9.9, open='10.5'))
        green = live_check.pullback_gates(facts, quote(9.9, open='9.5'))
        self.assertFalse(red['当日收阳'][0])
        self.assertTrue(green['当日收阳'][0])


class EntryBandTests(unittest.TestCase):
    def test_band_uses_the_frozen_plans_policy(self):
        forecast = {'reference_price': 10.0, 'policy': {'entry_gap_min': -0.03, 'entry_gap_max': 0.03}}
        self.assertTrue(live_check.entry_band(forecast, quote(10.2))['in_band'])
        self.assertFalse(live_check.entry_band(forecast, quote(10.4))['in_band'])
        self.assertFalse(live_check.entry_band(forecast, quote(9.5))['in_band'])

    def test_missing_reference_price_yields_nothing(self):
        self.assertEqual(live_check.entry_band({'reference_price': None}, quote(10)), {})


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.history = Path(tempfile.mkdtemp())
        save(self.history / 'alpha_data/series/sh600000.json',
             {'symbol': 'sh600000', 'bars': bars([10] * 20), 'fetched_at': '2026-09-15T16:00:00+08:00'})
        # check() 会顺手给持仓行的移动止损记一次历史最高价（book_state.touch_peak）；不隔离的话
        # 会写到这台机器上真实的 server/data/private/book_state.json。
        env = patch.dict(os.environ, {'PRIVATE_DATA_DIR': str(self.history / 'private')})
        env.start()
        self.addCleanup(env.stop)

    def test_holding_facts_and_roles(self):
        result = live_check.check(
            self.history, [quote(11)],
            holdings=[{'symbol': 'sh600000', 'shares': 100, 'cost_price': 10.0}],
            watchlist=[], agent={})
        row = result['rows'][0]
        self.assertIn('holding', row['roles'])
        self.assertEqual(row['holding']['unrealized_pnl'], 100.0)
        self.assertAlmostEqual(row['holding']['unrealized_pct'], 10.0)

    def test_indices_are_skipped(self):
        result = live_check.check(self.history, [quote(11, symbol='sh000001', is_index=True)],
                                  agent={})
        self.assertEqual(result['rows'], [])

    def test_missing_series_degrades_with_a_reason(self):
        result = live_check.check(self.history, [quote(11, symbol='sz000002')],
                                  watchlist=[{'symbol': 'sz000002', 'intent': 'watch'}], agent={})
        row = result['rows'][0]
        self.assertEqual(row['price_facts'], {})
        self.assertIn('日线缓存', row['issues'][0])

    def test_candidate_metadata_is_attached(self):
        agent = {'candidates': [{'symbol': 'sh600000', 'name': '测试', 'strategy_type': 'breakout',
                                 'score': 88, 'industry': '半导体', 'probability': {'probability': None},
                                 'reasons': ['行业强度前10%']}],
                 'forecasts': [], 'screen': {'cutoff': '2026-09-15', 'market_score': 50}}
        row = live_check.check(self.history, [quote(10.4)], agent=agent)['rows'][0]
        self.assertEqual(row['strategy']['track'], 'breakout')
        self.assertEqual(row['strategy']['score'], 88)
        self.assertIn('MA5偏离', row['live_gates'])
        # 只是候选、还没有冻结计划，就不该凭空造出一个入场带。
        self.assertIsNone(row.get('plan'))


if __name__ == '__main__':
    unittest.main()
