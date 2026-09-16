import unittest
from tushare_bridge import convert


class BridgeTests(unittest.TestCase):
    def test_split_adjustment_raw_prices_and_missing_dates(self):
        rows=[dict(trade_date='20260911',open=100,high=110,low=90,close=100,vol=100,adj_factor=1),
              dict(trade_date='20260915',open=50,high=55,low=45,close=50,vol=200,adj_factor=2)]
        adjusted, raw = convert(rows)
        self.assertEqual([float(b['close']) for b in adjusted],[50,50])
        self.assertEqual([float(b['close']) for b in raw],[100,50])
        self.assertEqual([b['volume_raw'] for b in adjusted],['100','200'])
        self.assertEqual([b['date'] for b in adjusted],['2026-09-11','2026-09-15'])
        with self.assertRaises(ValueError):
            convert(rows + [rows[0]])
        with self.assertRaises(ValueError):
            convert([{**rows[0], 'adj_factor':0}])
