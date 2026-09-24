import unittest
from datetime import date, timedelta

import book_triage as bt


def bars(closes):
    """closes：从最早到最近的收盘价列表；日期只是占位，triage 不看日期只看顺序。"""
    out, d = [], date(2026, 8, 1)
    for c in closes:
        out.append({'date': d.isoformat(), 'close': str(c)})
        d += timedelta(days=1)
    return out


class HealthyTests(unittest.TestCase):
    def test_price_above_cost_is_healthy_regardless_of_everything_else(self):
        r = bt.triage(10.0, 10.5, bars([9] * 30), {'ma20': 20.0}, 0.05, 0.03)
        self.assertEqual(r['bucket'], 'healthy')


class MissingDataTests(unittest.TestCase):
    def test_no_ma20_means_no_verdict_not_a_guess(self):
        r = bt.triage(10.0, 9.0, bars([9] * 10), {}, 0.05, 0.03)
        self.assertIsNone(r['bucket'])
        self.assertIn('无法分诊', r['reasons'][0])


class BrokenTests(unittest.TestCase):
    def test_below_ma20_with_ma20_declining_is_broken(self):
        # 25 根收盘价，最近5根之前的窗口 MA20 = 10.0；facts 里今天的 MA20 更低 = 下行。
        closes = [10.0] * 20 + [10.0, 10.0, 10.0, 10.0, 10.0]
        r = bt.triage(10.0, 8.5, bars(closes), {'ma20': 9.0, 'ma60': None}, 0.05, 0.03)
        self.assertEqual(r['bucket'], 'broken')
        self.assertIn('下行', r['reasons'][0])

    def test_below_both_moving_averages_is_broken(self):
        closes = [9.0] * 25          # MA20 走平（5天前和今天一样），不会被"下行"条件先截走
        r = bt.triage(10.0, 8.0, bars(closes), {'ma20': 9.0, 'ma60': 8.5}, 0.05, 0.03)
        self.assertEqual(r['bucket'], 'broken')
        self.assertIn('MA60', r['reasons'][0])

    def test_below_the_20_day_low_is_broken(self):
        closes = [9.0] * 25          # 同上，走平，且不设 MA60 避免命中"跌破两条均线"
        r = bt.triage(10.0, 7.0, bars(closes), {'ma20': 9.0, 'low20_close': 8.0}, 0.05, 0.03)
        self.assertEqual(r['bucket'], 'broken')
        self.assertIn('20 日低点', r['reasons'][0])

    def test_falls_back_to_broken_when_nothing_else_matches(self):
        """在 MA20 下方，但既不满足回调（MA20 走平不算上行）也不满足区间震荡（波动率不够）：
        保守按最坏的档处理，不要凭感觉判成"看起来还行"。"""
        closes = [10.0] * 25          # 完全走平，ma20_prior == ma20（不算"上行"）
        r = bt.triage(10.0, 9.5, bars(closes), {'ma20': 10.0, 'ma60': 9.0}, 0.05, 0.01)   # ATR 太小，进不了 range
        self.assertEqual(r['bucket'], 'broken')


class PullbackTests(unittest.TestCase):
    def test_still_above_ma20_is_a_pullback_even_if_below_cost(self):
        r = bt.triage(10.0, 9.5, bars([10.0] * 25), {'ma20': 9.0}, 0.05, 0.03)
        self.assertEqual(r['bucket'], 'pullback')

    def test_below_ma20_but_rising_and_above_ma60_and_shallow_drawdown_is_a_pullback(self):
        # MA20 上行：5 天前的窗口均值 9.0，今天 facts 给 9.5（更高）。回撤 (10-9.6)/10=4% < stop_pct 5%。
        closes = [9.0] * 20 + [9.0] * 5
        r = bt.triage(10.0, 9.6, bars(closes), {'ma20': 9.5, 'ma60': 9.0}, 0.05, 0.02)
        self.assertEqual(r['bucket'], 'pullback')

    def test_deep_drawdown_even_with_a_rising_ma20_is_not_a_pullback(self):
        """MA20 上行、仍在 MA60 之上，但回撤已经超过一个止损幅度——不能算"正常回调"。"""
        closes = [9.0] * 25
        r = bt.triage(10.0, 9.0, bars(closes), {'ma20': 9.5, 'ma60': 8.5}, 0.05, 0.01)
        self.assertNotEqual(r['bucket'], 'pullback')      # 落到 broken（波动率太小进不了 range）


class RangeTests(unittest.TestCase):
    def test_wide_flat_range_with_enough_volatility_is_a_range(self):
        # 周期为4的箱体 [8,8,11,11] 反复出现：任何 20 根（4的倍数）窗口均值恒为 9.5，20 落在
        # 4 的倍数上时相位不影响均值——今天和 5 天前的 MA20 因此严格相等（真正的"走平"）。
        # 区间宽度 (11-8)/8=37.5% ≥ 12%；现价跌破 MA20 但仍在 MA60 之上，回撤 15% 超过一个止损
        # 幅度（5%），所以不会先被判成"回调"。
        closes = [8.0, 8.0, 11.0, 11.0] * 7
        r = bt.triage(10.0, 8.5, bars(closes), {'ma20': 9.5, 'ma60': 8.0}, 0.05, 0.04)
        self.assertEqual(r['bucket'], 'range')

    def test_narrow_range_does_not_qualify(self):
        closes = [10.0] * 25          # 区间宽度接近 0，达不到 12%
        r = bt.triage(10.0, 9.9, bars(closes), {'ma20': 10.0, 'ma60': 9.5}, 0.05, 0.04)
        self.assertNotEqual(r['bucket'], 'range')

    def test_low_volatility_does_not_qualify_even_if_the_range_looks_wide_by_coincidence(self):
        closes = [9.0] * 20 + [11.5] * 5
        r = bt.triage(10.0, 9.5, bars(closes), {'ma20': 10.0, 'ma60': 10.0}, 0.05, 0.01)   # ATR 太小
        self.assertNotEqual(r['bucket'], 'range')


class SafetyBoundaryTests(unittest.TestCase):
    def test_triage_never_returns_an_exit_style_bucket(self):
        """分诊只在 healthy/broken/pullback/range/None 五个值里选，不能凭空造出"exit"之类的档——
        那是 book_verdict 的止损/止盈判断该管的事，分诊管不着，也不该有能力去管。"""
        cases = [
            (10.0, 10.5, [9] * 30, {'ma20': 9.0}),
            (10.0, 9.0, [9] * 10, {}),
            (10.0, 8.0, [10.0] * 25, {'ma20': 9.0, 'ma60': 8.5}),
            (10.0, 9.5, [10.0] * 25, {'ma20': 9.0}),
        ]
        for cost, last, closes, facts in cases:
            r = bt.triage(cost, last, bars(closes), facts, 0.05, 0.03)
            self.assertIn(r['bucket'], (None, 'healthy', 'broken', 'pullback', 'range'))


if __name__ == '__main__':
    unittest.main()
