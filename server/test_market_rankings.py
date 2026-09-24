import json
import unittest
from datetime import datetime

import market_rankings as mr
from collect_quotes import CST


def payload(rows):
    return {'data': {'diff': rows}}


def sector(code, name, change, up, down, flat, flow_pct, flow=1e8):
    return {'f12': code, 'f14': name, 'f3': change, 'f62': flow, 'f184': flow_pct,
            'f104': up, 'f105': down, 'f106': flat, 'f2': 100}


def stock(code, name, flow):
    return {'f12': code, 'f14': name, 'f2': 10.5, 'f3': 2.5, 'f62': flow, 'f184': 3.2,
            'f104': 0, 'f105': 0, 'f106': 0}


class RankingTests(unittest.TestCase):
    def test_sector_score_uses_three_weighted_percentiles(self):
        rows = mr.parse_sectors(payload([
            sector('BK1', '强', 3, 8, 2, 0, 6),
            sector('BK2', '中', 1, 5, 5, 0, 1),
            sector('BK3', '弱', -2, 2, 8, 0, -5),
        ]), 'industry')
        ranked = mr.rank_sectors(rows, 2)
        self.assertEqual([x['name'] for x in ranked['strong']], ['强', '中'])
        self.assertEqual([x['strength'] for x in ranked['strong']], [100.0, 50.0])
        self.assertEqual(ranked['weak'][0]['strength'], 0.0)

    def test_ties_receive_the_same_average_percentile(self):
        rows = mr.parse_sectors(payload([
            sector('B1', '甲', 1, 5, 5, 0, 1), sector('B2', '乙', 1, 5, 5, 0, 1)
        ]), 'concept')
        ranked = mr.rank_sectors(rows)
        self.assertEqual([x['strength'] for x in ranked['strong']], [50.0, 50.0])

    def test_stock_parser_excludes_beijing_and_sorts_both_directions(self):
        rows = [stock('600001', '甲', 3e8), stock('000001', '乙', -2e8), stock('920001', '北', 9e8)]
        parsed = mr.parse_stocks(payload(rows), 'stocks')
        self.assertEqual([x['symbol'] for x in parsed], ['sh600001', 'sz000001'])

    def test_partial_failure_does_not_drop_other_rankings(self):
        boards = [sector('BK1', '板块', 1, 6, 4, 0, 2)]
        stocks = [stock('600001', '个股', 3e8)]
        def http(url):
            if 'm%3A90%2Bt%3A3' in url:
                raise OSError('concept down')
            return json.dumps(payload(boards if 'm%3A90' in url else stocks)).encode()
        out = mr.current_rankings(datetime(2026, 9, 24, 10, tzinfo=CST), http)
        self.assertIsNotNone(out['sectors']['industry'])
        self.assertIsNone(out['sectors']['concept'])
        self.assertIn('concept', out['errors'])
        self.assertEqual(out['stocks']['inflow'][0]['name'], '个股')


if __name__ == '__main__':
    unittest.main()
