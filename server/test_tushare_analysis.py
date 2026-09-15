import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tushare_analysis import calculate, build
from tushare_sync import save


class AnalysisTests(unittest.TestCase):
    def test_full_window_and_missing_day_gate(self):
        with tempfile.TemporaryDirectory() as d:
            history = Path(d)
            root = history / 'tushare_data'
            dates = [f'202601{i:02}' for i in range(1, 22)]
            save(root / 'trade_cal.json', {'calendars': {e: [{'cal_date': day, 'is_open': 1} for day in dates] for e in ('SSE', 'SZSE')}})
            save(root / 'stock_basic.json', {'rows': [{'ts_code': '000001.SZ', 'name': '测试', 'list_status': 'L', 'exchange': 'SZSE'}]})
            for day in dates:
                save(root / 'days' / (day + '.json'), {'daily': [dict(ts_code='000001.SZ', trade_date=day, open=10, high=10, low=10, close=10, pre_close=10, vol=1, amount=1)], 'adj_factor': [dict(ts_code='000001.SZ', trade_date=day, adj_factor=1)]})
            now = datetime(2026, 1, 22, tzinfo=timezone.utc)
            r = build(history, now)
            self.assertEqual(r['status'], 'partial')
            self.assertEqual(r['rankings'][0]['return20_pct'], '0.00')
            (root / 'days' / (dates[0] + '.json')).unlink()
            r = build(history, now)
            self.assertEqual(r['status'], 'waiting_data')
            self.assertEqual(r['rankings'], [])

    def test_split_does_not_create_loss(self):
        r = calculate([100] * 20 + [50], [1] * 20 + [2], [100] * 21)
        self.assertEqual(r['return20_pct'], '0.00')
        self.assertEqual(r['ma20_deviation_pct'], '0.00')
        self.assertEqual(r['excess20_pp'], '0.00')

    def test_excess_and_missing_benchmark(self):
        self.assertEqual(calculate([100] * 20 + [120], [1] * 21, [100] * 20 + [110])['excess20_pp'], '10.00')
        self.assertIsNone(calculate([100] * 21, [1] * 21)['excess20_pp'])

    def test_missing_data_is_not_zero_return(self):
        with tempfile.TemporaryDirectory() as d:
            r = build(Path(d), datetime(2026, 9, 15, tzinfo=timezone.utc))
            self.assertEqual(r['status'], 'waiting_data')
            self.assertEqual(r['rankings'], [])
        with self.assertRaises(ValueError):
            calculate([100] * 20, [1] * 20)
