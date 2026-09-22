import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import market_review as mr
import next_day_watch as nw


def zt(symbol='sz000001', name='甲', boards=1, first='09:45:00', open_times=0, fund=5e7, cap=2e9, turn=8.0, industry='电力', **kw):
    return {'symbol': symbol, 'code': symbol[2:], 'name': name, 'price': 10.0, 'pct': 10.0, 'boards': boards,
            'first_seal': first, 'open_times': open_times, 'seal_fund': fund, 'float_cap': cap, 'turnover': turn,
            'industry': industry, **kw}


def qs(symbol='sz000101', name='强股', pct=8.0, industry='电力', turn=12.0, new_high=60, volume_ratio=2.3, **kw):
    return {'symbol': symbol, 'code': symbol[2:], 'name': name, 'price': 12.0, 'pct': pct, 'turnover': turn,
            'float_cap': 2e9, 'industry': industry, 'new_high': new_high, 'volume_ratio': volume_ratio, **kw}


def review(rows, lhb=None, **pools):
    return {'trade_date': '20260921', 'date': '2026-09-21', 'pools': {'zt': {'total': len(rows), 'rows': rows}, **pools},
            'lhb': {'rows': lhb or [], 'trade_date': '20260921', 'date': '2026-09-21'}}


class ScoringTests(unittest.TestCase):
    def score(self, row, counts=None, lhb=None):
        return nw.score_row(row, counts or {row['industry']: 1}, lhb)

    def test_early_sealed_never_opened_beats_late_broken(self):
        strong, _, reasons, _ = self.score(zt(first='09:31:00', open_times=0))
        weak, _, _, risks = self.score(zt(first='14:20:00', open_times=3))
        self.assertGreater(strong, weak)
        self.assertTrue(any('首封' in r for r in reasons))
        self.assertTrue(any('炸板 3 次' in r for r in risks))

    def test_high_board_is_flagged_not_rewarded(self):
        s5, _, _, risks = self.score(zt(boards=5))
        s3, _, _, _ = self.score(zt(boards=3))
        self.assertLess(s5, s3)
        self.assertTrue(any('高位接力' in r for r in risks))

    def test_sector_resonance_and_lhb(self):
        base, _, _, _ = self.score(zt(), {'电力': 1})
        res, tags, _, _ = self.score(zt(), {'电力': 9})
        self.assertGreater(res, base)
        self.assertIn('板块共振', tags)
        buy, tags, _, _ = self.score(zt(), lhb=1e8)
        sell, stags, _, risks = self.score(zt(), lhb=-2e7)
        self.assertGreater(buy, base)
        self.assertLess(sell, base)
        self.assertIn('龙虎榜净卖', stags)
        self.assertTrue(risks)

    def test_one_word_board_is_penalised_and_labelled(self):
        s, tags, _, risks = self.score(zt(boards=2, first='09:25:00', open_times=0, turn=0.6))
        self.assertIn('一字', tags)
        self.assertTrue(any('一字板' in r for r in risks))

    def test_score_is_clamped(self):
        s, *_ = self.score(zt(boards=3, first='09:25:00', fund=1e9, cap=1e9, turn=10), {'电力': 20}, 5e8)
        self.assertLessEqual(s, 100)
        self.assertGreaterEqual(nw.score_row(zt(boards=5, first='14:50:00', open_times=5, fund=1, turn=60), {'电力': 1}, -1e9)[0], 0)

    def test_missing_fields_are_neutral_not_fatal(self):
        row = zt(fund=None, cap=None, turn=None, first=None)
        s, tags, _, _ = self.score(row)
        self.assertIsInstance(s, int)


class RankTests(unittest.TestCase):
    def test_excludes_st_and_new_listings_and_sorts(self):
        rows = [zt('sz000001', '好股', boards=3, first='09:30:00'), zt('sz000002', '*ST某', boards=3),
                zt('sz000003', 'N新股', boards=1), zt('sz000004', '普通', boards=1, first='14:30:00', open_times=2)]
        out = nw.rank(review(rows))
        self.assertEqual([i['name'] for i in out['items']], ['好股', '普通'])
        self.assertEqual(out['candidates'], 2)

    def test_lhb_net_flows_into_items(self):
        out = nw.rank(review([zt()], lhb=[{'symbol': 'sz000001', 'net': 8e7}]))
        self.assertEqual(out['items'][0]['lhb_net'], 8e7)
        self.assertIn('龙虎榜净买', out['items'][0]['tags'])

    def test_no_limit_pool_gives_empty_with_note(self):
        out = nw.rank({'date': 'd', 'pools': {'zt': None, 'qs': None}})
        self.assertEqual(out['items'], [])
        self.assertIn('没有涨停池/强势股池', out['note'])

    def test_qs_pool_also_feeds_candidates(self):
        out = nw.rank(review([], qs={'total': 1, 'rows': [qs('sz000777', '强势样本')]}))
        self.assertEqual([i['name'] for i in out['items']], ['强势样本'])
        self.assertIn('强势股', out['items'][0]['tags'])
        self.assertIn('60 日新高', '；'.join(out['items'][0]['reasons']))

    def test_zt_and_qs_duplicate_symbol_is_merged(self):
        out = nw.rank(review([zt('sz000001', '甲', boards=2)], qs={'total': 1, 'rows': [qs('sz000001', '甲')]}))
        self.assertEqual(len(out['items']), 1)
        self.assertIn('zt', out['items'][0]['source_kinds'])
        self.assertIn('qs', out['items'][0]['source_kinds'])

    def test_sentiment_metrics(self):
        pools = {'zt': {'rows': [zt(boards=2, name='高'), zt(boards=1, name='低')]}, 'zb': {'rows': [zt()]},
                 'dt': {'rows': []}, 'yzt': {'rows': [{'pct': 4.0}, {'pct': -2.0}, {'pct': None}]}}
        s = nw.sentiment(pools)
        self.assertEqual((s['zt'], s['zb'], s['dt'], s['max_boards'], s['max_board_names']), (2, 1, 0, 2, ['高']))
        self.assertAlmostEqual(s['seal_rate'], 66.7, places=1)
        self.assertEqual((s['yzt_avg_pct'], s['yzt_down_pct']), (1.0, 50.0))
        empty = nw.sentiment({'zt': None})
        self.assertIsNone(empty['seal_rate'])
        self.assertIsNone(empty['max_boards'])


class AiTests(unittest.TestCase):
    def result(self):
        return nw.rank(review([zt('sz000001', '甲', boards=3), zt('sz000002', '乙', boards=2)]))

    def test_schema_uses_only_supported_keywords(self):
        import ai_analyst

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ('properties', '$defs'):
                        for sub in v.values():
                            walk(sub)
                    else:
                        self.assertIn(k, ai_analyst.SUPPORTED_KEYWORDS, k)
                        walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)
        walk(nw.SCHEMA)

    def test_apply_ai_drops_unknown_and_invalid_and_reports_missing(self):
        r = self.result()
        data = {'market_view': '情绪偏强', 'items': [
            {'symbol': 'sz000001', 'verdict': 'focus', 'view': 'v', 'plan': 'p', 'risk': 'r'},
            {'symbol': 'sz999999', 'verdict': 'focus', 'view': 'v', 'plan': 'p', 'risk': 'r'},
            {'symbol': 'sz000002', 'verdict': 'moon', 'view': 'v', 'plan': 'p', 'risk': 'r'}]}
        issues = nw.apply_ai(r, data)
        by = {i['symbol']: i for i in r['items']}
        self.assertEqual(by['sz000001']['ai']['verdict'], 'focus')
        self.assertNotIn('ai', by['sz000002'])
        self.assertEqual(r['ai']['market_view'], '情绪偏强')
        self.assertEqual(len(issues), 3)

    def test_analyze_degrades_when_ai_unavailable(self):
        import claude_client
        r = self.result()
        with patch('claude_client.complete_json', side_effect=claude_client.ClaudeError('unavailable', '没有 key')):
            meta = nw.analyze(r)
        self.assertEqual(meta['status'], 'unavailable')
        self.assertNotIn('ai', r['items'][0])

    def test_payload_has_no_price_fields_and_carries_sentiment(self):
        p = nw.build_payload(self.result())
        self.assertIn('sentiment', p)
        self.assertNotIn('price', p['stocks'][0])
        self.assertIn('seal_ratio_pct', p['stocks'][0])


class RunTests(unittest.TestCase):
    def test_run_writes_back_and_keeps_prior_ai_when_not_regenerating(self):
        d = Path(tempfile.mkdtemp())
        base = review([zt('sz000001', '甲', boards=3)])
        base.update(fetched_at='t', errors={})
        mr.save_review(base, d)
        first = nw.run(d, ai=False)
        self.assertIsNone(first['ai_meta'])
        # 模拟 17:30 的 AI 点评已落盘，再来一次 16:30 风格的不带 AI 的运行：点评不能丢
        saved = mr.load_latest(d)
        saved['next_day_watch']['items'][0]['ai'] = {'verdict': 'focus', 'view': 'v', 'plan': 'p', 'risk': 'r'}
        saved['next_day_watch']['ai'] = {'market_view': 'mv'}
        mr.save_review(saved, d)
        again = nw.run(d, ai=False)
        self.assertEqual(again['items'][0]['ai']['verdict'], 'focus')
        self.assertEqual(mr.load_latest(d)['next_day_watch']['ai']['market_view'], 'mv')

    def test_run_without_review_raises(self):
        with self.assertRaises(mr.ReviewError):
            nw.run(Path(tempfile.mkdtemp()) / 'none')


if __name__ == '__main__':
    unittest.main()
