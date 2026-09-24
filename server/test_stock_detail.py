import json
import unittest

import market_review as mr
import stock_detail as sd


def kline(rows, symbol='sh600721'):
    return {'data': {symbol: {'qfqday': rows}}}


class KlineTests(unittest.TestCase):
    def test_moving_average_needs_full_window(self):
        self.assertEqual(sd.moving_average([1, 2, 3, 4], 3), [None, None, 2.0, 3.0])
        self.assertEqual(sd.moving_average([5], 5), [None])

    def test_parse_kline_drops_bad_rows_and_computes_ma(self):
        rows = [['2026-09-%02d' % d, '10', '11', '12', '9', '100'] for d in range(1, 8)]
        rows.insert(3, ['2026-09-02', '1', '1', '1', '1', '1'])            # 重复日期
        rows.insert(2, ['2026-09-03', '10', '20', '12', '9', '5'])         # 收盘价超出最高价
        bars = sd.parse_kline(kline(rows), 'sh600721')
        self.assertEqual([b['date'] for b in bars], ['2026-09-0%d' % d for d in range(1, 8)])
        self.assertIsNone(bars[3]['ma5'])
        self.assertEqual(bars[4]['ma5'], 11.0)
        self.assertIsNone(bars[-1]['ma60'])

    def test_parse_kline_keeps_last_show_bars(self):
        rows = [['2026-%02d-%02d' % (m, d), '10', '11', '12', '9', '1'] for m in range(1, 8) for d in range(1, 29)]
        bars = sd.parse_kline(kline(rows), 'sh600721')
        self.assertEqual(len(bars), sd.SHOW_BARS)
        self.assertIsNotNone(bars[0]['ma60'])                              # 展示区间最左边的均线也有值

    def test_parse_kline_too_short_or_malformed(self):
        for bad in ({}, kline([]), kline([['2026-09-01', '1', '1', '1', '1', '1']])):
            with self.assertRaises(mr.ReviewError):
                sd.parse_kline(bad, 'sh600721')


class ProfileTests(unittest.TestCase):
    def test_parse_profile(self):
        p = sd.parse_profile({'result': {'data': [{'ORG_NAME': '新疆百花村', 'EM2016': '医药生物-医疗服务-医疗服务',
                                                   'BLGAINIAN': '西部大开发,CRO，创新药', 'REGIONBK': '新疆', 'ORG_WEB': 'w'}]}})
        self.assertEqual(p['industry_path'], ['医药生物', '医疗服务', '医疗服务'])
        self.assertEqual(p['concepts'], ['西部大开发', 'CRO', '创新药'])
        with self.assertRaises(mr.ReviewError):
            sd.parse_profile({'result': None})

    def test_secucode(self):
        self.assertEqual(sd.secucode('sh600721'), '600721.SH')
        self.assertEqual(sd.secucode('bj920298'), '920298.BJ')
        with self.assertRaises(mr.ReviewError):
            sd.secucode('600721')


class DetailTests(unittest.TestCase):
    review = {'trade_date': '20260921', 'date': '2026-09-21',
              'pools': {'zt': {'total': 1, 'rows': [{'symbol': 'sh600721', 'boards': 1}]}, 'dt': None},
              'lhb': {'rows': [{'symbol': 'sh600721', 'net': 1.0}], 'trade_date': '20260921', 'date': '2026-09-21'},
              'next_day_watch': {'items': [{'symbol': 'sh600721', 'score': 70}]}}

    def test_review_context(self):
        ctx = sd.review_context('sh600721', self.review)
        self.assertEqual(ctx['pools']['zt']['boards'], 1)
        self.assertEqual(ctx['lhb']['net'], 1.0)
        self.assertEqual(ctx['watch']['score'], 70)
        self.assertIsNone(ctx['prev_watch'])
        self.assertIsNone(sd.review_context('sh600721', None))
        self.assertEqual(sd.review_context('sz000001', self.review)['pools'], {})

    def test_review_context_carries_prev_watch_outcome_for_matching_symbol(self):
        review = dict(self.review, prev_watch={'date': '2026-09-20', 'items': [
            {'symbol': 'sh600721', 'score': 81, 'outcome': {'result': 'limit_up', 'verdict': 'hit'}}]})
        ctx = sd.review_context('sh600721', review)
        self.assertEqual(ctx['prev_watch']['date'], '2026-09-20')
        self.assertEqual(ctx['prev_watch']['item']['outcome']['verdict'], 'hit')
        self.assertIsNone(sd.review_context('sz000001', review)['prev_watch'])

    def test_detail_isolates_failures_and_fetches_seats_only_for_lhb_stocks(self):
        sd._cache.clear()
        urls = []

        def http(url):
            urls.append(url)
            if 'fqkline' in url:
                raise OSError('down')
            if 'RPT_F10' in url:
                return json.dumps({'result': {'data': [{'ORG_NAME': 'x', 'EM2016': 'a-b'}]}}).encode()
            return b'{"result":null}'
        snap = {'quotes': [{'name': '甲', 'last': '13.15', 'change_pct': '10.04', 'previous_close': '11.95'}]}
        out = sd.detail('sh600721', self.review, http=http, snapshot=snap, force=True)
        self.assertIsNone(out['kline'])
        self.assertIn('kline', out['errors'])
        self.assertEqual(out['quote']['last'], '13.15')
        self.assertEqual(out['profile']['full_name'], 'x')
        self.assertEqual(out['seats'], [])
        self.assertTrue(any('DAILYDETAILSBUY' in u for u in urls))
        urls.clear()
        out2 = sd.detail('sz000001', self.review, http=http, snapshot=snap, force=True)
        self.assertNotIn('seats', out2)
        self.assertFalse(any('DAILYDETAILS' in u for u in urls))
        sd._cache.clear()

    def test_bad_symbol_raises(self):
        with self.assertRaises(mr.ReviewError):
            sd.detail('nonsense')


if __name__ == '__main__':
    unittest.main()
