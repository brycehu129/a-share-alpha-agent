import unittest

import book_pnl as bp
import exec_spec


class CostSplitTests(unittest.TestCase):
    def test_buy_and_sell_cost_sum_to_round_trip(self):
        self.assertAlmostEqual(exec_spec.BUY_COST_PCT + exec_spec.SELL_COST_PCT, exec_spec.ROUND_TRIP_COST_PCT, places=6)


class RealizedPnlTests(unittest.TestCase):
    def test_no_sells_is_zero(self):
        r = bp.realized_pnl([{'symbol': 'sh600000', 'side': 'buy', 'price': 10, 'shares': 100}], 'sh600000')
        self.assertEqual(r, {'gross': 0, 'net': 0.0, 'trades': 0})

    def test_sums_only_matching_symbol_sells(self):
        trades = [
            {'symbol': 'sh600000', 'side': 'sell', 'price': 11, 'shares': 100, 'realized_pnl': 100},
            {'symbol': 'sh600000', 'side': 'sell', 'price': 12, 'shares': 100, 'realized_pnl': 200},
            {'symbol': 'sz000001', 'side': 'sell', 'price': 20, 'shares': 100, 'realized_pnl': 999},
            {'symbol': 'sh600000', 'side': 'buy', 'price': 9, 'shares': 100},
        ]
        r = bp.realized_pnl(trades, 'sh600000')
        self.assertEqual(r['gross'], 300)
        self.assertEqual(r['trades'], 2)
        self.assertLess(r['net'], r['gross'])          # 含费一定比不含费少

    def test_works_for_a_fully_closed_position_not_dependent_on_holdings(self):
        """已清仓标的（holdings 里已经没有这一行）依然能从 trades 算出已实现盈亏。"""
        trades = [{'symbol': 'sh600000', 'side': 'sell', 'price': 11, 'shares': 100, 'realized_pnl': 100}]
        r = bp.realized_pnl(trades, 'sh600000')
        self.assertEqual(r['gross'], 100)


class EffectiveCostTests(unittest.TestCase):
    def test_positive_realized_lowers_the_effective_cost(self):
        h = {'shares': 100, 'cost_price': 10.0}
        self.assertLess(bp.effective_cost(h, 200.0), 10.0)
        self.assertAlmostEqual(bp.effective_cost(h, 200.0), 8.0, places=4)

    def test_negative_realized_does_not_raise_the_effective_cost(self):
        """已实现是负数（之前割肉出局过）：不把成本线抬高，直接给回原始成本价，避免误导。"""
        h = {'shares': 100, 'cost_price': 10.0}
        self.assertEqual(bp.effective_cost(h, -200.0), 10.0)

    def test_no_shares_returns_cost_price(self):
        self.assertEqual(bp.effective_cost({'shares': 0, 'cost_price': 10.0}, 500.0), 10.0)


class CombinedTests(unittest.TestCase):
    def test_combined_merges_unrealized_and_realized_net(self):
        h = {'shares': 100, 'cost_price': 10.0}
        q = {'last': '10.5'}
        realized = {'gross': 200.0, 'net': 180.0, 'trades': 2}
        out = bp.combined(h, q, realized)
        self.assertEqual(out['unrealized_pnl'], 50.0)         # (10.5-10)*100
        self.assertEqual(out['combined_pnl'], 230.0)           # 50 + 180
        self.assertAlmostEqual(out['combined_pct'], 23.0, places=4)
        self.assertLess(out['effective_cost'], 10.0)

    def test_effective_cost_is_not_written_back_to_the_holding_dict(self):
        """combined() 只读不改：绝不能把等效成本悄悄塞回传入的 holding。"""
        h = {'shares': 100, 'cost_price': 10.0}
        q = {'last': '10.5'}
        realized = {'gross': 200.0, 'net': 180.0, 'trades': 2}
        bp.combined(h, q, realized)
        self.assertEqual(h, {'shares': 100, 'cost_price': 10.0})

    def test_a_losing_position_with_no_realized_history(self):
        h = {'shares': 100, 'cost_price': 10.0}
        q = {'last': '9.0'}
        realized = {'gross': 0, 'net': 0.0, 'trades': 0}
        out = bp.combined(h, q, realized)
        self.assertEqual(out['unrealized_pnl'], -100.0)
        self.assertEqual(out['combined_pnl'], -100.0)
        self.assertEqual(out['effective_cost'], 10.0)
        self.assertIsNone(out['to_breakeven_pct'])      # 等效成本没变，不重复给一个和账面盈亏一样的数


if __name__ == '__main__':
    unittest.main()
