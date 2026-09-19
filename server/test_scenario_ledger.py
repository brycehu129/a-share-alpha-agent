import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import scenario_ledger as sl
from collect_quotes import CST

DAY = '2026-09-21'
ISSUED = datetime(2026, 9, 21, 10, 0, 30, tzinfo=CST).isoformat()


def bars(prices, start=(10, 1)):
    """从 start 起逐分钟的分钟收盘价。"""
    out, (h, m) = [], start
    for p in prices:
        out.append({'t': '%02d%02d' % (h, m), 'price': p})
        m += 1
        if m == 60:
            h, m = h + 1, 0
    return out


def rec(direction='up', trig=10.5, lo=10.7, hi=10.9, inv=9.8, price=10.0, conf=3, node='sentinel.node_0945',
        symbol='sz000001', rid='r1'):
    return {'id': rid, 'day': DAY, 'symbol': symbol, 'name': 't', 'node': node, 'issued_at': ISSUED,
            'price_at_issue': price, 'confidence': conf, 'action_hint': 'wait', 'is_holding': True,
            'scenario': {'label': 'x', 'direction': direction, 'trigger_price': trig, 'trigger_condition': 'c',
                         'target_low': lo, 'target_high': hi, 'invalidate_price': inv}}


class JudgeUpTests(unittest.TestCase):
    def outcome(self, prices, **kw):
        return sl.judge(rec(**kw), bars(prices))['outcome']

    def test_never_touched(self):
        self.assertEqual(self.outcome([10.0, 10.1, 10.2, 10.3]), 'not_triggered')

    def test_triggered_then_hit_the_target(self):
        self.assertEqual(self.outcome([10.1, 10.5, 10.6, 10.75]), 'triggered_and_hit')

    def test_triggered_then_failed_by_touching_the_invalidation_price(self):
        self.assertEqual(self.outcome([10.1, 10.5, 10.3, 9.75]), 'triggered_but_failed')

    def test_invalidated_before_it_ever_triggered(self):
        self.assertEqual(self.outcome([10.1, 9.75, 10.3, 10.6]), 'invalidated_first')

    def test_triggered_but_neither_target_nor_invalidation_is_its_own_outcome(self):
        """触发了但直到收盘既没到目标也没触及失效价——没有结论。硬塞进"命中"或"失败"都会扭曲命中率。"""
        self.assertEqual(self.outcome([10.1, 10.5, 10.4, 10.45, 10.3]), 'triggered_unresolved')

    def test_target_and_failure_are_ordered_by_time(self):
        self.assertEqual(self.outcome([10.5, 10.75, 9.75]), 'triggered_and_hit')      # 先到目标，后来才跌破失效
        self.assertEqual(self.outcome([10.5, 9.75, 10.75]), 'triggered_but_failed')   # 先失效，后来才到目标

    def test_touching_exactly_counts(self):
        self.assertEqual(self.outcome([10.5, 10.7]), 'triggered_and_hit')


class JudgeDownTests(unittest.TestCase):
    def outcome(self, prices):
        return sl.judge(rec('down', trig=9.8, lo=9.3, hi=9.6, inv=10.2), bars(prices))['outcome']

    def test_mirror_of_up(self):
        self.assertEqual(self.outcome([9.9, 9.8, 9.7, 9.55]), 'triggered_and_hit')
        self.assertEqual(self.outcome([9.9, 9.8, 9.9, 10.2]), 'triggered_but_failed')
        self.assertEqual(self.outcome([9.95, 10.2, 9.7]), 'invalidated_first')
        self.assertEqual(self.outcome([9.9, 9.95, 9.9]), 'not_triggered')
        self.assertEqual(self.outcome([9.9, 9.8, 9.7, 9.75]), 'triggered_unresolved')


class NoHindsightTests(unittest.TestCase):
    def test_bars_before_or_at_the_issue_minute_are_ignored(self):
        """发布之前的走势一概不看，否则"事后诸葛亮"，任何情景都能被说成对的。"""
        r = rec()                                                     # 10:00:30 发布
        early = bars([10.6, 10.8], start=(9, 55)) + bars([10.0, 10.1], start=(10, 0)) + bars([10.1, 10.2], start=(10, 1))
        res = sl.judge(r, early)
        self.assertEqual(res['outcome'], 'not_triggered')             # 早先已经涨到 10.8 不算数
        self.assertEqual(res['first_bar_after_issue'], '1001')        # 严格晚于发布那一分钟

    def test_no_data_after_issue_is_not_judged(self):
        res = sl.judge(rec(), bars([10.6], start=(9, 40)))
        self.assertIsNone(res['outcome'])


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        sl.record(self.dir, DAY, ISSUED, 'sz000001', 't', 'sentinel.node_0945', 10.0,
                  [rec()['scenario']], 'wait', 3, True)

    def minute(self, complete=True, date=DAY, prices=(10.1, 10.6, 10.8)):
        return {'trade_date': date, 'complete': complete, 'bars': bars(list(prices))}

    def run_reconcile(self, **kw):
        return sl.reconcile(self.dir, DAY, lambda s, n: self.minute(**kw))

    def test_full_day_is_judged_and_persisted(self):
        out = self.run_reconcile()
        self.assertEqual(out['results'][0]['outcome'], 'triggered_and_hit')
        self.assertTrue((self.dir / ('reconcile-%s.json' % DAY)).exists())
        self.assertEqual(out['basis'], 'minute_close')
        self.assertTrue(out['limitations'])

    def test_incomplete_day_is_not_judged(self):
        """还没走到收盘就下结论，等于用不完整的数据判对错。"""
        r = self.run_reconcile(complete=False)['results'][0]
        self.assertIsNone(r['outcome'])
        self.assertIn('15:00', r['note'])

    def test_wrong_date_data_is_not_judged(self):
        self.assertIsNone(self.run_reconcile(date='2026-09-18')['results'][0]['outcome'])

    def test_fetch_failure_is_recorded_not_guessed(self):
        def boom(s, n):
            raise OSError('down')
        r = sl.reconcile(self.dir, DAY, boom)['results'][0]
        self.assertIsNone(r['outcome'])
        self.assertIn('取分时失败', r['note'])

    def test_each_symbol_is_fetched_once(self):
        sl.record(self.dir, DAY, ISSUED, 'sz000001', 't', 'x', 10.0, [rec()['scenario']] * 2, 'wait', 3, True)
        calls = []
        sl.reconcile(self.dir, DAY, lambda s, n: calls.append(s) or self.minute())
        self.assertEqual(calls, ['sz000001'])

    def test_record_ids_are_unique_and_stored_with_every_field_needed_for_judging(self):
        rows = sl.load_scenarios(self.dir, DAY)
        self.assertEqual(len({r['id'] for r in rows}), len(rows))
        for k in ('issued_at', 'price_at_issue', 'confidence', 'node', 'scenario'):
            self.assertIn(k, rows[0])


def joined(outcome, conf=3, node='sentinel.node_0945', symbol='sz000001', trig=10.5, price=10.0, direction='up'):
    r = rec(conf=conf, node=node, symbol=symbol, trig=trig, price=price)
    r['scenario']['direction'] = direction
    return {**r, 'outcome': outcome, 'judge': {}}


class SummaryTests(unittest.TestCase):
    def test_no_percentages_below_the_sample_gate(self):
        """样本不足时给百分比，人会把它当真。计数照给，百分比不给。"""
        rows = [joined('triggered_and_hit')] * 5 + [joined('triggered_but_failed')] * 3
        s = sl.summarize(rows)['overall']
        self.assertEqual(s['counts']['triggered_and_hit'], 5)
        self.assertIsNone(s['hit_rate_pct'])
        self.assertIsNone(s['trigger_rate_pct'])

    def test_hit_rate_is_over_concluded_scenarios_only(self):
        rows = ([joined('triggered_and_hit')] * 15 + [joined('triggered_but_failed')] * 5
                + [joined('triggered_unresolved')] * 40 + [joined('not_triggered')] * 40)
        s = sl.summarize(rows)['overall']
        self.assertEqual(s['concluded'], 20)
        self.assertEqual(s['hit_rate_pct'], 75.0)                      # 15/20，不是 15/100
        self.assertEqual(s['trigger_rate_pct'], 60.0)                  # (15+5+40)/100

    def test_unjudged_scenarios_are_excluded_and_counted(self):
        s = sl.summarize([joined('triggered_and_hit'), joined(None), joined(None)])
        self.assertEqual(s['overall']['n'], 1)
        self.assertEqual(s['unjudged'], 2)

    def test_trigger_distance_is_reported_so_far_away_triggers_cant_flatter_the_rate(self):
        near = [joined('not_triggered', trig=10.2)] * 3
        far = [joined('not_triggered', trig=11.0)] * 3
        self.assertEqual(sl.summarize(near)['overall']['median_trigger_distance_pct'], 2.0)
        self.assertEqual(sl.summarize(far)['overall']['median_trigger_distance_pct'], 10.0)

    def test_grouping(self):
        rows = [joined('triggered_and_hit', conf=5, node='a', symbol='s1'),
                joined('triggered_but_failed', conf=1, node='b', symbol='s2')]
        s = sl.summarize(rows)
        self.assertEqual(set(s['confidence']), {'1-2', '4-5'})
        self.assertEqual(set(s['node']), {'a', 'b'})
        self.assertEqual(set(s['symbol']), {'s1', 's2'})

    def test_undelivered_scenarios_are_excluded_but_counted(self):
        rows = [joined('triggered_and_hit'), {**joined('triggered_and_hit'), 'delivered': False}]
        s = sl.summarize(rows)
        self.assertEqual(s['overall']['n'], 1)
        self.assertEqual(s['undelivered'], 1)

    def test_limitations_ride_along_with_every_summary(self):
        self.assertEqual(len(sl.summarize([])['limitations']), 3)
        self.assertIn('不等于交易盈利', ' '.join(sl.LIMITATIONS))


class WeeklyTests(unittest.TestCase):
    def test_weekly_joins_reconcile_results_and_renders_without_percentages_when_thin(self):
        d = Path(tempfile.mkdtemp())
        sl.record(d, DAY, ISSUED, 'sz000001', 't', 'sentinel.node_0945', 10.0, [rec()['scenario']], 'wait', 3, True)
        sl.reconcile(d, DAY, lambda s, n: {'trade_date': DAY, 'complete': True, 'bars': bars([10.1, 10.6, 10.8])})
        w = sl.weekly(d, DAY)
        self.assertEqual(w['overall']['n'], 1)
        text = sl.render_weekly(w)
        self.assertIn('样本不足', text)
        self.assertIn('不等于交易盈利', text)
        self.assertNotRegex(text, r'命中率：\d')

    def test_days_without_any_scenarios_are_fine(self):
        self.assertEqual(sl.weekly(Path(tempfile.mkdtemp()), DAY)['overall']['n'], 0)


if __name__ == '__main__':
    unittest.main()
