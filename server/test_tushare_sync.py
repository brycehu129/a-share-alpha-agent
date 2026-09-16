import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from tushare_sync import fetch_pages, check_day, select_days, cooldowns, save, single_batch
from tushare_sync import partition_daily


class SyncTests(unittest.TestCase):
    def test_exact_zero_activity_placeholder_is_quarantined(self):
        row = dict(ts_code='000016.SZ',trade_date='20260915',open=0,high=0,low=0,close=2.46,pre_close=2.46,vol=0,amount=0)
        valid, excluded = partition_daily('20260915',[row])
        self.assertEqual(valid, [])
        self.assertEqual(excluded[0]['reason'], 'zero_activity_placeholder_unverified')
        factor = dict(ts_code='000016.SZ',trade_date='20260915',adj_factor=1)
        for change in ({'vol':1},{'close':2.5},{'open':1},{'amount':-1}):
            valid, excluded = partition_daily('20260915',[{**row,**change}])
            self.assertEqual(excluded, [])
            with self.assertRaises(ValueError):
                check_day('20260915',valid,[factor])
        with self.assertRaises(ValueError):
            partition_daily('20260915',[{**row,'close':'NaN'}])
        with self.assertRaises(ValueError):
            partition_daily('20260915',[row,row])

    def test_source_cooldowns_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / 'runs/1-1.json', {'requests':[{'api':'daily','status':'rate_limited','message':'1次/天','fetched_at':'2026-09-15T13:23:00+00:00','endpoint':'https://api.tushare.pro'}]})
            self.assertNotIn('daily', cooldowns(root, datetime(2026,9,15,14,tzinfo=timezone.utc), 'https://t.xiaodefa.top/'))

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
