import json
import os
import tempfile
import unittest

import portfolio_book as book


class SymbolTests(unittest.TestCase):
    def test_accepts_the_shapes_a_human_would_type(self):
        for raw, expected in [('sh600519', 'sh600519'), ('600519', 'sh600519'),
                              ('600519.SH', 'sh600519'), (' 000001 ', 'sz000001'),
                              ('300750', 'sz300750'), ('688981', 'sh688981'),
                              ('000001.sz', 'sz000001')]:
            self.assertEqual(book.normalize_symbol(raw), expected)

    def test_rejects_markets_this_project_does_not_cover(self):
        # 北交所（8/4开头）不在清单、日线和行业映射的覆盖范围里，录进来后面全算不出。
        for raw in ('830799', '430047', '', 'abc', '60051'):
            with self.assertRaises(book.BookError):
                book.normalize_symbol(raw)


class ValidationTests(unittest.TestCase):
    def test_lot_size_is_enforced_per_board(self):
        book.validate_holding({'symbol': '600519', 'shares': '100', 'cost_price': '10'})
        book.validate_holding({'symbol': '688981', 'shares': '200', 'cost_price': '10'})
        for bad in ({'symbol': '600519', 'shares': '150'},      # 主板非100整数倍
                    {'symbol': '688981', 'shares': '100'}):     # 科创板最低200股
            with self.assertRaises(book.BookError):
                book.validate_holding({'cost_price': '10', **bad})

    def test_rejects_bad_numbers_rather_than_coercing(self):
        for bad in ({'shares': '0'}, {'shares': '-100'}, {'shares': '10.5'},
                    {'cost_price': '0'}, {'cost_price': '-1'}, {'cost_price': 'abc'}):
            with self.assertRaises(book.BookError):
                book.validate_holding({'symbol': '600519', 'shares': '100',
                                       'cost_price': '10', **bad})

    def test_rejects_future_open_date(self):
        with self.assertRaises(book.BookError):
            book.validate_holding({'symbol': '600519', 'shares': '100',
                                   'cost_price': '10', 'opened_on': '2099-01-01'})

    def test_watch_intent_is_constrained(self):
        self.assertEqual(book.validate_watch({'symbol': '600519', 'intent': 'sell'})['intent'], 'sell')
        with self.assertRaises(book.BookError):
            book.validate_watch({'symbol': '600519', 'intent': 'yolo'})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_round_trip_and_overwrite_semantics(self):
        book.add_holding({'symbol': '600519', 'shares': '100', 'cost_price': '10'}, self.dir)
        book.add_holding({'symbol': '600519', 'shares': '200', 'cost_price': '12'}, self.dir)
        rows = book.load('holdings', self.dir)
        # 同一只股票只能有一条：两条不同成本价会让盈亏算不清。
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['shares'], 200)

    def test_remove_reports_missing_symbol(self):
        book.add_holding({'symbol': '600519', 'shares': '100', 'cost_price': '10'}, self.dir)
        book.remove('holdings', '600519', self.dir)
        self.assertEqual(book.load('holdings', self.dir), [])
        with self.assertRaises(book.BookError):
            book.remove('holdings', '600519', self.dir)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(book.load('holdings', self.dir), [])

    def test_corrupt_file_is_reported_not_silently_ignored(self):
        path = os.path.join(self.dir, 'holdings.json')
        with open(path, 'w') as f:
            f.write('{"not": "a list"}')
        with self.assertRaises(book.BookError):
            book.load('holdings', self.dir)

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        book.add_holding({'symbol': '600519', 'shares': '100', 'cost_price': '10'}, self.dir)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith('.tmp')], [])
        with open(os.path.join(self.dir, 'holdings.json')) as f:
            self.assertEqual(len(json.load(f)), 1)

    def test_all_symbols_merges_both_books(self):
        book.add_holding({'symbol': '600519', 'shares': '100', 'cost_price': '10'}, self.dir)
        book.add_watch({'symbol': '000001'}, self.dir)
        book.add_watch({'symbol': '600519'}, self.dir)  # 同时持有又自选，只算一次
        self.assertEqual(book.all_symbols(self.dir), ['sh600519', 'sz000001'])


if __name__ == '__main__':
    unittest.main()
