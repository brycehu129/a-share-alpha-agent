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


class SentinelFieldsTests(unittest.TestCase):
    base = {'symbol': '600519', 'shares': '200', 'cost_price': '1300'}

    def test_new_fields_are_optional_and_default_to_not_declared(self):
        h = book.validate_holding(self.base)
        self.assertIsNone(h['hold_type'])
        self.assertIsNone(h['stop_price'])
        self.assertIsNone(h['target_price'])
        self.assertEqual(h['t_base_shares'], 0)          # 没声明底仓 = 不做T，不替用户假设

    def test_declared_fields_round_trip(self):
        h = book.validate_holding({**self.base, 'hold_type': 'swing', 'stop_price': '1250',
                                   'target_price': '1450', 't_base_shares': '100'})
        self.assertEqual((h['hold_type'], h['stop_price'], h['target_price'], h['t_base_shares']),
                         ('swing', 1250.0, 1450.0, 100))

    def test_rejects_bad_declarations(self):
        for bad in ({'hold_type': 'yolo'}, {'stop_price': '-1'}, {'stop_price': 'abc'},
                    {'stop_price': '1500', 'target_price': '1400'},        # 止损高于目标
                    {'t_base_shares': '150'},                                # 不是整手
                    {'t_base_shares': '300'},                                # 超过持仓
                    {'t_base_shares': '-100'}, {'t_base_shares': '10.5'}):
            with self.assertRaises(book.BookError, msg=bad):
                book.validate_holding({**self.base, **bad})

    def test_garbage_t_base_is_a_book_error_not_a_crash(self):
        """早先这里对 'abc' 抛的是 ValueError，webapp 只捕获 BookError，会变成 500。"""
        for junk in ('abc', 'nan', 'inf'):
            with self.assertRaises(book.BookError):
                book.validate_holding({**self.base, 't_base_shares': junk})

    def test_star_board_base_lot_is_200(self):
        book.validate_holding({'symbol': '688981', 'shares': '400', 'cost_price': '50', 't_base_shares': '200'})
        with self.assertRaises(book.BookError):
            book.validate_holding({'symbol': '688981', 'shares': '400', 'cost_price': '50', 't_base_shares': '100'})

    def test_buy_zone_needs_both_ends_and_a_sensible_order(self):
        w = book.validate_watch({'symbol': '000001', 'buy_low': '10', 'buy_high': '10.5'})
        self.assertEqual((w['buy_low'], w['buy_high']), (10.0, 10.5))
        self.assertIsNone(book.validate_watch({'symbol': '000001'})['buy_low'])
        for bad in ({'buy_low': '10'}, {'buy_high': '10'}, {'buy_low': '11', 'buy_high': '10'},
                    {'buy_low': '10', 'buy_high': '10'}):
            with self.assertRaises(book.BookError, msg=bad):
                book.validate_watch({'symbol': '000001', **bad})


if __name__ == '__main__':
    unittest.main()
