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


def stock(code, name, flow, industry='电子', concepts='芯片,人工智能,机器人'):
    return {'f12': code, 'f14': name, 'f2': 10.5, 'f3': 2.5, 'f62': flow, 'f184': 3.2,
            'f100': industry, 'f103': concepts, 'f104': 0, 'f105': 0, 'f106': 0}


class RankingTests(unittest.TestCase):
    def setUp(self):
        mr._ranking_cache.clear()

    def test_sector_score_uses_three_weighted_percentiles(self):
        rows = mr.parse_sectors(payload([
            sector('BK1', '强', 3, 8, 2, 0, 6),
            sector('BK2', '中', 1, 5, 5, 0, 1),
            sector('BK3', '弱', -2, 2, 8, 0, -5),
        ]), 'industry')
        ranked = mr.rank_sectors(rows, 2)
        self.assertEqual([x['name'] for x in ranked['strong']], ['强', '中'])
        self.assertEqual([x['strength'] for x in ranked['strong']], [100.0, 50.0])

    def test_weak_side_is_not_offered_because_one_page_cannot_support_it(self):
        """单页只有按 fid 排序的前 100 行，从中挑"最弱"得到的是"第 91–100 强"。
        与其给一个误导的答案，不如不提供——见 rank_sectors 的文档。"""
        rows = mr.parse_sectors(payload([sector('BK1', '甲', 3, 8, 2, 0, 6),
                                         sector('BK2', '乙', -2, 2, 8, 0, -5)]), 'industry')
        self.assertNotIn('weak', mr.rank_sectors(rows))

    def test_total_reports_the_interface_total_not_the_page_size(self):
        """接口自报 496 个板块、本页只回 2 个时，total 必须是 496。
        历史上这里上报本页行数，于是界面声称"共 100 个板块"而实际有 496 个。"""
        raw = {'data': {'diff': [sector('BK1', '甲', 3, 8, 2, 0, 6),
                                 sector('BK2', '乙', 1, 5, 5, 0, 1)], 'total': 496}}
        rows = mr.parse_sectors(raw, 'industry')
        ranked = mr.rank_sectors(rows, 2, total=mr._total(raw))
        self.assertEqual(ranked['total'], 496)
        self.assertEqual(ranked['fetched'], 2)

    def test_total_is_none_safe_and_never_fakes_a_number(self):
        self.assertIsNone(mr._total({'data': {'diff': []}}))
        self.assertIsNone(mr._total({'data': {'total': 'x'}}))

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
        self.assertEqual(parsed[0]['industry'], '电子')
        self.assertEqual(parsed[0]['concepts'], ['芯片', '人工智能', '机器人'])

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

    def test_recent_success_is_used_when_a_source_temporarily_returns_502(self):
        boards = [sector('BK1', '板块', 1, 6, 4, 0, 2)]
        stocks = [stock('600001', '个股', 3e8)]
        good = lambda url: json.dumps(payload(boards if 'm%3A90' in url else stocks)).encode()
        first = mr.current_rankings(datetime(2026, 9, 24, 10, tzinfo=CST), good, now_ts=100)
        self.assertFalse(first['stale'])
        second = mr.current_rankings(datetime(2026, 9, 24, 10, 1, tzinfo=CST),
                                     lambda _url: (_ for _ in ()).throw(OSError('502')), now_ts=160)
        self.assertEqual(set(second['stale']), {'industry', 'concept', 'inflow', 'outflow'})
        self.assertEqual(second['sectors']['industry']['strong'][0]['name'], '板块')
        expired = mr.current_rankings(datetime(2026, 9, 24, 10, 20, tzinfo=CST),
                                      lambda _url: (_ for _ in ()).throw(OSError('502')), now_ts=1001)
        self.assertIsNone(expired['sectors']['industry'])


if __name__ == '__main__':
    unittest.main()
