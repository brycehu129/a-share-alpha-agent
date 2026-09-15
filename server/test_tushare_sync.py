import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from tushare_sync import fetch_pages, check_day, select_days, cooldowns, save, single_batch


class SyncTests(unittest.TestCase):
    def test_hourly_cooldown_survives_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / 'runs/1-1.json', {'requests': [{'api': 'stock_basic', 'status': 'rate_limited', 'message': '1次/小时', 'fetched_at': '2026-09-15T13:23:00+00:00'}]})
            self.assertIn('stock_basic', cooldowns(root, datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)))
            self.assertNotIn('stock_basic', cooldowns(root, datetime(2026, 9, 15, 14, 24, tzinfo=timezone.utc)))

    def test_row_ceiling_is_not_complete(self):
        with self.assertRaises(ValueError):
            single_batch('daily', {}, 'ts_code', lambda *args: [{'ts_code': str(i)} for i in range(6000)])

    def test_pagination_detects_ignored_offset(self):
        def call(*args):
            return [{'id': i} for i in range(2000)]
        with self.assertRaises(ValueError):
            fetch_pages('daily', {}, 'id', ('id',), call)

    def test_pair_validation(self):
        row = dict(ts_code='000001.SZ', trade_date='20260914', open=10, high=11, low=9, close=10, pre_close=10, vol=100, amount=100)
        adj = dict(ts_code='000001.SZ', trade_date='20260914', adj_factor=2)
        check_day('20260914', [row], [adj])
        with self.assertRaises(ValueError):
            check_day('20260914', [row], [{**adj, 'ts_code': '600000.SH'}])
        with self.assertRaises(ValueError):
            check_day('20260914', [{**row, 'close': 20}], [adj])

    def test_resume_and_recent_refresh(self):
        self.assertEqual(select_days(['01','02','03','04'], {'03','04'}, 3), ['04','03','02'])
