import unittest
from datetime import date, timedelta

import book_levels as bl


def bars(n=30, close=10.0, high=10.5, low=9.5):
    """n 根日线，真实波幅恒为 (high-low)，昨收=今收=close，所以 ATR14% = (high-low)/close。"""
    out, d = [], date(2026, 9, 18)
    while len(out) < n:
        if d.weekday() < 5:
            out.append({'date': d.isoformat(), 'close': str(close), 'open': str(close), 'high': str(high), 'low': str(low)})
        d -= timedelta(days=1)
    return list(reversed(out))


class ExitLevelsTests(unittest.TestCase):
    def test_levels_follow_the_stocks_own_volatility_from_the_cost_price(self):
        # ATR14% = 1/10 = 10%，×1.5 = 15%，夹到止损上限 8%；止盈 = 8%×1.5 = 12%（正好是封顶）
        lv = bl.exit_levels(bars(), 20.0)
        self.assertEqual((lv['stop_pct'], lv['target_pct'], lv['nominal']), (0.08, 0.12, False))
        self.assertEqual((lv['stop_price'], lv['target_price']), (18.4, 22.4))
        self.assertAlmostEqual(lv['atr_pct'], 0.1, places=4)

    def test_calm_stock_gets_the_minimum_stop(self):
        lv = bl.exit_levels(bars(high=10.1, low=9.9), 10.0)         # ATR 2% ×1.5 = 3% = 下限
        self.assertEqual((lv['stop_pct'], lv['stop_price']), (0.03, 9.7))
        self.assertEqual(lv['target_price'], 10.45)                 # 3% × 1.5 = 4.5%

    def test_too_little_history_falls_back_to_typical_volatility_and_says_so(self):
        lv = bl.exit_levels(bars(n=10), 10.0)
        self.assertTrue(lv['nominal'])
        self.assertIsNone(lv['atr_pct'])
        self.assertEqual(lv['stop_pct'], 0.0525)                    # 典型 ATR 3.5% × 1.5

    def test_a_flat_series_is_not_treated_as_zero_risk(self):
        """一字板/停牌的日线 ATR 是 0，不能据此把止损设在成本价上。"""
        lv = bl.exit_levels(bars(high=10.0, low=10.0), 10.0)
        self.assertTrue(lv['nominal'])
        self.assertLess(lv['stop_price'], 10.0)

    def test_todays_unfinished_bar_is_not_part_of_the_atr(self):
        series = bars()
        today = {'date': '2026-09-21', 'close': '10', 'open': '10', 'high': '30', 'low': '1'}
        with_today = bl.exit_levels(series + [today], 10.0, before='2026-09-21')
        self.assertEqual(with_today, bl.exit_levels(series, 10.0))


class EnrichTests(unittest.TestCase):
    H = {'symbol': 'sh600519', 'name': '茅台', 'shares': 500, 'cost_price': 10.0}

    def test_uses_the_systems_names_for_stop_target_and_t_base(self):
        e = bl.enrich({**self.H, 'sellable_shares': 350}, bars(high=10.1, low=9.9))
        self.assertEqual((e['stop_price'], e['target_price'], e['t_base_shares']), (9.7, 10.45, 300))   # 350 取整到手
        self.assertNotIn('stop_price', self.H)                                                            # 不改传入的

    def test_star_board_base_rounds_to_200(self):
        e = bl.enrich({**self.H, 'symbol': 'sh688981', 'shares': 700, 'sellable_shares': 500}, bars())
        self.assertEqual(e['t_base_shares'], 400)

    def test_holdings_without_a_trade_record_count_as_fully_sellable(self):
        self.assertEqual(bl.enrich(self.H, bars())['t_base_shares'], 500)

    def test_bought_today_means_no_base(self):
        self.assertEqual(bl.enrich({**self.H, 'sellable_shares': 0}, bars())['t_base_shares'], 0)


if __name__ == '__main__':
    unittest.main()
