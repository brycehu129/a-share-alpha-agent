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


class EffectiveStopTests(unittest.TestCase):
    """calm 系列日线：ATR14%=2%，stop_pct=0.03（下限），target_pct=0.045，
    breakeven_arm_pct = max(0.02, 0.5×0.045) = 0.0225 → 武装线 = 成本×1.0225。"""
    LEVELS = bl.exit_levels(bars(high=10.1, low=9.9), 10.0)

    def test_no_peak_means_only_the_cost_stop(self):
        s = bl.effective_stop(10.0, 500, self.LEVELS)
        self.assertEqual(s, {'price': 9.7, 'source': 'cost'})

    def test_a_peak_below_breakeven_arm_only_offers_the_trailing_stop(self):
        # 峰值 10.15 < 武装线 10.225：保本不启动；移动止损 = 10.15×(1-2×0.02) = 9.744 → 9.74，紧于成本止损 9.7。
        s = bl.effective_stop(10.0, 500, self.LEVELS, peak_price=10.15)
        self.assertEqual(s, {'price': 9.74, 'source': 'trail'})

    def test_a_peak_at_or_above_breakeven_arm_switches_to_breakeven(self):
        """保本价含真实双边费用，比移动止损（9.888）和成本止损（9.7）都紧，越赚钱止损跟得越紧。"""
        import conditional_exec
        from alpha_model import POLICY
        expected = conditional_exec.breakeven_price(10.0, 500, 5000.0, POLICY)
        s = bl.effective_stop(10.0, 500, self.LEVELS, peak_price=10.30)
        self.assertEqual(s, {'price': expected, 'source': 'breakeven'})
        self.assertGreater(expected, 9.888)      # 真的比只看往返成本近似值更紧

    def test_trailing_stop_only_uses_the_typical_atr_when_the_real_one_is_unavailable(self):
        nominal_levels = bl.exit_levels(bars(n=10), 10.0)     # 日线不足，退回典型 ATR 3.5%
        s = bl.effective_stop(10.0, 500, nominal_levels, peak_price=10.3)   # 低于这份规格的保本武装线 10.394
        self.assertEqual(s['source'], 'trail')
        self.assertAlmostEqual(s['price'], round(10.3 * (1 - 2 * 0.035), 2), places=2)


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

    def test_no_peak_price_defaults_to_the_cost_stop_and_says_so(self):
        e = bl.enrich(self.H, bars(high=10.1, low=9.9))
        self.assertEqual((e['stop_price'], e['stop_source'], e['peak_price']), (9.7, 'cost', None))

    def test_a_peak_price_can_tighten_the_stop_and_is_surfaced_on_the_row(self):
        e = bl.enrich(self.H, bars(high=10.1, low=9.9), peak_price=10.30)
        self.assertEqual(e['stop_source'], 'breakeven')
        self.assertGreater(e['stop_price'], e['cost_price'])          # 越赚钱止损跟得越紧，甚至能高于成本价
        self.assertEqual(e['peak_price'], 10.30)


if __name__ == '__main__':
    unittest.main()
