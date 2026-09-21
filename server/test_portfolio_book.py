import json
import os
import tempfile
import unittest
from datetime import datetime

import portfolio_book as book
from collect_quotes import CST

# 固定"现在"，T+1 的测试才不依赖跑测试的那一天。
NOW = datetime(2026, 9, 21, 10, 30, tzinfo=CST)
LATER = datetime(2026, 9, 22, 10, 30, tzinfo=CST)


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
    buy = {'symbol': '600519', 'price': '10', 'shares': '100'}

    def test_lot_size_is_enforced_per_board(self):
        book.validate_buy(self.buy, NOW)
        book.validate_buy({'symbol': '688981', 'price': '10', 'shares': '200'}, NOW)
        for bad in ({'symbol': '600519', 'shares': '150'},      # 主板非100整数倍
                    {'symbol': '688981', 'shares': '100'}):     # 科创板最低200股
            with self.assertRaises(book.BookError):
                book.validate_buy({**self.buy, **bad}, NOW)

    def test_rejects_bad_numbers_rather_than_coercing(self):
        for bad in ({'shares': '0'}, {'shares': '-100'}, {'shares': '10.5'}, {'shares': ''},
                    {'price': '0'}, {'price': '-1'}, {'price': 'abc'}, {'price': 'nan'}, {'price': 'inf'},
                    {'shares': '1e12'}):
            with self.assertRaises(book.BookError, msg=bad):
                book.validate_buy({**self.buy, **bad}, NOW)

    def test_trade_date_defaults_to_today_and_cannot_be_in_the_future(self):
        self.assertEqual(book.validate_buy(self.buy, NOW)['date'], '2026-09-21')
        self.assertEqual(book.validate_buy({**self.buy, 'date': '2026-09-01'}, NOW)['date'], '2026-09-01')
        for bad in ('2099-01-01', '2026-9-1x', 'yesterday'):
            with self.assertRaises(book.BookError, msg=bad):
                book.validate_buy({**self.buy, 'date': bad}, NOW)

    def test_watch_entry_carries_no_intent_or_buy_zone(self):
        w = book.validate_watch({'symbol': '600519', 'name': '贵州茅台', 'note': ' 盯一下 '}, NOW)
        self.assertEqual((w['symbol'], w['name'], w['note'], w['added_on']), ('sh600519', '贵州茅台', '盯一下', '2026-09-21'))
        self.assertNotIn('intent', w)
        self.assertNotIn('buy_low', w)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def watch(self, symbol='600519', name='贵州茅台'):
        return book.add_watch({'symbol': symbol, 'name': name}, self.dir, NOW)

    def buy(self, shares='100', price='10', symbol='600519', now=NOW, **extra):
        return book.buy({'symbol': symbol, 'price': price, 'shares': shares, **extra}, self.dir, now)

    def sell(self, shares='100', price='11', symbol='600519', now=LATER, **extra):
        return book.sell({'symbol': symbol, 'price': price, 'shares': shares, **extra}, self.dir, now)

    def holding(self, symbol='sh600519'):
        return next(h for h in book.load('holdings', self.dir) if h['symbol'] == symbol)

    def test_watch_is_idempotent_and_keeps_the_original_added_date(self):
        self.watch()
        book.add_watch({'symbol': '600519', 'note': '后来补的备注'}, self.dir, LATER)
        rows = book.load('watchlist', self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['added_on'], rows[0]['name'], rows[0]['note']), ('2026-09-21', '贵州茅台', '后来补的备注'))

    def test_buying_requires_the_stock_to_be_on_the_watchlist(self):
        with self.assertRaises(book.BookError) as ctx:
            self.buy()
        self.assertIn('自选', str(ctx.exception))
        self.assertEqual(book.load('holdings', self.dir), [])
        self.assertEqual(book.load('trades', self.dir), [])          # 被拒绝的买入不留痕

    def test_first_buy_creates_the_holding_with_the_buy_price_as_cost(self):
        self.watch()
        self.buy(shares='200', price='12.5')
        h = self.holding()
        self.assertEqual((h['shares'], h['cost_price'], h['opened_on'], h['name']), (200, 12.5, '2026-09-21', '贵州茅台'))
        self.assertEqual(book.load('watchlist', self.dir)[0]['symbol'], 'sh600519')   # 买入后仍留在自选

    def test_second_buy_uses_the_weighted_average_cost(self):
        self.watch()
        self.buy(shares='100', price='10')
        self.buy(shares='300', price='12', now=LATER)
        h = self.holding()
        self.assertEqual((h['shares'], h['cost_price']), (400, 11.5))    # (100×10 + 300×12) / 400
        self.assertEqual(h['opened_on'], '2026-09-21')

    def test_back_dated_buy_moves_the_open_date_earlier(self):
        self.watch()
        self.buy(shares='100', price='10')
        self.buy(shares='100', price='10', date='2026-09-01')
        self.assertEqual(self.holding()['opened_on'], '2026-09-01')

    def test_every_trade_lands_in_the_ledger_newest_first(self):
        self.watch()
        self.buy()
        self.sell(shares='100', price='11')
        trades = book.recent_trades(10, self.dir)
        self.assertEqual([t['side'] for t in trades], ['sell', 'buy'])
        self.assertEqual((trades[0]['cost_price'], trades[0]['realized_pnl']), (10.0, 100.0))

    def test_partial_sell_keeps_cost_and_records_realized_pnl(self):
        self.watch()
        self.buy(shares='300', price='10')
        self.sell(shares='100', price='12')
        h = self.holding()
        self.assertEqual((h['shares'], h['cost_price']), (200, 10.0))
        self.assertEqual(book.recent_trades(1, self.dir)[0]['realized_pnl'], 200.0)

    def test_selling_everything_clears_the_holding_but_not_the_history(self):
        self.watch()
        self.buy()
        self.sell()
        self.assertEqual(book.load('holdings', self.dir), [])
        self.assertEqual(len(book.load('trades', self.dir)), 2)

    def test_cannot_sell_more_than_held_or_something_not_held(self):
        self.watch()
        self.buy(shares='100')
        with self.assertRaises(book.BookError):
            self.sell(shares='200')
        with self.assertRaises(book.BookError):
            self.sell(symbol='000001')
        self.assertEqual(self.holding()['shares'], 100)              # 失败的卖出不改账

    def test_t_plus_1_shares_bought_today_cannot_be_sold_today(self):
        self.watch()
        self.buy(shares='300', price='10', date='2026-09-18')        # 老仓
        self.buy(shares='200', price='10')                           # 今天买的
        with self.assertRaises(book.BookError) as ctx:
            self.sell(shares='400', now=NOW)
        self.assertIn('T+1', str(ctx.exception))
        self.sell(shares='300', now=NOW)                             # 老仓可以卖
        self.assertEqual(self.holding()['shares'], 200)
        # 第二天今天买的就能卖了
        self.sell(shares='200', now=LATER)
        self.assertEqual(book.load('holdings', self.dir), [])

    def test_sellable_shares_follow_the_trade_ledger(self):
        self.watch()
        self.buy(shares='300', price='10', date='2026-09-18')
        self.buy(shares='200', price='10')
        holdings = book.load('holdings', self.dir)
        self.assertEqual(book.with_sellable(holdings, now=NOW, directory=self.dir)[0]['sellable_shares'], 300)
        self.assertEqual(book.with_sellable(holdings, now=LATER, directory=self.dir)[0]['sellable_shares'], 500)

    def test_selling_an_odd_remainder_is_only_allowed_when_it_is_everything(self):
        self.watch()
        self.buy(shares='200')
        with self.assertRaises(book.BookError):
            self.sell(shares='150')

    def test_rewriting_a_legacy_row_drops_the_declared_fields(self):
        """旧版本录入的持仓带着 hold_type/止损/目标/做T底仓；这次改写就该把它们清掉。"""
        self.watch()
        self.buy(shares='300', price='10', date='2026-09-18')
        path = os.path.join(self.dir, 'holdings.json')
        with open(path) as f:
            rows = json.load(f)
        rows[0].update({'hold_type': 'swing', 'stop_price': 9.0, 'target_price': 12.0, 't_base_shares': 100})
        with open(path, 'w') as f:
            json.dump(rows, f)
        self.sell(shares='100')
        self.assertFalse({'hold_type', 'stop_price', 'target_price', 't_base_shares'} & set(self.holding()))

    def test_remove_reports_missing_symbol(self):
        self.watch()
        self.buy()
        book.remove('holdings', '600519', self.dir)
        self.assertEqual(book.load('holdings', self.dir), [])
        with self.assertRaises(book.BookError):
            book.remove('holdings', '600519', self.dir)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(book.load('holdings', self.dir), [])
        self.assertEqual(book.load('trades', self.dir), [])

    def test_corrupt_file_is_reported_not_silently_ignored(self):
        path = os.path.join(self.dir, 'holdings.json')
        with open(path, 'w') as f:
            f.write('{"not": "a list"}')
        with self.assertRaises(book.BookError):
            book.load('holdings', self.dir)

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        self.watch()
        self.buy()
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith('.tmp')], [])
        with open(os.path.join(self.dir, 'holdings.json')) as f:
            self.assertEqual(len(json.load(f)), 1)

    def test_all_symbols_merges_both_books(self):
        self.watch('600519')
        self.watch('000001', '平安银行')
        self.buy()
        self.assertEqual(book.all_symbols(self.dir), ['sh600519', 'sz000001'])   # 同时持有又自选，只算一次


if __name__ == '__main__':
    unittest.main()
