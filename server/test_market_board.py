import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import market_board as mb
import tushare_sync


def write(history, folder, day, rows, corrupt=False):
    d = Path(history) / 'tushare_data' / folder
    d.mkdir(parents=True, exist_ok=True)
    payload = {'fetched_at': 'x', 'source': 't', 'trade_date': day, 'rows': rows}
    env = {'sha256': 'bad' if corrupt else tushare_sync.sha(payload), 'payload': payload}
    (d / (day + '.json')).write_text(json.dumps(env, ensure_ascii=False))


def limit(code, kind, times=1.0, fd=1e6, pct=10.0, name='测试'):
    return {'trade_date': '20260915', 'ts_code': code, 'name': name, 'close': 1.0, 'pct_chg': pct,
            'limit_amount': 1.0, 'fd_amount': fd, 'open_times': 0, 'limit_times': times, 'limit': kind}


def hm(code, net, desk='游资甲', name='测试'):
    return {'trade_date': '20260915', 'ts_code': code, 'ts_name': name, 'buy_amount': 1.0, 'sell_amount': 1.0,
            'net_amount': net, 'hm_name': desk, 'hm_orgs': desk}


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.h = Path(tempfile.mkdtemp())
        mb._cache.clear()

    def test_no_data_at_all_is_none_not_an_error(self):
        self.assertEqual(mb.build(self.h), {'hotmoney_board': None, 'limit_counts': None})
        self.assertEqual(mb.build(self.h / 'missing'), {'hotmoney_board': None, 'limit_counts': None})

    def test_each_table_uses_its_own_latest_day_with_data_and_skips_empty_or_corrupt_files(self):
        write(self.h, 'hm_detail', '20260915', [hm('000428.SZ', 5e7)])
        write(self.h, 'hm_detail', '20260917', [])                                   # 最新一天是空的
        write(self.h, 'limit_list_d', '20260915', [limit('000428.SZ', 'U')])
        write(self.h, 'limit_list_d', '20260916', [limit('000001.SZ', 'D')], corrupt=True)   # 校验和不对：不信
        b = mb.build(self.h)['hotmoney_board']
        self.assertEqual((b['hm_date'], b['limit_date']), ('2026-09-15', '2026-09-15'))

    def test_hm_rows_sorted_by_absolute_net_amount_with_bj_codes_kept_and_junk_dropped(self):
        write(self.h, 'hm_detail', '20260915', [
            hm('000428.SZ', 5e7), hm('600000.SH', -9e7, '游资乙'), hm('830799.BJ', 1e6), hm('bad', 1e9),
            {'ts_code': '000001.SZ', 'net_amount': None}])
        b = mb.build(self.h)['hotmoney_board']
        self.assertEqual([r['symbol'] for r in b['hm_rows']], ['sh600000', 'sz000428', 'bj830799'])
        self.assertEqual(b['hm_total_rows'], 3)
        self.assertEqual(b['hm_rows'][0]['hm_name'], '游资乙')
        self.assertEqual(b['limit_rows'], [])
        self.assertIsNone(b['limit_date'])

    def test_limit_pool_orders_up_by_streak_then_broken_then_down_and_counts_all_three(self):
        write(self.h, 'limit_list_d', '20260915', [
            limit('000001.SZ', 'D', fd=9e9), limit('000002.SZ', 'Z'), limit('000003.SZ', 'U', times=1.0, fd=5e6),
            limit('000004.SZ', 'U', times=3.0, fd=1e6), limit('000005.SZ', 'U', times=1.0, fd=9e6),
            limit('000006.SZ', 'X')])                                                 # 未知状态不展示
        out = mb.build(self.h)
        self.assertEqual([r['symbol'] for r in out['hotmoney_board']['limit_rows']],
                         ['sz000004', 'sz000005', 'sz000003', 'sz000002', 'sz000001'])
        self.assertEqual(out['hotmoney_board']['limit_rows'][0]['limit_times'], 3)
        self.assertEqual(out['limit_counts'], {'date': '2026-09-15', 'up': 3, 'down': 1, 'broken': 1})

    def test_tables_are_capped_but_totals_are_not(self):
        write(self.h, 'limit_list_d', '20260915', [limit('%06d.SZ' % i, 'U') for i in range(1, 61)])
        write(self.h, 'hm_detail', '20260915', [hm('%06d.SZ' % i, float(i)) for i in range(1, 51)])
        b = mb.build(self.h)['hotmoney_board']
        self.assertEqual((len(b['limit_rows']), b['limit_total_rows']), (mb.LIMIT_TOP, 60))
        self.assertEqual((len(b['hm_rows']), b['hm_total_rows']), (mb.HM_TOP, 50))

    def test_result_is_cached_briefly(self):
        write(self.h, 'limit_list_d', '20260915', [limit('000001.SZ', 'U')])
        first = mb.build(self.h, now=1000.0)
        write(self.h, 'limit_list_d', '20260916', [limit('000001.SZ', 'D'), limit('000002.SZ', 'D')])
        self.assertEqual(mb.build(self.h, now=1030.0), first)                         # 60 秒内不重读
        self.assertEqual(mb.build(self.h, now=1100.0)['limit_counts']['down'], 2)

    def test_approx_limits_falls_back_to_the_universe_archive_and_says_so(self):
        ready = {'status': 'ready', 'limit_up_approx': 12, 'limit_down_approx': 3,
                 'source_generated_at': '2026-09-18T15:40:00+08:00', 'note': '近似'}
        with patch('market_context.breadth_from_universe', return_value=ready):
            self.assertEqual(mb.approx_limits(self.h)['up'], 12)
        with patch('market_context.breadth_from_universe', return_value={'status': 'missing'}):
            self.assertIsNone(mb.approx_limits(self.h))
        with patch('market_context.breadth_from_universe', side_effect=RuntimeError('x')):
            self.assertIsNone(mb.approx_limits(self.h))


if __name__ == '__main__':
    unittest.main()
