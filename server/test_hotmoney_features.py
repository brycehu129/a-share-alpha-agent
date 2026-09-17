import tempfile
import unittest
from pathlib import Path

from tushare_sync import save
from hotmoney_features import load, to_symbol


class HotmoneyFeatureTests(unittest.TestCase):
    def test_to_symbol_converts_sh_sz_and_skips_others(self):
        self.assertEqual(to_symbol('000001.SZ'), 'sz000001')
        self.assertEqual(to_symbol('600000.SH'), 'sh600000')
        self.assertIsNone(to_symbol('430047.BJ'))
        self.assertIsNone(to_symbol('not-a-code'))

    def test_missing_checkpoints_report_unavailable_not_empty(self):
        with tempfile.TemporaryDirectory() as d:
            signals, availability = load(Path(d), '20260917')
        self.assertEqual(signals, {})
        self.assertEqual(availability, {'hm_detail': False, 'limit_list_d': False})

    def test_aggregates_multiple_hotmoney_rows_for_same_stock(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            save(root / 'tushare_data' / 'hm_detail' / '20260917.json', {'rows': [
                {'ts_code': '000001.SZ', 'net_amount': 100.0},
                {'ts_code': '000001.SZ', 'net_amount': -30.0},
                {'ts_code': '430047.BJ', 'net_amount': 999.0},
            ]})
            signals, availability = load(root, '20260917')
        self.assertEqual(availability['hm_detail'], True)
        self.assertEqual(availability['limit_list_d'], False)
        self.assertEqual(signals['sz000001']['hm_net_amount'], 70.0)
        self.assertEqual(signals['sz000001']['hm_desks'], 2)
        self.assertNotIn('bj430047', signals)

    def test_merges_limit_status_onto_same_symbol(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            save(root / 'tushare_data' / 'hm_detail' / '20260917.json',
                 {'rows': [{'ts_code': '000001.SZ', 'net_amount': 50.0}]})
            save(root / 'tushare_data' / 'limit_list_d' / '20260917.json',
                 {'rows': [{'ts_code': '000001.SZ', 'limit': 'U', 'limit_times': 2}]})
            signals, availability = load(root, '20260917')
        self.assertTrue(availability['hm_detail'] and availability['limit_list_d'])
        self.assertEqual(signals['sz000001'], {'hm_net_amount': 50.0, 'hm_desks': 1,
                                                'limit_status': 'U', 'limit_times': 2})

    def test_null_limit_times_preserved_as_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            save(root / 'tushare_data' / 'limit_list_d' / '20260917.json',
                 {'rows': [{'ts_code': '000572.SZ', 'limit': 'Z', 'limit_times': None}]})
            signals, _ = load(root, '20260917')
        self.assertIsNone(signals['sz000572']['limit_times'])
        self.assertEqual(signals['sz000572']['limit_status'], 'Z')


if __name__ == '__main__':
    unittest.main()
