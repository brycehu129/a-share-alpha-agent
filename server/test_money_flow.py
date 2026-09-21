import json
import unittest
from datetime import datetime

import money_flow as mf
from collect_quotes import CST

NOW = datetime(2026, 9, 21, 15, 5, tzinfo=CST)


def response(lines):
    return json.dumps({'rc': 0, 'data': {'code': '300458', 'market': 0, 'name': '全志科技', 'klines': lines}}).encode()


# 第一行取自 2026-09-21 sz300458 的真实响应：主力 = 大单 + 超大单（-1116177 + 813839 = -302338）。
REAL_FIRST = '2026-09-21 09:31,-302338.0,2458076.0,-2155738.0,-1116177.0,813839.0'
REAL_LAST = '2026-09-21 15:00,-224507630.0,282523772.0,-58016118.0,-148816241.0,-75691389.0'


def series(mains, day='2026-09-21'):
    """按分钟造一条只有主力在变的序列（其余档位固定）。"""
    out = []
    for i, m in enumerate(mains):
        h, mi = divmod(9 * 60 + 31 + i, 60)
        out.append('%s %02d:%02d,%s,0,0,%s,0' % (day, h, mi, float(m), float(m)))
    return out


class ParseTests(unittest.TestCase):
    def test_real_shaped_rows_parse_and_main_equals_large_plus_xlarge(self):
        f = mf.parse_flow(response([REAL_FIRST, REAL_LAST]), 'sz300458', NOW)
        self.assertEqual(f['trade_date'], '2026-09-21')
        self.assertEqual(f['as_of'], '1500')
        self.assertEqual(f['main'], -224507630.0)
        self.assertAlmostEqual(f['large'] + f['xlarge'], f['main'], places=0)
        self.assertEqual(f['rows'][0]['t'], '0931')

    def test_bad_structure_and_rows_are_rejected_not_guessed(self):
        for raw in (b'not json', json.dumps({'data': None}).encode(), json.dumps({'data': {'klines': 'x'}}).encode()):
            with self.assertRaises(mf.FlowError):
                mf.parse_flow(raw, 'sz300458', NOW)
        with self.assertRaises(mf.FlowError):
            mf.parse_flow(response(['2026-09-21 09:31,1,2']), 'sz300458', NOW)
        with self.assertRaises(mf.FlowError):
            mf.parse_flow(response([REAL_FIRST, '2026-09-22 09:31,1,2,3,4,5']), 'sz300458', NOW)

    def test_pre_open_dash_rows_are_skipped_and_empty_is_an_error(self):
        f = mf.parse_flow(response(['2026-09-21 09:30,-,-,-,-,-', REAL_FIRST]), 'sz300458', NOW)
        self.assertEqual(len(f['rows']), 1)
        with self.assertRaises(mf.FlowError):
            mf.parse_flow(response(['2026-09-21 09:30,-,-,-,-,-']), 'sz300458', NOW)


class DeriveTests(unittest.TestCase):
    def test_window_change_peak_and_flip(self):
        # 先流入到 100，再一路流出到 -50：峰值 100，转负点在 -0 之后的第一行
        mains = list(range(0, 101, 10)) + list(range(90, -60, -10))
        f = mf.parse_flow(response(series(mains)), 'sz300458', NOW)
        self.assertEqual(f['main'], -50)
        self.assertEqual(f['peak_main'], 100)
        self.assertEqual(f['from_peak'], -150)
        self.assertEqual(f['main_5m'], -50 - mains[-1 - 5])
        self.assertEqual(f['flip']['from'], '净流入')
        self.assertEqual(f['flip']['to'], '净流出')
        # 转向时刻 = 最后一个为正的分钟之后的那一分钟（这里恰好是穿过 0 的那一行）
        last_pos = max(i for i, m in enumerate(mains) if m > 0)
        self.assertEqual(f['flip']['at'], f['rows'][last_pos + 1]['t'])

    def test_short_series_uses_zero_as_the_base(self):
        f = mf.parse_flow(response(series([10, 20, 30])), 'sz300458', NOW)
        self.assertEqual(f['main_5m'], 30)      # 不足 5 分钟：相对开盘的 0
        self.assertIsNone(f['flip'])            # 一直为正，没有转向


class FetchTests(unittest.TestCase):
    def test_secid_prefix_and_rejects_non_stock_codes(self):
        self.assertEqual(mf.secid('sh600519'), '1.600519')
        self.assertEqual(mf.secid('sz300458'), '0.300458')
        for bad in ('hkHSI', '600519', 'bj830799', ''):
            with self.assertRaises(mf.FlowError):
                mf.secid(bad)

    def test_fetch_uses_short_timeout_and_reports_failures_as_flow_errors(self):
        seen = {}

        def ok(url, timeout):
            seen.update(url=url, timeout=timeout)
            return response([REAL_FIRST])
        f = mf.fetch_flow('sz300458', NOW, http_get=ok)
        self.assertIn('secid=0.300458', seen['url'])
        self.assertEqual(seen['timeout'], mf.DEFAULT_TIMEOUT_S)
        self.assertEqual(f['symbol'], 'sz300458')

        def down(url, timeout):
            raise OSError('boom')
        with self.assertRaises(mf.FlowError) as ctx:
            mf.fetch_flow('sz300458', NOW, http_get=down)
        self.assertIn('OSError', str(ctx.exception))

    def test_yesterdays_data_is_not_todays_flow(self):
        old = response(['2026-09-18 09:31,1,2,3,4,5'])
        with self.assertRaises(mf.FlowError) as ctx:
            mf.fetch_flow('sz300458', NOW, http_get=lambda u, t: old)
        self.assertIn('不是今天', str(ctx.exception))


class DisplayTests(unittest.TestCase):
    def test_money_formatting(self):
        self.assertEqual(mf.fmt_money(-224507630.0), '-2.25亿')
        self.assertEqual(mf.fmt_money(56_000_000), '+5600万')
        self.assertEqual(mf.fmt_money(0), '0万')
        self.assertEqual(mf.fmt_money(None), '—')

    def test_book_facts_from_quote_and_missing_fields_are_omitted(self):
        b = mf.book_facts({'outer_vol': '213103', 'inner_vol': '189631', 'bid_ask_ratio': '5.18'})
        self.assertEqual(b, {'outer_pct': 52.9, 'bid_ask_ratio': 5.18})
        self.assertEqual(mf.book_facts({'outer_vol': None, 'inner_vol': '5'}), {})
        self.assertEqual(mf.book_facts({'outer_vol': '0', 'inner_vol': '0'}), {})   # 没成交：不算 0%

    def test_flow_line_degrades_and_never_prints_zero_for_missing(self):
        flow = mf.parse_flow(response([REAL_FIRST, REAL_LAST]), 'sz300458', NOW)
        line = mf.flow_line(flow, {'outer_pct': 52.9, 'bid_ask_ratio': 5.18})
        self.assertTrue(line.startswith('资金｜主力 -2.25亿（近30分钟'))
        self.assertIn('大单 -1.49亿', line)
        self.assertIn('外盘占比 52.9%', line)
        self.assertIn('委比 +5.2%', line)
        self.assertEqual(mf.flow_line(None, {'outer_pct': 52.9}), '资金｜外盘占比 52.9%')
        self.assertEqual(mf.flow_line(None, {}), '')
        self.assertEqual(mf.flow_line(None, None), '')

    def test_half_hour_table_keeps_the_last_row(self):
        f = mf.parse_flow(response(series(list(range(75)))), 'sz300458', NOW)
        table = mf.half_hour_table(f['rows'])
        self.assertEqual([r['main'] for r in table], [29.0, 59.0, 74.0])
        self.assertEqual(table[0]['t'], '10:00')
        self.assertEqual(mf.half_hour_table([]), [])

    def test_compact_drops_the_series(self):
        f = mf.parse_flow(response([REAL_FIRST, REAL_LAST]), 'sz300458', NOW)
        c = mf.compact(f)
        self.assertNotIn('rows', c)
        self.assertEqual(c['main'], -224507630.0)
        self.assertIsNone(mf.compact(None))


if __name__ == '__main__':
    unittest.main()
