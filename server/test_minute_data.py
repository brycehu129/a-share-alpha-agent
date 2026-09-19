import json
import unittest

import minute_data as md


def payload(rows, symbol='sh600519', date='20260918'):
    return json.dumps({'code': 0, 'msg': '', 'data': {symbol: {'data': {'date': date, 'data': rows}}}}).encode()


# 600519 真实收盘行（成交量单位：手）
HAND = ['0930 1262.99 113 14271787.32', '0931 1259.18 529 66704818.26', '0932 1261.44 913 115121045.89']
# 科创板 688061 真实收盘行（成交量单位：股）：按手算 VWAP=0.45，按股算 45.21
SHARE = ['0930 44.44 500000 22220000.00', '1500 45.37 2500240 113037080.00']


class UnitInferenceTests(unittest.TestCase):
    def test_main_board_volume_is_in_hands(self):
        unit, vwap = md.infer_volume_unit(1256.30, 1265.87, 24891, 3135865485.91)
        self.assertEqual(unit, 'hand')
        self.assertAlmostEqual(vwap, 1259.84, places=1)

    def test_star_board_volume_is_in_shares(self):
        """真实事故：sh688061 按"手"算出的 VWAP 是 0.452，价格却在 44–46——差了 100 倍。
        硬套"手"会让所有科创板票的"均价线上下"判断全错。"""
        unit, vwap = md.infer_volume_unit(44.44, 46.31, 2500240, 113037080.00)
        self.assertEqual(unit, 'share')
        self.assertAlmostEqual(vwap, 45.21, places=1)

    def test_neither_unit_fits_is_an_error_not_a_guess(self):
        with self.assertRaises(md.MinuteError):
            md.infer_volume_unit(10, 11, 1000, 999999999)   # 两种单位都对不上区间

    def test_zero_volume_cannot_infer(self):
        with self.assertRaises(md.MinuteError):
            md.infer_volume_unit(10, 11, 0, 0)


class ParseTests(unittest.TestCase):
    def test_hand_unit_is_normalised_to_shares(self):
        r = md.parse_minute(payload(HAND), 'sh600519')
        self.assertEqual(r['volume_unit'], 'hand')
        self.assertEqual(r['bars'][0]['cum_volume_shares'], 113 * 100)
        self.assertEqual(r['bars'][1]['minute_volume_shares'], (529 - 113) * 100)
        self.assertAlmostEqual(r['bars'][0]['vwap'], 14271787.32 / 11300, places=3)
        self.assertEqual(r['trade_date'], '2026-09-18')

    def test_share_unit_is_left_as_shares(self):
        r = md.parse_minute(payload(SHARE, 'sh688061'), 'sh688061')
        self.assertEqual(r['volume_unit'], 'share')
        self.assertAlmostEqual(r['vwap'], 113037080.00 / 2500240, places=3)   # ≈45.21

    def test_post_close_rows_are_dropped_and_counted(self):
        rows = HAND + ['1500 1257.12 24891 3135865485.91', '1501 1257.12 24891 3135865485.91',
                       '1530 1257.12 24891 3135865485.91']
        r = md.parse_minute(payload(rows), 'sh600519')
        self.assertEqual(r['last_time'], '1500')
        self.assertEqual(r['dropped_rows'], 2)
        self.assertTrue(r['complete'])

    def test_intraday_snapshot_is_not_complete(self):
        self.assertFalse(md.parse_minute(payload(HAND), 'sh600519')['complete'])

    def test_extremes_are_labelled_as_minute_closes(self):
        r = md.parse_minute(payload(HAND), 'sh600519')
        self.assertEqual((r['high_close'], r['low_close']), (1262.99, 1259.18))

    def test_rejects_non_increasing_time(self):
        with self.assertRaises(md.MinuteError):
            md.parse_minute(payload(HAND + ['0931 1260.00 1000 1.0']), 'sh600519')

    def test_rejects_cumulative_volume_going_backwards(self):
        with self.assertRaises(md.MinuteError):
            md.parse_minute(payload(['0930 1262.99 500 1', '0931 1259.18 100 2']), 'sh600519')

    def test_rejects_bad_rows_and_bad_envelope(self):
        for bad in (payload(['0930 abc 1 1']), payload(['0930 0 1 1']), payload(['9 1 1 1']),
                    payload([]), b'not json', json.dumps({'code': 1}).encode(),
                    payload(HAND, symbol='sz000001')):        # 请求 sh600519 但响应里是别的代码
            with self.assertRaises(md.MinuteError):
                md.parse_minute(bad, 'sh600519')

    def test_only_a_share_codes_are_fetchable(self):
        with self.assertRaises(md.MinuteError):
            md.fetch_minute('hkHSI')

    def test_is_today_guards_against_yesterdays_curve(self):
        """非交易日/盘前接口返回的是上一个交易日——别把昨天的走势当今天的。"""
        from datetime import datetime
        from collect_quotes import CST
        r = md.parse_minute(payload(HAND), 'sh600519')
        self.assertTrue(md.is_today(r, datetime(2026, 9, 18, 10, 0, tzinfo=CST)))
        self.assertFalse(md.is_today(r, datetime(2026, 9, 21, 10, 0, tzinfo=CST)))


if __name__ == '__main__':
    unittest.main()
