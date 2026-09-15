import json
import unittest
from datetime import date
from daily_data import parse_daily, quality_warnings


class DailyTests(unittest.TestCase):
    def raw(self, rows, key='day'):
        return json.dumps({'code': 0, 'data': {'sz000001': {key: rows}}}).encode()

    def test_order_and_prices(self):
        row = ['2026-09-14', '10', '11', '12', '9', '100']
        self.assertEqual(parse_daily(self.raw([row]), 'sz000001', 'none', date(2026, 9, 15))[0]['close'], '11')
        for rows in ([row, row], [['2026-09-16'] + row[1:]], [['2026-09-14', '10', '13', '12', '9', '100']]):
            with self.assertRaises(ValueError):
                parse_daily(self.raw(rows), 'sz000001', 'none', date(2026, 9, 15))

    def test_no_adjustment_fallback(self):
        with self.assertRaises(ValueError):
            parse_daily(self.raw([['2026-09-14', '10', '11', '12', '9', '100']]), 'sz000001', 'qfq', date(2026, 9, 15))

    def test_adjusted_lag_is_visible(self):
        series = [{'symbol': 'sz000001', 'adjustment': 'qfq', 'bars': [{'date': '2026-09-14'}]}]
        self.assertEqual(len(quality_warnings(series, ['2026-09-15'])), 1)
        self.assertEqual(quality_warnings(series, ['2026-09-14']), [])
