import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import market_review as mr
from collect_quotes import CST


def pool_payload(rows, tc=None):
    return {'data': {'tc': len(rows) if tc is None else tc, 'qdate': 20260921, 'pool': rows}}


def zt_row(code='000504', name='南华生物', lbc=3, fbt=92500, zbc=0, p=11010, zdp=9.99, hybk='医疗服务'):
    return {'c': code, 'm': 0, 'n': name, 'p': p, 'zdp': zdp, 'amount': 5.8e7, 'ltsz': 3.6e9, 'hs': 1.62,
            'lbc': lbc, 'fbt': fbt, 'lbt': 93000, 'fund': 1.8e8, 'zbc': zbc, 'hybk': hybk}


class HttpFallbackTests(unittest.TestCase):
    def test_push2_falls_back_to_http_only_after_https_fails(self):
        calls = []

        def http(url):
            calls.append(url)
            if url.startswith('https://'):
                raise OSError('tls reset')
            return b'{"ok": 1}'
        self.assertEqual(mr.get_json('https://push2.eastmoney.com/api/x', http), {'ok': 1})
        self.assertEqual([u.split(':')[0] for u in calls], ['https', 'https', 'http'])        # 先 https 试两次，才降级

    def test_healthy_https_never_downgrades(self):
        calls = []
        http = lambda url: calls.append(url) or b'{"ok": 1}'
        mr.get_json('https://push2.eastmoney.com/api/x', http)
        self.assertEqual(calls, ['https://push2.eastmoney.com/api/x'])

    def test_other_hosts_never_downgrade(self):
        calls = []

        def http(url):
            calls.append(url)
            raise OSError('down')
        with self.assertRaises(mr.ReviewError):
            mr.get_json('https://datacenter-web.eastmoney.com/api/x', http)
        self.assertTrue(all(u.startswith('https://') for u in calls))

    def test_both_failing_raises_review_error(self):
        def http(url):
            raise OSError('down')
        with self.assertRaises(mr.ReviewError):
            mr.get_json('https://push2his.eastmoney.com/api/x', http)


class PoolTests(unittest.TestCase):
    def test_zt_row_is_normalised(self):
        total, rows = mr.parse_pool('zt', pool_payload([zt_row()]))
        self.assertEqual(total, 1)
        r = rows[0]
        self.assertEqual((r['symbol'], r['name'], r['price'], r['boards'], r['first_seal'], r['industry']),
                         ('sz000504', '南华生物', 11.01, 3, '09:25:00', '医疗服务'))
        self.assertEqual(r['open_times'], 0)

    def test_seal_time_after_noon_and_missing(self):
        self.assertEqual(mr.hhmmss(130001), '13:00:01')
        self.assertEqual(mr.hhmmss(93000), '09:30:00')
        self.assertIsNone(mr.hhmmss(0))
        self.assertIsNone(mr.hhmmss(None))

    def test_symbol_prefix_by_code(self):
        self.assertEqual(mr.symbol_of_code('600721'), 'sh600721')
        self.assertEqual(mr.symbol_of_code('688112'), 'sh688112')
        self.assertEqual(mr.symbol_of_code('300829'), 'sz300829')
        self.assertEqual(mr.symbol_of_code('002080'), 'sz002080')
        self.assertEqual(mr.symbol_of_code('920298'), 'bj920298')
        self.assertIsNone(mr.symbol_of_code('12345'))
        self.assertIsNone(mr.symbol_of_code(None))

    def test_row_without_price_is_dropped_not_zeroed(self):
        bad = zt_row(code='000505')
        bad['p'] = None
        total, rows = mr.parse_pool('zt', pool_payload([zt_row(), bad]))
        self.assertEqual([r['code'] for r in rows], ['000504'])
        self.assertEqual(total, 2)

    def test_malformed_payload_raises(self):
        for bad in ({}, {'data': None}, {'data': {'tc': 'x', 'pool': []}}, {'data': {'tc': 1, 'pool': 'no'}}):
            with self.assertRaises(mr.ReviewError):
                mr.parse_pool('zt', bad)

    def test_dt_zb_yzt_qs_specific_fields(self):
        _, dt = mr.parse_pool('dt', pool_payload([{**zt_row(), 'days': 2, 'oc': 3, 'lbt': 145609, 'fund': 1926825}]))
        self.assertEqual((dt[0]['days'], dt[0]['open_times'], dt[0]['last_seal']), (2, 3, '14:56:09'))
        _, zb = mr.parse_pool('zb', pool_payload([{**zt_row(), 'zbc': 2, 'zf': 9.28, 'ztp': 18020}]))
        self.assertEqual((zb[0]['open_times'], zb[0]['amplitude'], zb[0]['limit_price']), (2, 9.28, 18.02))
        _, yz = mr.parse_pool('yzt', pool_payload([{**zt_row(), 'ylbc': 2, 'yfbt': 130001, 'zf': 5.0}]))
        self.assertEqual((yz[0]['boards'], yz[0]['first_seal']), (2, '13:00:01'))
        _, qs = mr.parse_pool('qs', pool_payload([{**zt_row(), 'nh': 1, 'lb': 15.47}]))
        self.assertEqual((qs[0]['new_high'], qs[0]['volume_ratio']), (1, 15.47))

    def test_find_trade_day_skips_weekend_and_empty_days(self):
        calls = []

        def http(url):
            calls.append(url)
            day = url.split('date=')[1][:8]
            rows = [zt_row()] if day == '20260918' else []
            return json.dumps(pool_payload(rows)).encode()
        sunday = datetime(2026, 9, 20, 12, 0, tzinfo=CST)
        day, pool = mr.find_trade_day(sunday, http)
        self.assertEqual(day, '20260918')
        self.assertEqual(pool['total'], 1)
        self.assertEqual(len(calls), 1)                      # 周日、周六直接跳过，不打接口

    def test_find_trade_day_none_when_nothing(self):
        http = lambda url: json.dumps(pool_payload([])).encode()
        self.assertEqual(mr.find_trade_day(datetime(2026, 9, 21, tzinfo=CST), http), (None, None))

    def test_fetch_pools_isolates_failures(self):
        def http(url):
            if 'getTopicZBPool' in url:
                raise OSError('boom')
            return json.dumps(pool_payload([zt_row()])).encode()
        out = mr.fetch_pools('20260921', http)
        self.assertIsNone(out['zb'])
        self.assertIn('zb', out['errors'])
        self.assertEqual(out['zt']['total'], 1)


def lhb_row(code, net, reason='日涨幅偏离值达到7%的前5只证券', buy=None, sell=None):
    return {'SECURITY_CODE': code, 'SECURITY_NAME_ABBR': '甲' + code, 'CLOSE_PRICE': 13.15, 'CHANGE_RATE': 10.04,
            'BILLBOARD_BUY_AMT': buy if buy is not None else max(net, 0) + 1e6, 'BILLBOARD_SELL_AMT': sell if sell is not None else 1e6 - min(net, 0),
            'BILLBOARD_NET_AMT': net, 'TURNOVERRATE': 29.0, 'FREE_MARKET_CAP': 5e9, 'EXPLANATION': reason}


class LhbTests(unittest.TestCase):
    def test_merges_reasons_and_prefers_single_day_amounts(self):
        rows = [lhb_row('600721', 2.27e8, '非S证券连续三个交易日内收盘价格涨幅偏离值累计达到20%的证券'),
                lhb_row('600721', 1.66e8, '日收盘价格涨幅偏离值达到7%的前五只证券'),
                lhb_row('000002', -1.9e8)]
        out = mr.parse_lhb(rows)
        self.assertEqual([r['symbol'] for r in out], ['sh600721', 'sz000002'])     # 按净买入降序
        self.assertEqual(out[0]['net'], 1.66e8)                                     # 不是累计口径的 2.27e8
        self.assertEqual(len(out[0]['reasons']), 2)

    def test_only_multiday_reason_falls_back_to_it(self):
        out = mr.parse_lhb([lhb_row('600721', 2.27e8, '连续三个交易日累计达到20%')])
        self.assertEqual(out[0]['net'], 2.27e8)

    def test_rows_without_net_are_skipped(self):
        bad = lhb_row('600721', 1.0)
        bad['BILLBOARD_NET_AMT'] = None
        self.assertEqual(mr.parse_lhb([bad]), [])

    def test_seats_group_by_reason_and_merge_identical_groups(self):
        def seat(reason, name, buy=None, sell=None):
            return {'EXPLANATION': reason, 'OPERATEDEPT_NAME': name, 'BUY': buy, 'SELL': sell,
                    'NET': (buy or 0) - (sell or 0)}
        r7, r20 = '日涨幅7%', '日换手20%'
        buys = [seat(r, 'A营业部', buy=100.0) for r in (r7, r20)] + [seat('连续三日', 'A营业部', buy=300.0)]
        sells = [seat(r, 'B营业部', sell=40.0) for r in (r7, r20)] + [seat('连续三日', 'B营业部', sell=90.0)]
        groups = mr.parse_seats(buys, sells)
        self.assertEqual(len(groups), 2)                                            # 7% 与 20% 完全相同 → 合并
        self.assertEqual(sorted(groups[0]['reasons']), sorted([r7, r20]))           # 当日口径排在前
        self.assertEqual((groups[0]['buy_total'], groups[0]['sell_total'], groups[0]['net']), (100.0, 40.0, 60.0))
        self.assertTrue(groups[1]['multiday'])

    def test_fetch_lhb_treats_null_result_as_empty(self):
        http = lambda url: b'{"result":null,"success":false,"message":"no data"}'
        out = mr.fetch_lhb('20260921', http)
        self.assertEqual(out['rows'], [])
        self.assertRegex(out['fetched_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+08:00$')          # 精确到秒

    def test_fetch_seats_rejects_bad_code(self):
        with self.assertRaises(mr.ReviewError):
            mr.fetch_seats('60072', '20260921', lambda u: b'{}')


class MarketStatsTests(unittest.TestCase):
    def test_breadth_sums_markets(self):
        p = {'data': {'diff': [{'f104': 1840, 'f105': 473, 'f106': 41}, {'f104': 2423, 'f105': 456, 'f106': 53},
                               {'f104': 320, 'f105': 22, 'f106': 2}]}}
        b = mr.parse_breadth(p)
        self.assertEqual((b['up'], b['down'], b['flat'], b['total']), (4583, 951, 96, 5630))
        self.assertEqual(b['up_pct'], 81.4)

    def test_breadth_empty_or_partial_rows(self):
        with self.assertRaises(mr.ReviewError):
            mr.parse_breadth({'data': {'diff': [{'f104': '-', 'f105': '-', 'f106': '-'}]}})
        with self.assertRaises(mr.ReviewError):
            mr.parse_breadth({})

    def test_turnover_expand_and_shrink(self):
        def k(*amts):
            return {'data': {'klines': ['2026-09-%02d,1,1,1,1,1,%s' % (d, a) for d, a in amts]}}
        sh = k((18, 9.9e11), (21, 9.4e11))
        sz = k((18, 1.1e12), (21, 1.09e12))
        t = mr.parse_turnover_klines(sh, sz)
        self.assertEqual((t['date'], t['prev_date'], t['trend']), ('2026-09-21', '2026-09-18', 'shrink'))
        self.assertAlmostEqual(t['delta_pct'], -2.9, places=1)
        up = mr.parse_turnover_klines(k((18, 1e11), (21, 2e11)), k((18, 1e11), (21, 2e11)))
        self.assertEqual(up['trend'], 'expand')

    def test_turnover_refuses_misaligned_days(self):
        one = {'data': {'klines': ['2026-09-21,1,1,1,1,1,5']}}
        two = {'data': {'klines': ['2026-09-18,1,1,1,1,1,5', '2026-09-21,1,1,1,1,1,5']}}
        with self.assertRaises(mr.ReviewError):
            mr.parse_turnover_klines(one, two)

    def test_flow(self):
        f = mr.parse_flow({'data': {'klines': ['2026-09-21,8.6e9,5.4e9,-1.4e10,-1.8e9,1.04e10']}})
        self.assertEqual((f['main_net'], f['small_net'], f['mid_net'], f['big_net'], f['huge_net']), (8.6e9, 5.4e9, -1.4e10, -1.8e9, 1.04e10))
        with self.assertRaises(mr.ReviewError):
            mr.parse_flow({'data': {'klines': []}})

    def test_indices_come_from_snapshot_not_archive(self):
        snap = {'quotes': [{'symbol': 'sh000001', 'last': '3949.91', 'change_pct': '0.97', 'quote_at': 'x', 'amount_wan': '9.4e7'}],
                'session': 'post_close', 'fetched_at': 'now'}
        out = mr.fetch_indices(snap)
        self.assertEqual(out['quotes'][0]['name'], '上证指数')
        with self.assertRaises(mr.ReviewError):
            mr.fetch_indices({'quotes': []})

    def test_prev_turnover_and_today_from_quotes(self):
        def k(*amts):
            return {'data': {'klines': ['%s,1,1,1,1,1,%s' % (d, a) for d, a in amts]}}
        sh, sz = k(('2026-09-18', 9e11), ('2026-09-21', 5e11)), k(('2026-09-18', 1.1e12), ('2026-09-21', 6e11))
        self.assertEqual(mr.parse_prev_turnover(sh, sz, '2026-09-21'), ('2026-09-18', 2.0e12))     # 严格早于今天
        self.assertEqual(mr.parse_prev_turnover(sh, sz, '2026-09-19'), ('2026-09-18', 2.0e12))
        with self.assertRaises(mr.ReviewError):
            mr.parse_prev_turnover(sh, sz, '2026-09-18')
        idx = {'quotes': [{'symbol': 'sh000001', 'amount_wan': 9.4e7, 'quote_at': '2026-09-21T15:00:03+08:00'},
                          {'symbol': 'sz399001', 'amount_wan': 1.09e8, 'quote_at': '2026-09-21T15:00:03+08:00'}]}
        t = mr.turnover_from_quotes(idx, ('2026-09-18', 2.0e12))
        self.assertAlmostEqual(t['amount'], 2.03e12)
        self.assertEqual((t['trend'], t['date'], t['prev_date']), ('expand', '2026-09-21', '2026-09-18'))

    def test_turnover_from_quotes_refuses_bad_inputs(self):
        prev = ('2026-09-18', 2e12)
        with self.assertRaises(mr.ReviewError):
            mr.turnover_from_quotes({'quotes': [{'symbol': 'sh000001', 'amount_wan': 1, 'quote_at': '2026-09-21T15:00:00+08:00'}]}, prev)
        with self.assertRaises(mr.ReviewError):
            mr.turnover_from_quotes({'quotes': [{'symbol': 'sh000001', 'amount_wan': 1, 'quote_at': '2026-09-21T15:00:00+08:00'},
                                                {'symbol': 'sz399001', 'amount_wan': 1, 'quote_at': '2026-09-18T15:00:00+08:00'}]}, prev)
        with self.assertRaises(mr.ReviewError):
            mr.turnover_from_quotes(None, prev)

    def stats_http(self, calls, fail=()):
        def http(url):
            calls.append(url)
            for word in fail:
                if word in url:
                    raise OSError('down')
            if 'ulist' in url:
                return json.dumps({'data': {'diff': [{'f104': 3, 'f105': 1, 'f106': 0}]}}).encode()
            if 'fflow' in url:
                return json.dumps({'data': {'klines': ['2026-09-21,1,2,3,4,5']}}).encode()
            return json.dumps({'data': {'klines': ['2026-09-18,1,1,1,1,1,10', '2026-09-21,1,1,1,1,1,12']}}).encode()
        return http

    SNAP = {'quotes': [{'symbol': 'sh000001', 'last': '1', 'change_pct': '0', 'quote_at': '2026-09-21T15:00:00+08:00', 'amount_wan': '100'},
                       {'symbol': 'sz399001', 'last': '1', 'change_pct': '0', 'quote_at': '2026-09-21T15:00:00+08:00', 'amount_wan': '200'}],
            'session': 'post_close'}

    def test_market_stats_degrades_per_block(self):
        mr._live_cache.clear()
        calls = []
        out = mr.market_stats(http=self.stats_http(calls, fail=('ulist',)), snapshot=self.SNAP, force=True)
        self.assertIsNone(out['breadth'])
        self.assertIn('breadth', out['errors'])
        self.assertEqual(out['flow']['main_net'], 1.0)
        self.assertEqual(out['turnover']['trend'], 'expand')          # 3e6 元 vs 昨日 2e1 元（夹具里的数）
        self.assertEqual(out['turnover']['quote_at'], '2026-09-21T15:00:00+08:00')
        self.assertRegex(out['flow']['fetched_at'], r'T\d\d:\d\d:\d\d\+08:00$')
        self.assertRegex(out['fetched_at'], r'T\d\d:\d\d:\d\d\+08:00$')
        self.assertTrue(out['complete'])
        mr._live_cache.clear()

    def test_market_stats_hits_the_external_api_sparingly(self):
        mr._live_cache.clear()
        calls = []
        http = self.stats_http(calls)
        mr.market_stats(http=http, snapshot=self.SNAP, force=True)
        first = len(calls)
        mr.market_stats(http=http, snapshot=self.SNAP)                 # 缓存期内再来一次
        self.assertEqual(len(calls), first)
        mr.market_stats(http=http, snapshot=self.SNAP, force=True)     # 强制刷新：breadth/flow 重取，上一交易日成交额不重取
        self.assertEqual(len(calls) - first, 2)
        self.assertEqual(sum('push2his' in u for u in calls), 2)      # 沪、深各一次，只在第一轮
        mr._live_cache.clear()

    def test_stale_value_is_used_briefly_when_the_source_fails(self):
        mr._live_cache.clear()
        mr.market_stats(http=self.stats_http([]), snapshot=self.SNAP, force=True)
        out = mr.market_stats(http=self.stats_http([], fail=('ulist',)), snapshot=self.SNAP, force=True)
        self.assertTrue(out['breadth']['stale'])
        self.assertNotIn('breadth', out['errors'])
        key = 'breadth'
        ts, val = mr._live_cache[key]
        mr._live_cache[key] = (ts - mr.STALE_OK_SECONDS - 1, val)      # 太旧了就不再顶着用
        out = mr.market_stats(http=self.stats_http([], fail=('ulist',)), snapshot=self.SNAP, force=True)
        self.assertIsNone(out['breadth'])
        self.assertIn('breadth', out['errors'])
        mr._live_cache.clear()


class TimestampTests(unittest.TestCase):
    def test_lhb_block_falls_back_to_the_review_time_for_old_files(self):
        stored = {'trade_date': '20260921', 'date': '2026-09-21', 'fetched_at': '2026-09-21T17:30:05+08:00', 'lhb': {'rows': []}}
        self.assertEqual(mr._lhb_block(stored)['fetched_at'], '2026-09-21T17:30:05+08:00')
        stored['lhb']['fetched_at'] = '2026-09-21T17:30:41+08:00'
        self.assertEqual(mr._lhb_block(stored)['fetched_at'], '2026-09-21T17:30:41+08:00')

    def test_current_review_never_replaces_stored_review_with_intraday_pool(self):
        stored = {'trade_date': '20260918', 'date': '2026-09-18', 'fetched_at': 'saved',
                  'pools': {'zt': {'total': 1, 'rows': [{'symbol': 'sh600000'}]}}, 'errors': {}}
        def must_not_fetch(_url):
            raise AssertionError('盘中不应抓取当天复盘池')
        with patch('market_review.load_latest', return_value=stored):
            review = mr.current_review(now=datetime(2026, 9, 21, 10, 0, tzinfo=CST),
                                       http=must_not_fetch)
        self.assertEqual(review['trade_date'], '20260918')
        self.assertEqual(review['source'], 'stored')

    def test_current_quotes_are_added_without_overwriting_review_values(self):
        review = {'pools': {'zt': {'total': 1, 'rows': [
            {'symbol': 'sh600000', 'price': 10.0, 'pct': 10.0}]}}}
        snap = lambda _symbols: {'fetched_at': 'now', 'failures': [], 'quotes': [
            {'symbol': 'sh600000', 'last': '10.50', 'change_pct': '5.00', 'quote_at': 'quote'}]}
        out = mr.with_current_quotes(review, snap)
        row = out['pools']['zt']['rows'][0]
        self.assertEqual((row['price'], row['pct']), (10.0, 10.0))
        self.assertEqual((row['current_price'], row['current_pct']), (10.5, 5.0))
        self.assertEqual(out['current_quotes_at'], 'now')
        self.assertNotIn('current_pct', review['pools']['zt']['rows'][0])


class PersistTests(unittest.TestCase):
    def test_save_load_latest_skips_corrupt(self):
        d = Path(tempfile.mkdtemp())
        mr.save_review({'trade_date': '20260918', 'x': 1}, d)
        mr.save_review({'trade_date': '20260921', 'x': 2}, d)
        self.assertEqual(mr.load_latest(d)['x'], 2)
        p = d / '20260921.json'
        p.write_text(p.read_text().replace('"x": 2', '"x": 9'))
        self.assertEqual(mr.load_latest(d)['x'], 1)                                  # 损坏的退回前一天
        self.assertIsNone(mr.load_latest(d / 'missing'))

    def test_run_keeps_existing_next_day_watch(self):
        d = Path(tempfile.mkdtemp())
        mr.save_review({'trade_date': '20260921', 'next_day_watch': {'items': [1]}}, d)
        http = lambda url: json.dumps(pool_payload([zt_row()])).encode() if 'push2ex' in url else b'{"result":null}'
        orig = mr.http_get
        mr.http_get = http
        try:
            r = mr.run(day='20260921', directory=d)
        finally:
            mr.http_get = orig
        self.assertEqual(r['next_day_watch'], {'items': [1]})
        self.assertEqual(mr.load_latest(d)['pools']['zt']['total'], 1)

    def test_run_keeps_existing_prev_watch(self):
        # watch_outcome.py 把上一交易日的兑现结果写进今天的文件后，17:30 的 market_review.run()
        # 不能把它冲掉——否则页面的「上一交易日兑现」会在 16:30→17:30 之间闪没。
        d = Path(tempfile.mkdtemp())
        mr.save_review({'trade_date': '20260921', 'prev_watch': {'date': '2026-09-20', 'items': [1]}}, d)
        http = lambda url: json.dumps(pool_payload([zt_row()])).encode() if 'push2ex' in url else b'{"result":null}'
        orig = mr.http_get
        mr.http_get = http
        try:
            r = mr.run(day='20260921', directory=d)
        finally:
            mr.http_get = orig
        self.assertEqual(r['prev_watch'], {'date': '2026-09-20', 'items': [1]})


if __name__ == '__main__':
    unittest.main()
