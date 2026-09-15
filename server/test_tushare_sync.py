import unittest
from tushare_sync import fetch_pages, check_day, select_days


class SyncTests(unittest.TestCase):
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
