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

    def test_first_board_one_word_is_now_detected(self):
        # rules-1 的 bug：要求 boards>=2，首板一字完全漏判。rules-2 去掉了这个条件。
        row = zt(boards=1, first='09:25:00', open_times=0, turn=0.8)
        self.assertTrue(nw._is_one_word(row))
        _, tags, _, _ = self.score(row)
        self.assertIn('一字', tags)

    def test_score_is_clamped(self):
        s, *_ = self.score(zt(boards=3, first='09:25:00', fund=1e9, cap=1e9, turn=10), {'电力': 20}, 5e8)
        self.assertLessEqual(s, 100)
        self.assertGreaterEqual(nw.score_row(zt(boards=5, first='14:50:00', open_times=5, fund=1, turn=60), {'电力': 1}, -1e9)[0], 0)

    def test_missing_fields_are_neutral_not_fatal(self):
        row = zt(fund=None, cap=None, turn=None, first=None)
        s, tags, _, _ = self.score(row)
        self.assertIsInstance(s, int)

    def test_cooling_phase_halves_board_bonus(self):
        steady, _, reasons_s, _ = nw.score_row(zt(boards=3), {'电力': 1}, None, 'steady')
        cooling, _, reasons_c, _ = nw.score_row(zt(boards=3), {'电力': 1}, None, 'cooling')
        self.assertLess(cooling, steady)
        self.assertTrue(any('情绪退潮已折半' in r for r in reasons_c))
        self.assertFalse(any('情绪退潮已折半' in r for r in reasons_s))


class PositionTests(unittest.TestCase):
    def bars(self, closes, ma20=None, ma60=None):
        out = [{'close': c} for c in closes]
        out[-1]['ma20'] = ma20
        out[-1]['ma60'] = ma60
        return out

    def test_too_few_bars_gives_no_position(self):
        self.assertIsNone(nw.position(self.bars([10.0] * 10)))

    def test_run_up_and_bias_and_below_ma60(self):
        closes = [10.0] * 50 + [20.0]  # 100% 的 10 日涨幅，远高于 ma20
        pos = nw.position(self.bars(closes, ma20=15.0, ma60=25.0))
        self.assertAlmostEqual(pos['run_up_10'], 100.0)
        self.assertTrue(pos['ma20_bias'] > 0)
        self.assertTrue(pos['below_ma60'])
        self.assertTrue(pos['new_high_60'])

    def test_score_position_high_run_up_is_risk_not_bonus(self):
        pos = {'run_up_10': 90.0, 'ma20_bias': 40.0, 'run_up_20': 90.0, 'new_high_60': True, 'below_ma60': False}
        delta, reasons, risks, tags = nw.score_position(pos)
        self.assertLess(delta, 0)
        self.assertTrue(risks)
        self.assertFalse(reasons)

    def test_score_position_calm_start_and_fresh_break_are_bonus(self):
        pos = {'run_up_10': 12.0, 'ma20_bias': 5.0, 'run_up_20': 15.0, 'new_high_60': True, 'below_ma60': False}
        delta, reasons, risks, tags = nw.score_position(pos)
        self.assertGreater(delta, 0)
        self.assertIn('平台突破', tags)

    def test_missing_position_is_neutral_and_labelled(self):
        delta, reasons, risks, tags = nw.score_position(None)
        self.assertEqual(delta, 0)
        self.assertEqual(tags, ['位置未知'])


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

    def test_high_board_goes_to_high_bucket_not_main_board(self):
        rows = [zt('sz000001', '龙头', boards=5, first='09:30:00'),
                zt('sz000002', '普通', boards=2, first='09:31:00')]
        out = nw.rank(review(rows))
        by = {i['name']: i for i in out['items']}
        self.assertEqual(by['龙头']['bucket'], 'high')
        self.assertEqual(by['普通']['bucket'], 'core')

    def test_one_word_goes_to_unbuyable_bucket_not_main_board(self):
        rows = [zt('sz000001', '一字股', boards=2, first='09:25:00', open_times=0, turn=0.5),
                zt('sz000002', '普通', boards=2, first='09:31:00')]
        out = nw.rank(review(rows))
        by = {i['name']: i for i in out['items']}
        self.assertEqual(by['一字股']['bucket'], 'unbuyable')
        self.assertEqual(by['普通']['bucket'], 'core')

    def test_position_fn_only_scores_run_up_when_supplied(self):
        rows = [zt('sz000001', '高位股', boards=2, first='09:31:00')]
        fake_positions = lambda symbols, http=None: {'sz000001': {'run_up_10': 90.0, 'ma20_bias': None,
                                                                    'run_up_20': None, 'new_high_60': False,
                                                                    'below_ma60': False}}
        out = nw.rank(review(rows), position_fn=fake_positions)
        it = out['items'][0]
        self.assertEqual(it['bucket'], 'high')  # 10 日涨幅 90% 超过 high_run_up_10 阈值
        self.assertTrue(any('位置偏高' in r for r in it['risks']))

    def test_no_position_fn_marks_position_unknown(self):
        out = nw.rank(review([zt()]))
        self.assertIn('位置未知', out['items'][0]['tags'])
        self.assertIsNone(out['items'][0]['position'])

    def test_cooling_phase_shrinks_high_and_core_quota(self):
        rows = [zt('sz%06d' % i, '股%d' % i, boards=2, first='09:%02d:00' % (30 + i)) for i in range(10)]
        rows += [zt('sz900001', '高位1', boards=5), zt('sz900002', '高位2', boards=5), zt('sz900003', '高位3', boards=5), zt('sz900004', '高位4', boards=5)]
        pools = {'zt': {'total': len(rows), 'rows': rows}, 'dt': {'rows': [{}] * 25}, 'zb': {'rows': []}}
        out = nw.rank({'trade_date': '20260921', 'date': '2026-09-21', 'pools': pools, 'lhb': {'rows': []}})
        self.assertEqual(out['phase'], 'cooling')
        self.assertLessEqual(out['buckets']['core'], 5)
        self.assertLessEqual(out['buckets']['high'], 1)
        self.assertIn('退潮', out['note'])

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
        first = nw.run(d, ai=False, position_fn=None)
        self.assertIsNone(first['ai_meta'])
        # 模拟 17:30 的 AI 点评已落盘，再来一次 16:30 风格的不带 AI 的运行：点评不能丢
        saved = mr.load_latest(d)
        saved['next_day_watch']['items'][0]['ai'] = {'verdict': 'focus', 'view': 'v', 'plan': 'p', 'risk': 'r'}
        saved['next_day_watch']['ai'] = {'market_view': 'mv'}
        mr.save_review(saved, d)
        again = nw.run(d, ai=False, position_fn=None)
        self.assertEqual(again['items'][0]['ai']['verdict'], 'focus')
        self.assertEqual(mr.load_latest(d)['next_day_watch']['ai']['market_view'], 'mv')

    def test_run_without_review_raises(self):
        with self.assertRaises(mr.ReviewError):
            nw.run(Path(tempfile.mkdtemp()) / 'none', position_fn=None)

    def test_run_never_touches_network_when_position_fn_is_none(self):
        # main() 里 --no-position 用的就是这条路径；这里保证它确实不会调 fetch_positions。
        d = Path(tempfile.mkdtemp())
        base = review([zt('sz000001', '甲', boards=3)])
        base.update(fetched_at='t', errors={})
        mr.save_review(base, d)
        with patch('next_day_watch.fetch_positions', side_effect=AssertionError('不应该被调用')):
            nw.run(d, ai=False, position_fn=None)


if __name__ == '__main__':
    unittest.main()
