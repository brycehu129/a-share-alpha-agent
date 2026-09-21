import unittest
from datetime import datetime

import live_quote
from collect_quotes import CST

NOW = datetime(2026, 9, 18, 16, 20, tzinfo=CST)

# 真实响应片段（字段数与下标按实测保留）。A股88个，港股78个，美股73个。
CN = ('1~贵州茅台~600519~1257.12~1266.98~1262.99~24891~12061~12829~1257.12'
      + '~0' * 19
      + '~~20260918161436~-9.86~-0.78~1265.88~1256.10~x~24891~313585~0.20~19.30~'
      + '~1265.88~1256.10~0.77~15715.03~15715.03~6.25~1393.68~1140.28~1.14'
      + '~0' * 38)
CN_INDEX = CN.replace('~贵州茅台~600519~', '~上证指数~000001~').replace('~1393.68~1140.28~', '~-1~-1~')
CN_SZ_STOCK = CN.replace('~贵州茅台~600519~', '~平安银行~000001~')
HK = ('100~恒生指数~HSI~24750.780~24604.290~24723.850~26651252~0~0~24750.780'
      + '~0' * 19
      + '~0.0~2026/09/18 18:31:31~146.490~0.60~24862.980~24703.920'
      + '~0' * 43)
US = ('200~道琼斯~.DJI~51682.64~51778.04~51826.78~858494006~0~0~51589.80'
      + '~0' * 19
      + '~~2026-09-18 17:52:27~-95.40~-0.18~51826.78~51497.47'
      + '~0' * 38)


def body(symbol, payload):
    return ('v_%s="%s";\n' % (symbol, payload)).encode('gb18030')


class MarketDetectionTests(unittest.TestCase):
    def test_prefix_decides_market(self):
        self.assertEqual(live_quote.market_of('sh600519'), 'cn')
        self.assertEqual(live_quote.market_of('sz399001'), 'cn')
        self.assertEqual(live_quote.market_of('hkHSI'), 'hk')
        self.assertEqual(live_quote.market_of('usDJI'), 'us')
        for bad in ('bj830799', '600519', 'sh60051', ''):
            with self.assertRaises(live_quote.QuoteError):
                live_quote.market_of(bad)

    def test_index_flag_separates_sh000001_from_sz000001(self):
        """sz000001 是平安银行，sh000001 是上证指数，前三位数字相同。"""
        index = live_quote.parse_one(CN_INDEX, 'sh000001', NOW)
        stock = live_quote.parse_one(CN_SZ_STOCK, 'sz000001', NOW)
        self.assertTrue(index['is_index'])
        self.assertFalse(stock['is_index'])


class ParseTests(unittest.TestCase):
    def test_a_share_extracts_named_fields(self):
        q = live_quote.parse_one(CN, 'sh600519', NOW)
        self.assertEqual(q['name'], '贵州茅台')
        self.assertEqual(q['last'], '1257.12')
        self.assertEqual(q['change_pct'], '-0.78')
        self.assertEqual(q['volume_ratio'], '1.14')
        self.assertEqual(q['turnover_pct'], '0.20')
        self.assertEqual(q['limit_up'], '1393.68')
        self.assertEqual(q['limit_down'], '1140.28')
        self.assertEqual(q['quote_date'], '2026-09-18')
        self.assertTrue(q['timezone_verified'])

    def test_order_flow_fields_are_extracted_and_missing_ones_stay_none(self):
        """内外盘/盘口字段：下标按实测 sz300458 核对（外盘 213103 + 内盘 189631 ≈ 总量 402733 手）。"""
        q = live_quote.parse_one(CN, 'sh600519', NOW)
        self.assertEqual(q['outer_vol'], '12061')
        self.assertEqual(q['inner_vol'], '12829')
        self.assertEqual(q['bid1_price'], '1257.12')
        fields = CN.split('~')
        fields[74] = '5.18'
        fields[50] = '221'
        q = live_quote.parse_one('~'.join(fields), 'sh600519', NOW)
        self.assertEqual(q['bid_ask_ratio'], '5.18')
        self.assertEqual(q['bid_ask_diff'], '221')
        fields[74] = ''
        q = live_quote.parse_one('~'.join(fields), 'sh600519', NOW)
        self.assertIsNone(q['bid_ask_ratio'])   # 缺失就是缺失，不是 0

    def test_index_limit_prices_of_minus_one_become_none(self):
        """指数没有涨跌停，源返回 -1；当成 -1 元的限价会让"距跌停"算出荒谬结果。"""
        q = live_quote.parse_one(CN_INDEX, 'sh000001', NOW)
        self.assertIsNone(q['limit_up'])
        self.assertIsNone(q['limit_down'])

    def test_hk_and_us_use_their_own_layouts(self):
        # 港股盘后仍在更新，实测时间戳晚于A股收盘；用各自合理的采集时刻。
        evening = datetime(2026, 9, 18, 19, 0, tzinfo=CST)
        hk = live_quote.parse_one(HK, 'hkHSI', evening)
        self.assertEqual(hk['name'], '恒生指数')
        self.assertEqual(hk['quote_date'], '2026-09-18')
        self.assertTrue(hk['timezone_verified'])
        self.assertNotIn('volume_ratio', hk)  # A股专有字段不该出现在港股上

        us = live_quote.parse_one(US, 'usDJI', evening)
        self.assertEqual(us['quote_date'], '2026-09-18')
        self.assertIsNone(us['quote_at'])      # 时区未核验，不给带时区的时间
        self.assertIsNone(us['age_seconds'])   # 因此也不算延迟
        self.assertFalse(us['timezone_verified'])

    def test_rejects_short_field_list(self):
        with self.assertRaises(live_quote.QuoteError):
            live_quote.parse_one('1~名~600519~10~10~10', 'sh600519', NOW)

    def test_rejects_mismatched_code(self):
        with self.assertRaises(live_quote.QuoteError):
            live_quote.parse_one(CN, 'sh600520', NOW)

    def test_rejects_non_positive_price(self):
        with self.assertRaises(live_quote.QuoteError):
            live_quote.parse_one(CN.replace('~1257.12~1266.98~', '~0~1266.98~', 1), 'sh600519', NOW)

    def test_rejects_future_quote_time(self):
        past = datetime(2026, 9, 18, 15, 0, tzinfo=CST)
        with self.assertRaises(live_quote.QuoteError):
            live_quote.parse_one(CN, 'sh600519', past)

    def test_age_is_measured_against_fetch_time(self):
        q = live_quote.parse_one(CN, 'sh600519', NOW)
        self.assertEqual(q['age_seconds'], round((NOW - datetime(
            2026, 9, 18, 16, 14, 36, tzinfo=CST)).total_seconds()))


class BatchTests(unittest.TestCase):
    def test_one_bad_symbol_does_not_lose_the_rest(self):
        raw = body('sh600519', CN) + body('hkHSI', 'broken~payload')
        quotes, failures = live_quote.parse_batch(raw, ['sh600519', 'hkHSI'], NOW)
        self.assertEqual([q['symbol'] for q in quotes], ['sh600519'])
        self.assertEqual(failures[0]['symbol'], 'hkHSI')

    def test_missing_symbol_is_reported_not_silently_dropped(self):
        quotes, failures = live_quote.parse_batch(body('sh600519', CN),
                                                  ['sh600519', 'sz000002'], NOW)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(failures, [{'symbol': 'sz000002', 'reason': '响应中缺少该代码'}])

    def test_duplicate_symbol_in_response_is_rejected(self):
        with self.assertRaises(live_quote.QuoteError):
            live_quote.parse_batch(body('sh600519', CN) * 2, ['sh600519'], NOW)


class SessionTests(unittest.TestCase):
    def test_clock_sessions(self):
        cases = {(9, 0): 'pre_open', (9, 20): 'call_auction', (10, 0): 'morning',
                 (12, 0): 'lunch_break', (14, 0): 'afternoon', (15, 10): 'closing',
                 (16, 30): 'post_close'}
        for (hour, minute), expected in cases.items():
            # 2026-09-18 是周五
            self.assertEqual(live_quote.clock_session(
                datetime(2026, 9, 18, hour, minute, tzinfo=CST)), expected)

    def test_weekend_is_flagged_without_claiming_holiday_knowledge(self):
        self.assertEqual(live_quote.clock_session(
            datetime(2026, 9, 19, 10, 0, tzinfo=CST)), 'weekend')


if __name__ == '__main__':
    unittest.main()
