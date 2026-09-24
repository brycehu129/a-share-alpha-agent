import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import market_review as mr
import next_day_watch as nw
import watch_outcome as wo


def pool_row(symbol='sz000001', name='甲', boards=2, first_seal='09:31:00', open_times=0, turnover=8.0, pct=10.0, **kw):
    return {'symbol': symbol, 'code': symbol[2:], 'name': name, 'boards': boards, 'first_seal': first_seal,
            'open_times': open_times, 'turnover': turnover, 'pct': pct, **kw}


def quote(symbol='sz000001', last=11.0, previous_close=10.0, open_=10.5, high=11.5, low=10.2, turnover_pct=9.0):
    return {'symbol': symbol, 'last': str(last), 'previous_close': str(previous_close), 'open': str(open_),
            'high': str(high), 'low': str(low), 'turnover_pct': str(turnover_pct)}


def watch_item(symbol='sz000001', name='甲', score=81, bucket='core', ai=None):
    it = {'symbol': symbol, 'name': name, 'score': score, 'bucket': bucket, 'boards': 2,
          'tags': ['2连板'], 'reasons': ['2 板（+12）'], 'risks': []}
    if ai:
        it['ai'] = ai
    return it


def make_review(trade_date, date, items=None, pools=None):
    out = {'trade_date': trade_date, 'date': date, 'fetched_at': 't', 'errors': {},
           'pools': pools or {}, 'lhb': {'rows': [], 'trade_date': trade_date, 'date': date}}
    if items is not None:
        out['next_day_watch'] = {'rules_version': nw.RULES_VERSION, 'date': date, 'items': items,
                                 'sentiment': {}, 'candidates': len(items)}
    return out


class ClassifyTests(unittest.TestCase):
    def test_limit_up(self):
        pools = {'zt': {'rows': [pool_row(first_seal='10:00:00', turnover=8.0)]}}
        result, row = wo.classify_pool('sz000001', pools)
        self.assertEqual(result, 'limit_up')
        self.assertIsNotNone(row)

    def test_one_word(self):
        pools = {'zt': {'rows': [pool_row(first_seal='09:25:00', open_times=0, turnover=0.5)]}}
        result, row = wo.classify_pool('sz000001', pools)
        self.assertEqual(result, 'one_word')

    def test_broke(self):
        pools = {'zt': {'rows': []}, 'zb': {'rows': [pool_row()]}}
        result, row = wo.classify_pool('sz000001', pools)
        self.assertEqual(result, 'broke')

    def test_limit_down(self):
        pools = {'zt': {'rows': []}, 'dt': {'rows': [pool_row()]}}
        result, row = wo.classify_pool('sz000001', pools)
        self.assertEqual(result, 'limit_down')

    def test_not_found_returns_none(self):
        result, row = wo.classify_pool('sz000001', {'zt': {'rows': []}})
        self.assertIsNone(result)
        self.assertIsNone(row)


class OutcomeTests(unittest.TestCase):
    def test_one_word_is_unbuyable_regardless_of_price_action(self):
        pools = {'zt': {'rows': [pool_row(first_seal='09:25:00', open_times=0, turnover=0.4)]}}
        out = wo.build_outcome('sz000001', pools, quote(last=12.0, open_=12.0), None)
        self.assertEqual(out['result'], 'one_word')
        self.assertEqual(out['verdict'], 'unbuyable')

    def test_close_vs_open_hit(self):
        pools = {'zt': {'rows': []}}
        out = wo.build_outcome('sz000001', pools, quote(last=11.5, open_=10.5), None)
        self.assertAlmostEqual(out['close_vs_open'], round((11.5 / 10.5 - 1) * 100, 2))
        self.assertEqual(out['verdict'], 'hit')

    def test_close_vs_open_miss(self):
        pools = {'zt': {'rows': []}}
        out = wo.build_outcome('sz000001', pools, quote(last=9.8, previous_close=10.0, open_=10.5, high=10.6, low=9.7), None)
        self.assertLess(out['close_vs_open'], -3)
        self.assertEqual(out['verdict'], 'miss')

    def test_close_vs_open_flat(self):
        pools = {'zt': {'rows': []}}
        out = wo.build_outcome('sz000001', pools, quote(last=10.4, previous_close=10.0, open_=10.5, high=10.6, low=10.3), None)
        self.assertEqual(out['verdict'], 'flat')

    def test_close_pct_kept_as_separate_reference(self):
        pools = {'zt': {'rows': []}}
        out = wo.build_outcome('sz000001', pools, quote(last=11.0, previous_close=10.0, open_=10.5), None)
        self.assertAlmostEqual(out['close_pct'], 10.0)
        self.assertNotEqual(out['close_pct'], out['close_vs_open'])

    def test_no_pool_and_no_quote_is_unknown(self):
        out = wo.build_outcome('bj800001', {'zt': {'rows': []}}, None, None)
        self.assertTrue(out['quote_missing'])
        self.assertEqual(out['verdict'], 'unknown')
        self.assertIsNone(out['result'])

    def test_pool_pct_used_when_no_quote(self):
        pools = {'zt': {'rows': [pool_row(first_seal='10:00:00', turnover=8.0, pct=9.9)]}}
        out = wo.build_outcome('sz000001', pools, None, None)
        self.assertEqual(out['close_pct'], 9.9)
        self.assertEqual(out['result'], 'limit_up')
        # 没有开盘价数据，close_vs_open 拿不到，只能是 unknown（不是 hit/miss 的规则分母）
        self.assertEqual(out['verdict'], 'unknown')

    def test_minute_path_carried_through(self):
        out = wo.build_outcome('sz000001', {'zt': {'rows': []}}, quote(), '09:31 冲高 +7%，尾盘 +1%')
        self.assertEqual(out['path'], '09:31 冲高 +7%，尾盘 +1%')


class FetchTests(unittest.TestCase):
    def test_fetch_quotes_skips_unsupported_markets_without_calling_snapshot(self):
        with patch('live_quote.snapshot') as m:
            out = wo.fetch_quotes(['bj800001'])
        m.assert_not_called()
        self.assertEqual(out, {})

    def test_fetch_quotes_maps_by_symbol(self):
        with patch('live_quote.snapshot', return_value={'quotes': [quote('sz000001'), quote('sh600000')]}):
            out = wo.fetch_quotes(['sz000001', 'sh600000'])
        self.assertEqual(set(out), {'sz000001', 'sh600000'})

    def test_fetch_minute_paths_one_failure_does_not_break_others(self):
        def fake_fetch(symbol, now=None):
            if symbol == 'sz000002':
                raise Exception('boom')
            return {'trade_date': '2026-09-22', 'bars': [{'t': '0931', 'price': 11.0}, {'t': '1500', 'price': 10.5}],
                    'open': 10.0, 'last': 10.5}

        with patch('minute_data.fetch_minute', side_effect=fake_fetch), \
             patch('minute_data.is_today', return_value=True):
            out = wo.fetch_minute_paths(['sz000001', 'sz000002'])
        self.assertIn('sz000001', out)
        self.assertNotIn('sz000002', out)

    def test_fetch_minute_paths_skips_unsupported_markets(self):
        with patch('minute_data.fetch_minute') as m:
            out = wo.fetch_minute_paths(['bj800001'])
        m.assert_not_called()
        self.assertEqual(out, {})


class SchemaTests(unittest.TestCase):
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
        walk(wo.SCHEMA)


class SettleTests(unittest.TestCase):
    def prior(self):
        items = [watch_item('sz000001', '甲', ai={'verdict': 'focus', 'view': 'v', 'plan': 'p', 'risk': 'r'}),
                 watch_item('sz000002', '乙')]
        return make_review('20260921', '2026-09-21', items=items)

    def test_settle_without_ai_fills_outcome_and_summary(self):
        today_pools = {'zt': {'rows': [pool_row('sz000001', '甲', first_seal='09:31:00')]}, 'zb': {'rows': []}, 'dt': {'rows': []}}
        with patch.object(wo, 'fetch_quotes', return_value={'sz000001': quote('sz000001', last=11.0, open_=10.5)}), \
             patch.object(wo, 'fetch_minute_paths', return_value={}):
            result = wo.settle(self.prior(), today_pools, ai=False)
        self.assertEqual(len(result['items']), 2)
        self.assertIn('outcome', result['items'][0])
        self.assertEqual(result['summary']['n'], 2)
        self.assertEqual(result['ai_meta']['status'], 'skipped')

    def test_settle_with_ai_success_attaches_review(self):
        today_pools = {'zt': {'rows': []}, 'zb': {'rows': []}, 'dt': {'rows': []}}
        ai_data = {'market_view': '整体偏强', 'items': [{'symbol': 'sz000001', 'review': '接得住'},
                                                       {'symbol': 'sz000002', 'review': '未接住'}]}
        with patch.object(wo, 'fetch_quotes', return_value={}), patch.object(wo, 'fetch_minute_paths', return_value={}), \
             patch('claude_client.complete_json', return_value=(ai_data, {'model': 'x'})):
            result = wo.settle(self.prior(), today_pools, ai=True)
        self.assertEqual(result['ai_market_view'], '整体偏强')
        self.assertEqual(result['items'][0]['outcome']['ai_review'], '接得住')

    def test_settle_ai_failure_degrades_to_rules_only(self):
        import claude_client
        today_pools = {'zt': {'rows': []}, 'zb': {'rows': []}, 'dt': {'rows': []}}
        with patch.object(wo, 'fetch_quotes', return_value={}), patch.object(wo, 'fetch_minute_paths', return_value={}), \
             patch('claude_client.complete_json', side_effect=claude_client.ClaudeError('unavailable', '没有 key')):
            result = wo.settle(self.prior(), today_pools, ai=True)
        self.assertEqual(result['ai_meta']['status'], 'unavailable')
        self.assertEqual(result['ai_market_view'], '')
        self.assertNotIn('ai_review', result['items'][0]['outcome'])


class SummaryTests(unittest.TestCase):
    def test_unbuyable_excluded_from_hit_rate_style_split(self):
        items = [watch_item('sz000001'), watch_item('sz000002')]
        items[0]['outcome'] = {'result': 'one_word', 'verdict': 'unbuyable', 'close_vs_open': 5.0}
        items[1]['outcome'] = {'result': 'up', 'verdict': 'hit', 'close_vs_open': 2.0}
        s = wo.summarize(items)
        self.assertEqual(s['verdict_counts']['unbuyable'], 1)
        self.assertEqual(s['buyable_n'], 1)


class StatsTests(unittest.TestCase):
    def _write(self, d, trade_date, date, rules_version, verdicts):
        items = []
        for i, v in enumerate(verdicts):
            it = watch_item('sz%06d' % i, '票%d' % i)
            it['outcome'] = {'result': None, 'verdict': v}
            items.append(it)
        review = make_review(trade_date, date, items=items)
        review['next_day_watch']['rules_version'] = rules_version
        mr.save_review(review, d)

    def test_groups_by_rules_version_and_gates_on_min_n(self):
        d = Path(tempfile.mkdtemp())
        self._write(d, '20260901', '2026-09-01', 'nextday-rules-1', ['hit'] * 5 + ['miss'] * 5)
        self._write(d, '20260902', '2026-09-02', 'nextday-rules-2', ['hit'] * 15 + ['miss'] * 10)
        out = wo.stats(d)
        self.assertIn('nextday-rules-1', out)
        self.assertIn('nextday-rules-2', out)
        self.assertIsNone(out['nextday-rules-1']['hit_rate_pct'])   # 10 已结算 < MIN_N_FOR_RATE
        self.assertIsNotNone(out['nextday-rules-2']['hit_rate_pct'])  # 25 已结算 >= MIN_N_FOR_RATE

    def test_empty_dir_returns_empty(self):
        d = Path(tempfile.mkdtemp()) / 'nope'
        self.assertEqual(wo.stats(d), {})


class RunTests(unittest.TestCase):
    def test_no_review_at_all_raises(self):
        with self.assertRaises(mr.ReviewError):
            wo.run(Path(tempfile.mkdtemp()) / 'none')

    def test_no_prior_watch_returns_none(self):
        d = Path(tempfile.mkdtemp())
        mr.save_review(make_review('20260921', '2026-09-21', pools={}), d)
        self.assertIsNone(wo.run(d))

    def test_run_writes_outcome_into_both_files_and_is_idempotent(self):
        d = Path(tempfile.mkdtemp())
        items = [watch_item('sz000001', '甲')]
        mr.save_review(make_review('20260921', '2026-09-21', items=items), d)
        today_pools = {'zt': {'rows': [pool_row('sz000001', '甲', first_seal='09:31:00')]}, 'zb': {'rows': []}, 'dt': {'rows': []}}
        mr.save_review(make_review('20260922', '2026-09-22', pools=today_pools), d)

        with patch.object(wo, 'fetch_quotes', return_value={}), patch.object(wo, 'fetch_minute_paths', return_value={}):
            first = wo.run(d, ai=False)
            second = wo.run(d, ai=False)

        self.assertEqual(first['summary']['n'], 1)
        self.assertEqual(second['summary']['n'], 1)
        prior_on_disk = mr.load_latest(d)
        # load_latest 给的是最新（今天）文件；今天文件应带 prev_watch
        self.assertIn('prev_watch', prior_on_disk)
        self.assertEqual(prior_on_disk['prev_watch']['date'], '2026-09-21')

    def test_prior_file_itself_gets_outcome_archived(self):
        import tushare_sync
        d = Path(tempfile.mkdtemp())
        items = [watch_item('sz000001', '甲')]
        mr.save_review(make_review('20260921', '2026-09-21', items=items), d)
        mr.save_review(make_review('20260922', '2026-09-22', pools={}), d)
        with patch.object(wo, 'fetch_quotes', return_value={}), patch.object(wo, 'fetch_minute_paths', return_value={}):
            wo.run(d, ai=False)
        archived = tushare_sync.read(d / '20260921.json')
        self.assertIn('outcome', archived['next_day_watch']['items'][0])
        self.assertIn('outcome_summary', archived['next_day_watch'])

    def test_today_not_after_prior_is_skipped(self):
        # 同一天或更早的文件不能拿来结算自己
        d = Path(tempfile.mkdtemp())
        items = [watch_item('sz000001', '甲')]
        mr.save_review(make_review('20260921', '2026-09-21', items=items, pools={}), d)
        self.assertIsNone(wo.run(d))


if __name__ == '__main__':
    unittest.main()
