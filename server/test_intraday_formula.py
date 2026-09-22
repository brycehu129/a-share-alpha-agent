import unittest

import intraday_formula as inf


def quote(last=10.3, prev=10.0, high=10.4, low=9.6):
    return {'last': str(last), 'previous_close': str(prev), 'high': str(high), 'low': str(low)}


def minute(prices=(9.8, 9.7, 10.3), vwap=10.0):
    return {'vwap': vwap, 'bars': [{'price': price} for price in prices]}


class IntradayFormulaTests(unittest.TestCase):
    def test_support_and_resistance_follow_the_formula(self):
        facts = inf.intraday_facts(quote(), minute())
        self.assertAlmostEqual(facts['support'], 9.6444, places=3)
        self.assertAlmostEqual(facts['resistance'], 10.3111, places=3)

    def test_crosses_are_detected_from_observed_minute_closes(self):
        buy = inf.intraday_facts(quote(last=10.3, prev=10.0, high=10.4, low=9.6), minute((9.7, 9.6, 10.3)))
        self.assertTrue(buy['buy_cross_support'])
        sell = inf.intraday_facts(quote(last=9.8, prev=10.0, high=10.4, low=9.6), minute((10.35, 10.38, 9.8)))
        self.assertTrue(sell['sell_cross_resistance'])

    def test_macd_state_is_reported(self):
        facts = inf.intraday_facts(quote(last=10.5, prev=10.0, high=10.5, low=9.8), minute((9.8, 9.9, 10.0, 10.2, 10.5), 10.1))
        self.assertIn(facts['macd_state'], {'bullish_above_zero', 'bullish_below_zero', 'bearish_above_zero', 'bearish_below_zero'})
        self.assertIn('macd1', facts)
        self.assertIn('macd2', facts)


if __name__ == '__main__':
    unittest.main()