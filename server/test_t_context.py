import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import t_context as tc
from collect_quotes import CST
from tushare_sync import save

NOW = datetime(2026, 9, 21, 10, 30, tzinfo=CST)
DAY = '2026-09-21'


def stock(code, exchange, industry, name=None):
    return {'ts_code': '%s.%s' % (code, exchange), 'symbol': code, 'name': name or ('股' + code), 'industry': industry,
            'exchange': 'SSE' if exchange == 'SH' else 'SZSE', 'list_status': 'L'}


def history(rows):
    d = Path(tempfile.mkdtemp())
    save(d / 'tushare_data' / 'stock_basic.json', {'rows': rows})
    return d


def quote(symbol, pct, day=DAY, **kw):
    return {'symbol': symbol, 'change_pct': str(pct), 'quote_date': day, 'is_index': False, **kw}


class MarketTests(unittest.TestCase):
    QUOTES = {'sh000001': {'change_pct': '0.40'}, 'sh000300': {'change_pct': '0.80'}, 'sz399006': {'change_pct': '-1.20'}}

    def test_main_board_stocks_use_the_shanghai_and_csi300_average(self):
        m = tc.market_change('sh600519', self.QUOTES)
        self.assertEqual((m['change'], m['name']), (0.6, '上证/沪深300'))

    def test_chinext_and_star_stocks_use_the_chinext_index(self):
        for symbol in ('sz300458', 'sz301001', 'sh688981'):
            self.assertEqual(tc.market_change(symbol, self.QUOTES)['change'], -1.2)

    def test_missing_index_quotes_are_none_not_zero(self):
        self.assertIsNone(tc.market_change('sh600519', {}))
        self.assertEqual(tc.market_change('sh600519', {'sh000300': {'change_pct': '1.0'}})['name'], '沪深300')


class PeerTests(unittest.TestCase):
    def rows(self, n=100):
        rows = [stock('%06d' % (600000 + i), 'SH', '银行') for i in range(n)]
        rows += [stock('000001', 'SZ', '银行', 'ST银行'), stock('000002', 'SZ', '银行', '退市银行'), stock('000003', 'SZ', '软件服务'),
                 {**stock('000004', 'SZ', '银行'), 'list_status': 'D'}]
        return rows

    def test_samples_evenly_from_the_same_industry_and_excludes_self_st_delisted_and_other_industries(self):
        h = history(self.rows())
        industry, syms = tc.peers(h, 'sh600000')
        self.assertEqual(industry, '银行')
        self.assertEqual(len(syms), 40)
        self.assertNotIn('sh600000', syms)
        self.assertFalse({'sz000001', 'sz000002', 'sz000003', 'sz000004'} & set(syms))
        self.assertGreater(int(syms[-1][2:]), 600070)                    # 抽样覆盖到后段，不是只取前 40 只

    def test_small_industries_use_everyone_and_unknown_stock_has_no_peers(self):
        h = history(self.rows(12))
        self.assertEqual(len(tc.peers(h, 'sh600000')[1]), 11)
        self.assertEqual(tc.peers(h, 'sh699999'), (None, []))


class SectorTests(unittest.TestCase):
    def setUp(self):
        self.h = history([stock('%06d' % (600000 + i), 'SH', '银行') for i in range(30)])
        self.calls = []
        self.cache = Path(tempfile.mkdtemp())

    def snap(self, changes, day=DAY):
        def fn(symbols):
            self.calls.append(list(symbols))
            return {'quotes': [quote(s, c, day) for s, c in zip(symbols, changes)]}
        return fn

    def test_is_the_median_of_the_peers_not_the_mean(self):
        out = tc.sector_change(self.h, 'sh600000', DAY, NOW, self.snap([1.0] * 14 + [1.2] * 14 + [30.0]))
        self.assertEqual((out['industry'], out['change'], out['n']), ('银行', 1.2, 29))

    def test_too_few_valid_peers_or_a_stale_day_gives_none(self):
        self.assertIsNone(tc.sector_change(self.h, 'sh600000', DAY, NOW, self.snap([1.0] * 5)))
        self.assertIsNone(tc.sector_change(self.h, 'sh600000', DAY, NOW, self.snap([1.0] * 29, day='2026-09-18')))

    def test_network_failure_and_missing_stock_list_give_none_not_an_exception(self):
        def boom(symbols):
            raise OSError('down')
        self.assertIsNone(tc.sector_change(self.h, 'sh600000', DAY, NOW, boom))
        self.assertIsNone(tc.sector_change(Path(tempfile.mkdtemp()), 'sh600000', DAY, NOW, self.snap([1.0] * 29)))

    def test_result_is_cached_for_five_minutes_then_refetched(self):
        fn = self.snap([1.0] * 29)
        tc.sector_change(self.h, 'sh600000', DAY, NOW, fn, self.cache)
        tc.sector_change(self.h, 'sh600001', DAY, NOW + timedelta(minutes=4), fn, self.cache)     # 同行业，命中缓存
        self.assertEqual(len(self.calls), 1)
        tc.sector_change(self.h, 'sh600000', DAY, NOW + timedelta(minutes=6), fn, self.cache)
        self.assertEqual(len(self.calls), 2)

    def test_a_corrupt_cache_file_is_ignored(self):
        (self.cache / ('sector-%s.json' % DAY)).write_text('{not json')
        self.assertIsNotNone(tc.sector_change(self.h, 'sh600000', DAY, NOW, self.snap([1.0] * 29), self.cache))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, *rows):
        (self.dir / ('flow-%s.jsonl' % DAY)).write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def row(self, at, **kw):
        return {'at': at.isoformat(), 'symbol': 'sz000001', 'as_of': '1025', 'main': -2e7, 'main_30m': -5e6, **kw}

    def test_returns_the_latest_successful_row(self):
        self.write(self.row(NOW - timedelta(minutes=10), main=1.0), self.row(NOW - timedelta(minutes=3)),
                   {'at': NOW.isoformat(), 'symbol': 'sz000001', 'error': '接口挂了'})
        self.assertEqual(tc.flow_facts(self.dir, 'sz000001', DAY, NOW), {'main': -2e7, 'main_30m': -5e6, 'as_of': '1025'})

    def test_todays_row_older_than_fifteen_minutes_is_not_used(self):
        self.write(self.row(NOW - timedelta(minutes=20)))
        self.assertIsNone(tc.flow_facts(self.dir, 'sz000001', DAY, NOW))

    def test_a_past_days_last_row_is_fine_for_the_after_hours_page(self):
        self.write(self.row(NOW - timedelta(hours=5)))
        self.assertIsNotNone(tc.flow_facts(self.dir, 'sz000001', DAY, NOW + timedelta(days=1)))

    def test_missing_file_or_incomplete_row_is_none(self):
        self.assertIsNone(tc.flow_facts(self.dir, 'sz000001', DAY, NOW))
        self.write(self.row(NOW, main_30m=None))
        self.assertIsNone(tc.flow_facts(self.dir, 'sz000001', DAY, NOW))


class BuildEnvTests(unittest.TestCase):
    def test_assembles_all_three_and_never_raises(self):
        quotes = {'sh000300': {'change_pct': '0.5'}}
        env = tc.build_env('sh600519', quotes, DAY, lambda s, d: {'industry': '白酒', 'change': 0.3, 'n': 25},
                           lambda s, d: {'main': 1.0, 'main_30m': 2.0, 'as_of': '1030'})
        self.assertEqual((env['market'], env['sector'], env['sector_name'], env['sector_n'], env['flow']['main']),
                         (0.5, 0.3, '白酒', 25, 1.0))

        def boom(s, d):
            raise RuntimeError('x')
        env = tc.build_env('sh600519', {}, DAY, boom, boom)
        self.assertEqual((env['market'], env['sector'], env['flow']), (None, None, None))


if __name__ == '__main__':
    unittest.main()
