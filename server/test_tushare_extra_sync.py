import unittest
from tushare_extra_sync import validate_hm_detail, validate_limit_list, validate_cyq_chips, capped_batch


class ExtraSyncTests(unittest.TestCase):
    def test_hm_detail_accepts_consistent_rows(self):
        rows = [dict(trade_date='20260915', ts_code='000001.SZ', ts_name='平安银行',
                     buy_amount=120.5, sell_amount=30.2, net_amount=90.3,
                     hm_name='国泰君安上海江苏路', hm_orgs='')]
        self.assertEqual(validate_hm_detail('20260915', rows), rows)

    def test_hm_detail_rejects_mismatched_net_amount(self):
        rows = [dict(trade_date='20260915', ts_code='000001.SZ', ts_name='平安银行',
                     buy_amount=100, sell_amount=10, net_amount=50,
                     hm_name='游资甲', hm_orgs='')]
        with self.assertRaises(ValueError):
            validate_hm_detail('20260915', rows)

    def test_hm_detail_rejects_wrong_trade_date(self):
        rows = [dict(trade_date='20260914', ts_code='000001.SZ', ts_name='x',
                     buy_amount=1, sell_amount=0, net_amount=1, hm_name='y', hm_orgs='')]
        with self.assertRaises(ValueError):
            validate_hm_detail('20260915', rows)

    def test_hm_detail_rejects_duplicate_stock_hotmoney_pair(self):
        row = dict(trade_date='20260915', ts_code='000001.SZ', ts_name='x',
                   buy_amount=1, sell_amount=0, net_amount=1, hm_name='y', hm_orgs='')
        with self.assertRaises(ValueError):
            validate_hm_detail('20260915', [row, dict(row)])

    def test_hm_detail_rejects_negative_amount(self):
        rows = [dict(trade_date='20260915', ts_code='000001.SZ', ts_name='x',
                     buy_amount=-1, sell_amount=0, net_amount=-1, hm_name='y', hm_orgs='')]
        with self.assertRaises(ValueError):
            validate_hm_detail('20260915', rows)

    def test_limit_list_accepts_valid_row(self):
        rows = [dict(trade_date='20260915', ts_code='000001.SZ', name='平安银行', close=11.0,
                     pct_chg=10.0, limit_amount=1.2e8, fd_amount=5.3e7, open_times=0, limit_times=1, limit='U')]
        self.assertEqual(validate_limit_list('20260915', rows), rows)

    def test_limit_list_rejects_unknown_limit_type(self):
        rows = [dict(trade_date='20260915', ts_code='000001.SZ', name='x', close=1, pct_chg=1,
                     limit_amount=1, fd_amount=1, open_times=0, limit_times=1, limit='X')]
        with self.assertRaises(ValueError):
            validate_limit_list('20260915', rows)

    def test_limit_list_rejects_nonpositive_close(self):
        rows = [dict(trade_date='20260915', ts_code='000001.SZ', name='x', close=-1, pct_chg=1,
                     limit_amount=1, fd_amount=1, open_times=0, limit_times=1, limit='U')]
        with self.assertRaises(ValueError):
            validate_limit_list('20260915', rows)

    def test_limit_list_rejects_duplicate_symbol(self):
        row = dict(trade_date='20260915', ts_code='000001.SZ', name='x', close=1, pct_chg=1,
                   limit_amount=1, fd_amount=1, open_times=0, limit_times=1, limit='U')
        with self.assertRaises(ValueError):
            validate_limit_list('20260915', [row, dict(row)])

    def test_limit_list_accepts_zhaban_row_with_all_nulls(self):
        rows = [dict(trade_date='20260917', ts_code='000572.SZ', name='海马汽车', close=4.36,
                     pct_chg=7.92, limit_amount=None, fd_amount=None,
                     open_times=2, limit_times=None, limit='Z')]
        self.assertEqual(validate_limit_list('20260917', rows), rows)

    def test_limit_list_rejects_zero_limit_times_when_present(self):
        rows = [dict(trade_date='20260917', ts_code='000001.SZ', name='x', close=1,
                     pct_chg=1, limit_amount=None, fd_amount=None,
                     open_times=0, limit_times=0, limit='U')]
        with self.assertRaises(ValueError):
            validate_limit_list('20260917', rows)

    def test_limit_list_accepts_null_limit_and_fd_amount(self):
        rows = [dict(trade_date='20260917', ts_code='000504.SZ', name='南华生物', close=9.1,
                     pct_chg=10.04, limit_amount=None, fd_amount=34195070.0,
                     open_times=1, limit_times=1.0, limit='U')]
        self.assertEqual(validate_limit_list('20260917', rows), rows)

    def test_limit_list_rejects_negative_fd_amount_when_present(self):
        rows = [dict(trade_date='20260917', ts_code='000504.SZ', name='x', close=9.1,
                     pct_chg=10.04, limit_amount=None, fd_amount=-1,
                     open_times=1, limit_times=1, limit='U')]
        with self.assertRaises(ValueError):
            validate_limit_list('20260917', rows)

    def test_limit_list_rejects_invalid_limit_times(self):
        rows = [dict(trade_date='20260915', ts_code='000001.SZ', name='x', close=1, pct_chg=1,
                     limit_amount=1, fd_amount=1, open_times=0, limit_times=0, limit='U')]
        with self.assertRaises(ValueError):
            validate_limit_list('20260915', rows)

    def test_cyq_chips_accepts_rows_summing_to_100(self):
        rows = [dict(ts_code='000001.SZ', trade_date='20260915', price=10.0, percent=60.0),
                dict(ts_code='000001.SZ', trade_date='20260915', price=10.5, percent=40.0)]
        self.assertEqual(validate_cyq_chips('20260915', '000001.SZ', rows), rows)

    def test_cyq_chips_rejects_percentages_not_summing_to_100(self):
        rows = [dict(ts_code='000001.SZ', trade_date='20260915', price=10.0, percent=60.0),
                dict(ts_code='000001.SZ', trade_date='20260915', price=10.5, percent=20.0)]
        with self.assertRaises(ValueError):
            validate_cyq_chips('20260915', '000001.SZ', rows)

    def test_cyq_chips_rejects_wrong_symbol(self):
        rows = [dict(ts_code='600000.SH', trade_date='20260915', price=10.0, percent=100.0)]
        with self.assertRaises(ValueError):
            validate_cyq_chips('20260915', '000001.SZ', rows)

    def test_cyq_chips_rejects_nonpositive_price(self):
        rows = [dict(ts_code='000001.SZ', trade_date='20260915', price=0, percent=100.0)]
        with self.assertRaises(ValueError):
            validate_cyq_chips('20260915', '000001.SZ', rows)

    def test_capped_batch_flags_row_ceiling(self):
        with self.assertRaises(ValueError):
            capped_batch('limit_list_d', {}, 'ts_code', lambda *a: [{'ts_code': str(i)} for i in range(2500)], 2500)

    def test_capped_batch_accepts_under_cap(self):
        rows = capped_batch('limit_list_d', {}, 'ts_code', lambda *a: [{'ts_code': '1'}], 2500)
        self.assertEqual(rows, [{'ts_code': '1'}])


if __name__ == '__main__':
    unittest.main()
