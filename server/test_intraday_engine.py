import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import intraday_engine as ie
import minute_data
from collect_quotes import CST

DAY = datetime(2026, 9, 21, 10, 0, 0, tzinfo=CST)    # 周一 上午盘中


def at(h, m, s=0, day=21):
    return datetime(2026, 9, day, h, m, s, tzinfo=CST)


def quote(symbol='sz000001', last='10.00', when=None, age=5, date='2026-09-21'):
    when = when or DAY
    return {'symbol': symbol, 'market': 'cn', 'name': symbol, 'last': last, 'previous_close': '10',
            'open': '10', 'high': last, 'low': last, 'change_pct': '0', 'quote_date': date,
            'quote_at': when.isoformat(), 'age_seconds': age, 'batch_sha256': 'h' * 8,
            'batch_fetched_at': when.isoformat()}


def snap(*quotes, failures=()):
    return {'quotes': list(quotes), 'failures': [{'symbol': s, 'reason': 'x'} for s in failures]}


def sig(active, key='k', symbol='sz000001', **kw):
    return {'key': key, 'symbol': symbol, 'kind': 'test', 'active': active, 'detail': 'd', **kw}


class GateTests(unittest.TestCase):
    def test_runs_only_in_continuous_session_with_confirmed_open_calendar(self):
        self.assertTrue(ie.gate(at(10, 0), 'open')[0])
        self.assertTrue(ie.gate(at(14, 30), 'open')[0])
        self.assertTrue(ie.gate(at(15, 1), 'open')[0])          # 收盘后最后一轮，抓收盘价
        for when in (at(9, 10), at(9, 29), at(12, 0), at(15, 3), at(16, 0), at(10, 0, day=20)):  # 20日是周日
            self.assertFalse(ie.gate(when, 'open')[0], when)

    def test_unknown_or_closed_calendar_never_runs(self):
        """日历缺失时假定开市，比漏跑一轮糟得多：非交易日会拿旧数据当实时数据。"""
        for state in ('closed', 'unknown', 'unknown(ValueError)'):
            self.assertFalse(ie.gate(at(10, 0), state)[0])


class TimeMathTests(unittest.TestCase):
    def test_lunch_break_is_not_a_gap(self):
        self.assertEqual(ie.trading_seconds_between(at(11, 29), at(13, 1)), 120)
        self.assertEqual(ie.trading_seconds_between(at(10, 0), at(10, 3)), 180)
        self.assertEqual(ie.trading_seconds_between(at(10, 3), at(10, 0)), 0)


class SignalDecisionTests(unittest.TestCase):
    def fire(self, state, signals, now, quotes=None):
        return ie.apply_signals(state, signals, quotes or {}, now)

    def test_edge_signal_fires_once_then_stays_quiet_while_true(self):
        st = ie.new_state('2026-09-21')
        e1, _, _ = self.fire(st, [sig(True)], at(10, 0))
        e2, _, _ = self.fire(st, [sig(True)], at(10, 1))
        e3, _, _ = self.fire(st, [sig(True)], at(10, 2))
        self.assertEqual((len(e1), len(e2), len(e3)), (1, 0, 0))

    def test_duplicate_keys_in_one_tick_fire_once(self):
        """两个评估器都汇报了同一个 key（比如一只股票既是持仓又在自选）：只能推一次。"""
        st = ie.new_state('2026-09-21')
        events, _, _ = self.fire(st, [sig(True), sig(True), sig(True, detail='另一个评估器')], at(10, 0))
        self.assertEqual(len(events), 1)
        self.assertEqual(st['signals']['k']['fired'], 1)

    def test_inactive_signal_never_fires(self):
        st = ie.new_state('2026-09-21')
        self.assertEqual(self.fire(st, [sig(False)], at(10, 0))[0], [])

    def test_flapping_around_a_threshold_is_suppressed(self):
        """价格在阈值附近来回穿越：不压制的话每分钟推一条，用户很快就不看了。"""
        st = ie.new_state('2026-09-21')
        self.fire(st, [sig(True)], at(10, 0))
        self.fire(st, [sig(False)], at(10, 1))
        events, _, flapped = self.fire(st, [sig(True)], at(10, 2))     # 只安静了 60s < 300s
        self.assertEqual(events, [])
        self.assertEqual(flapped, ['k'])

    def test_rearms_after_being_quiet_long_enough(self):
        st = ie.new_state('2026-09-21')
        self.fire(st, [sig(True)], at(10, 0))
        self.fire(st, [sig(False)], at(10, 1))
        events, _, _ = self.fire(st, [sig(True)], at(10, 7))           # 安静了 6 分钟
        self.assertEqual(len(events), 1)

    def test_cooldown_mode_repeats_only_after_the_cooldown(self):
        st = ie.new_state('2026-09-21')
        s = lambda: [sig(True, mode='cooldown', cooldown_s=600)]
        self.assertEqual(len(self.fire(st, s(), at(10, 0))[0]), 1)
        self.assertEqual(len(self.fire(st, s(), at(10, 5))[0]), 0)
        self.assertEqual(len(self.fire(st, s(), at(10, 10))[0]), 1)

    def test_per_tick_cap_defers_instead_of_dropping(self):
        """被上限挤掉的不能悄悄丢——下一轮它们仍是"从未推过"，必须顺延推出。"""
        st = ie.new_state('2026-09-21')
        many = [sig(True, key='k%d' % i, symbol='sz00000%d' % i) for i in range(ie.MAX_EVENTS_PER_TICK + 2)]
        first, deferred, _ = self.fire(st, many, at(10, 0))
        self.assertEqual(len(first), ie.MAX_EVENTS_PER_TICK)
        self.assertEqual(len(deferred), 2)
        second, deferred2, _ = self.fire(st, many, at(10, 1))
        self.assertEqual(sorted(e['key'] for e in second), sorted(deferred))
        self.assertEqual(deferred2, [])
        self.assertEqual(len({e['key'] for e in first + second}), len(many))    # 一条没丢、没重复

    def test_deferred_signal_is_not_marked_as_fired(self):
        """被挤掉的信号状态必须原封不动。边沿模式下即使误标"已推"也碰巧能顺延（因为
        没有 inactive_since 时不触发抖动压制），所以要直接断言状态本身，不能只看结果。"""
        st = ie.new_state('2026-09-21')
        many = [sig(True, key='k%d' % i) for i in range(ie.MAX_EVENTS_PER_TICK + 1)]
        _, deferred, _ = self.fire(st, many, at(10, 0))
        rec = st['signals'][deferred[0]]
        self.assertIsNone(rec['last_fired_at'])
        self.assertEqual(rec['fired'], 0)
        self.assertFalse(rec['active'])

    def test_cooldown_mode_deferral_does_not_burn_the_cooldown(self):
        """冷却模式下如果被挤掉的信号被误记成"刚推过"，它要再等一整个冷却期才能推——
        等于把事件丢了。"""
        st = ie.new_state('2026-09-21')
        many = [sig(True, key='k%d' % i, mode='cooldown', cooldown_s=3600)
                for i in range(ie.MAX_EVENTS_PER_TICK + 1)]
        _, deferred, _ = self.fire(st, many, at(10, 0))
        second, _, _ = self.fire(st, many, at(10, 1))          # 只过了 1 分钟，远小于冷却期
        self.assertEqual([e['key'] for e in second], deferred)

    def test_urgent_signals_outrank_normal_when_capped(self):
        st = ie.new_state('2026-09-21')
        normal = [sig(True, key='n%d' % i) for i in range(ie.MAX_EVENTS_PER_TICK)]
        urgent = sig(True, key='zzz-urgent', severity='urgent')
        events, deferred, _ = self.fire(st, normal + [urgent], at(10, 0))
        self.assertIn('zzz-urgent', [e['key'] for e in events])
        self.assertEqual(len(deferred), 1)

    def test_event_carries_audit_evidence(self):
        st = ie.new_state('2026-09-21')
        q = quote(last='9.50')
        events, _, _ = self.fire(st, [sig(True)], at(10, 0), {'sz000001': q})
        e = events[0]
        self.assertEqual(e['price'], '9.50')
        self.assertEqual(e['evidence']['batch_sha256'], q['batch_sha256'])
        self.assertEqual(e['observed_at'], at(10, 0).isoformat())

    def test_bad_signals_are_rejected_loudly(self):
        for bad in ({'symbol': 's', 'kind': 'k', 'active': True},                      # 缺 key
                    sig(True, mode='sometimes'), sig(True, severity='panic')):
            with self.assertRaises(ValueError):
                ie.normalize_signal(bad)


class CarryTests(unittest.TestCase):
    def test_carried_signal_does_not_refire_the_next_morning(self):
        """"已跌破成本价"是持续状态：昨天就已经破了，今天早上不该再当新事件推一遍。"""
        yesterday = ie.new_state('2026-09-18')
        ie.apply_signals(yesterday, [sig(True, carry=True)], {}, at(14, 0, day=18))
        today = ie.new_state('2026-09-21', yesterday)
        events, _, _ = ie.apply_signals(today, [sig(True, carry=True)], {}, at(9, 31))
        self.assertEqual(events, [])

    def test_non_carry_signals_reset_every_day(self):
        yesterday = ie.new_state('2026-09-18')
        ie.apply_signals(yesterday, [sig(True)], {}, at(14, 0, day=18))
        today = ie.new_state('2026-09-21', yesterday)
        self.assertNotIn('k', today['signals'])
        self.assertEqual(len(ie.apply_signals(today, [sig(True)], {}, at(9, 31))[0]), 1)

    def test_carried_signal_that_recovers_can_fire_again(self):
        yesterday = ie.new_state('2026-09-18')
        ie.apply_signals(yesterday, [sig(True, carry=True)], {}, at(14, 0, day=18))
        today = ie.new_state('2026-09-21', yesterday)
        ie.apply_signals(today, [sig(False, carry=True)], {}, at(9, 31))
        self.assertEqual(len(ie.apply_signals(today, [sig(True, carry=True)], {}, at(9, 40))[0]), 1)


class NearMissTests(unittest.TestCase):
    def levels(self, prices, direction='up', target=10.0):
        st = ie.new_state('2026-09-21')
        for i, p in enumerate(prices):
            q = quote(last=str(p), when=at(10, i))
            ie.apply_signals(st, [sig(False, level={'price': target, 'direction': direction})],
                             {'sz000001': q}, at(10, i))
        return st['levels']['k']

    def test_records_the_closest_approach_without_reaching(self):
        """"距激活只差 0.3%" 直接说明阈值是不是卡得太死，这类信息事后补不了。"""
        rec = self.levels([9.5, 9.97, 9.8])
        self.assertFalse(rec['reached'])
        self.assertAlmostEqual(rec['closest_pct'], (10 / 9.97 - 1) * 100, places=3)

    def test_reaching_the_level_is_recorded_as_zero_remaining(self):
        rec = self.levels([9.9, 10.2])
        self.assertTrue(rec['reached'])
        self.assertEqual(rec['closest_pct'], 0.0)

    def test_overshoot_is_not_confused_with_being_short(self):
        """越过 0.5% 记成 0，而不是"还差 0.5%"。"""
        rec = self.levels([10.05, 9.9])
        self.assertEqual(rec['closest_pct'], 0.0)

    def test_down_direction(self):
        rec = self.levels([10.4, 10.06], direction='down', target=10.0)
        self.assertAlmostEqual(rec['closest_pct'], (10.06 / 10 - 1) * 100, places=3)


class TickContextTests(unittest.TestCase):
    def tick(self, snapshot, state=None, now=DAY, minute_fetch=None):
        return ie.make_tick(now, 'morning', snapshot, state or ie.new_state('2026-09-21'),
                            minute_fetch or (lambda s, n: None))

    def test_only_fresh_quotes_from_today_reach_evaluators(self):
        t = self.tick(snap(quote('sz000001'), quote('sz000002', age=600),
                           quote('sz000003', date='2026-09-18', age=5)))
        self.assertEqual(list(t['quotes']), ['sz000001'])
        self.assertEqual(t['stale'], ['sz000002'])
        self.assertEqual(t['wrong_day'], ['sz000003'])

    def test_quote_without_a_measurable_age_is_not_trusted(self):
        t = self.tick(snap(quote(age=None)))
        self.assertEqual(t['quotes'], {})

    def test_gap_is_reported_but_lunch_is_not_a_gap(self):
        st = ie.new_state('2026-09-21')
        st['last_tick_at'] = at(9, 40).isoformat()
        self.assertEqual(self.tick(snap(), st, now=at(9, 41))['gap_seconds'], 60)
        self.assertEqual(self.tick(snap(), st, now=at(9, 50))['gap_seconds'], 600)
        st['last_tick_at'] = at(11, 29).isoformat()
        self.assertEqual(self.tick(snap(), st, now=at(13, 0, 30))['gap_seconds'], 90)

    def test_first_tick_of_the_day_has_no_gap(self):
        self.assertIsNone(self.tick(snap())['gap_seconds'])

    def test_node_due_reports_lateness_once_the_boundary_is_crossed(self):
        st = ie.new_state('2026-09-21')
        st['last_tick_at'] = at(9, 44, 30).isoformat()
        self.assertEqual(self.tick(snap(), st, now=at(9, 45, 20))['node_due']('0945'), 20)
        self.assertIsNone(self.tick(snap(), st, now=at(9, 44, 50))['node_due']('0945'))
        st['last_tick_at'] = at(9, 45, 30).isoformat()                      # 已经跨过了，这一轮不再算
        self.assertIsNone(self.tick(snap(), st, now=at(9, 46))['node_due']('0945'))

    def test_minutes_are_fetched_once_per_tick_and_must_be_today(self):
        calls = []
        today = {'trade_date': '2026-09-21', 'symbol': 'sz000001'}

        def fetch(symbol, now):
            calls.append(symbol)
            return today

        t = self.tick(snap(), minute_fetch=fetch)
        t['minutes']('sz000001'); t['minutes']('sz000001')
        self.assertEqual(calls, ['sz000001'])
        stale = self.tick(snap(), minute_fetch=lambda s, n: {'trade_date': '2026-09-18'})
        with self.assertRaises(minute_data.MinuteError):
            stale['minutes']('sz000001')


class RunTickTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def run_tick(self, now=DAY, snapshot=None, evaluators=(), calendar='open', **kw):
        return ie.run_tick(Path('.'), ['sz000001'], now=now, directory=self.dir,
                           snapshot_fn=lambda syms: snapshot or snap(quote(when=now)),
                           calendar_fn=lambda d: calendar, evaluators=list(evaluators), **kw)

    def test_skips_outside_the_gate_without_touching_the_network(self):
        called = []
        r = ie.run_tick(Path('.'), ['sz000001'], now=at(20, 0), directory=self.dir,
                        snapshot_fn=lambda s: called.append(1), calendar_fn=lambda d: 'open')
        self.assertFalse(r['ran'])
        self.assertEqual(called, [])
        self.assertFalse(list(self.dir.glob('*')))

    def test_force_bypasses_the_gate(self):
        self.assertTrue(self.run_tick(now=at(20, 0), force=True)['ran'])

    def test_state_and_logs_are_persisted(self):
        r = self.run_tick(evaluators=[lambda t: [sig(True)]])
        self.assertTrue(r['ran'])
        state = json.loads((self.dir / 'state-2026-09-21.json').read_text())
        self.assertEqual(state['tick_count'], 1)
        self.assertEqual(state['extremes']['sz000001']['n'], 1)
        ticks = (self.dir / 'ticks-2026-09-21.jsonl').read_text().splitlines()
        events = (self.dir / 'events-2026-09-21.jsonl').read_text().splitlines()
        self.assertEqual((len(ticks), len(events)), (1, 1))
        self.assertEqual(json.loads(ticks[0])['event_count'], 1)

    def test_dry_run_writes_nothing(self):
        self.run_tick(evaluators=[lambda t: [sig(True)]], dry_run=True)
        self.assertFalse(list(self.dir.glob('*')))

    def test_second_tick_does_not_repeat_an_edge_event(self):
        ev = [lambda t: [sig(True)]]
        first = self.run_tick(now=at(10, 0), evaluators=ev, snapshot=snap(quote(when=at(10, 0))))
        second = self.run_tick(now=at(10, 1), evaluators=ev, snapshot=snap(quote(when=at(10, 1))))
        self.assertEqual((len(first['events']), len(second['events'])), (1, 0))
        self.assertEqual(second['gap_seconds'], 60)

    def test_a_crash_in_one_evaluator_does_not_silence_the_others(self):
        def broken(tick):
            raise RuntimeError('boom')
        r = self.run_tick(evaluators=[broken, lambda t: [sig(True)]])
        self.assertEqual(len(r['events']), 1)
        self.assertTrue(any('boom' in e for e in r['evaluator_errors']))

    def test_stale_and_failed_symbols_are_reported_not_hidden(self):
        r = self.run_tick(snapshot=snap(quote(age=999), failures=['sz000002']))
        self.assertEqual(r['stale'], ['sz000001'])
        self.assertEqual(r['failures'], ['sz000002'])
        self.assertEqual(r['fresh'], 0)

    def test_snapshot_exception_is_contained(self):
        def boom(symbols):
            raise OSError('network down')
        r = ie.run_tick(Path('.'), ['sz000001'], now=DAY, directory=self.dir,
                        snapshot_fn=boom, calendar_fn=lambda d: 'open')
        self.assertFalse(r['ran'])
        self.assertIn('network down', r['error'])          # 记录原因，不抛出，下一分钟照常再来

    def test_calendar_failure_is_treated_as_unknown_not_open(self):
        def broken(d):
            raise ValueError('checksum')
        r = ie.run_tick(Path('.'), ['sz000001'], now=DAY, directory=self.dir,
                        snapshot_fn=lambda s: snap(), calendar_fn=broken)
        self.assertFalse(r['ran'])

    def test_overlapping_run_is_skipped_not_queued(self):
        """一分钟一轮：上一轮还没结束就不能再开一轮，否则两个进程互相覆盖状态。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / '.lock').write_text('existing lock')
        held = open(self.dir / '.lock', 'a')
        ie._lock_file(held)
        try:
            r = self.run_tick()
            self.assertFalse(r['ran'])
            self.assertIn('上一轮', r['skipped'])
        finally:
            ie._unlock_file(held)
            held.close()
        self.assertTrue(self.run_tick()['ran'])

    def test_corrupt_state_file_is_quarantined_not_fatal(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / 'state-2026-09-21.json').write_text('{not json')
        self.assertTrue(self.run_tick()['ran'])
        self.assertTrue((self.dir / 'state-2026-09-21.broken').exists())

    def test_no_symbols_means_nothing_to_do(self):
        r = ie.run_tick(Path('.'), [], now=DAY, directory=self.dir, calendar_fn=lambda d: 'open')
        self.assertEqual(r['skipped'], '没有需要监控的股票')


class RetentionAndHealthTests(unittest.TestCase):
    """每分钟取到的报价要留下来，并且能回答"今天到底采集了多少轮、为什么少"。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tick(self, now, symbols=('sz000001',), calendar='open', quotes=None, **kw):
        qs = quotes if quotes is not None else [quote(s, when=now) for s in symbols]
        return ie.run_tick(Path('.'), list(symbols), now=now, directory=self.dir,
                           snapshot_fn=lambda s: snap(*qs), calendar_fn=lambda d: calendar, evaluators=[], **kw)

    def rows(self, name):
        return [json.loads(l) for l in (self.dir / name).read_text().splitlines()]

    def test_each_tick_appends_one_row_per_fresh_quote_with_only_observed_fields(self):
        q = quote(when=at(10, 0))
        q.update(outer_vol='213103', inner_vol='189631', bid_ask_ratio=None)     # 委比缺失
        self.tick(at(10, 0), quotes=[q])
        self.tick(at(10, 1), symbols=('sz000001', 'sz000002'))
        rows = self.rows('bars-2026-09-21.jsonl')
        self.assertEqual([(r['symbol'], r['at'][11:16]) for r in rows],
                         [('sz000001', '10:00'), ('sz000001', '10:01'), ('sz000002', '10:01')])
        self.assertEqual(rows[0]['outer_vol'], '213103')
        self.assertNotIn('bid_ask_ratio', rows[0])       # 缺失就缺失，不补 0

    def test_stale_quotes_and_indexes_are_not_recorded(self):
        idx = {**quote('sh000001', when=at(10, 0)), 'is_index': True}
        self.tick(at(10, 0), symbols=('sz000001', 'sh000001'), quotes=[quote(age=999, when=at(10, 0)), idx])
        self.assertFalse((self.dir / 'bars-2026-09-21.jsonl').exists())

    def test_dry_run_records_nothing(self):
        self.tick(at(10, 0), dry_run=True)
        self.assertFalse(list(self.dir.glob('*')))

    def test_in_session_skips_leave_a_reason_but_routine_skips_do_not(self):
        self.tick(at(10, 0), calendar='unknown(ValueError)')       # 日历读不出来：值得记
        self.tick(at(10, 1), calendar='closed')                    # 确认休市：例行
        self.tick(at(12, 0))                                       # 午休：例行
        self.tick(at(20, 0))                                       # 盘后：例行
        rows = self.rows('ticks-2026-09-21.jsonl')
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['ran'])
        self.assertIn('unknown', rows[0]['skipped'])

    def test_overlapping_run_and_empty_book_are_logged(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        held = open(self.dir / '.lock', 'a')
        ie._lock_file(held)
        try:
            self.tick(at(10, 0))
        finally:
            ie._unlock_file(held)
            held.close()
        ie.run_tick(Path('.'), [], now=at(10, 1), directory=self.dir, calendar_fn=lambda d: 'open')
        reasons = [r['skipped'] for r in self.rows('ticks-2026-09-21.jsonl')]
        self.assertTrue(any('上一轮' in r for r in reasons))
        self.assertIn('没有需要监控的股票', reasons)

    def test_expected_ticks_counts_session_minutes_only(self):
        self.assertEqual(ie.expected_ticks('2026-09-18', at(10, 0)), 243)          # 过去的整天
        self.assertEqual(ie.expected_ticks('2026-09-22', at(10, 0)), 0)            # 还没到
        self.assertEqual(ie.expected_ticks('2026-09-21', at(9, 29)), 0)
        self.assertEqual(ie.expected_ticks('2026-09-21', at(9, 30, 1)), 1)
        self.assertEqual(ie.expected_ticks('2026-09-21', at(11, 29)), 120)
        self.assertEqual(ie.expected_ticks('2026-09-21', at(12, 30)), 120)         # 午休不增加
        self.assertEqual(ie.expected_ticks('2026-09-21', at(13, 0)), 121)
        self.assertEqual(ie.expected_ticks('2026-09-21', at(16, 0)), 243)

    def test_health_summarises_ticks_gaps_skips_and_per_symbol_observations(self):
        self.tick(at(10, 0), symbols=('sz000001', 'sz000002'))
        self.tick(at(10, 1), symbols=('sz000001', 'sz000002'))
        self.tick(at(10, 6), symbols=('sz000001', 'sz000002'))                    # 中间断了 5 分钟
        self.tick(at(10, 7), calendar='unknown')
        h = ie.health(self.dir, '2026-09-21', at(10, 8))
        self.assertEqual((h['ticks'], h['expected']), (3, 39))
        self.assertEqual((h['first_tick'], h['last_tick']), ('10:00:00', '10:06:00'))
        self.assertEqual((h['gap_count'], h['gap_seconds']), (1, 300))
        self.assertEqual(h['gaps'][0], {'from': '10:01:00', 'to': '10:06:00', 'seconds': 300})
        self.assertEqual(h['skips'], [{'reason': '交易日历状态为 unknown，不是确认开市', 'count': 1}])
        self.assertEqual(h['observations'], {'sz000001': 3, 'sz000002': 3})

    def test_health_of_a_day_with_no_data_is_all_zero_not_an_error(self):
        h = ie.health(self.dir, '2026-09-18', at(10, 0))
        self.assertEqual((h['ticks'], h['expected'], h['gap_count'], h['skips'], h['observations']),
                         (0, 243, 0, [], {}))

    def test_old_retention_files_are_pruned_on_the_first_tick_of_a_day(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for name in ('bars-2026-06-01.jsonl', 'flow-2026-06-01.jsonl', 'bars-2026-09-01.jsonl', 'sentinel-note.txt'):
            (self.dir / name).write_text('x')
        self.tick(at(10, 0))
        left = sorted(p.name for p in self.dir.glob('*') if p.name.startswith(('bars', 'flow', 'sentinel')))
        self.assertEqual(left, ['bars-2026-09-01.jsonl', 'bars-2026-09-21.jsonl', 'sentinel-note.txt'])


class ObservedOnlyDisciplineTests(unittest.TestCase):
    """这一层的核心纪律：只认实际观测到的报价，漏了就是漏了，绝不事后补记。"""

    def test_a_touch_between_two_polls_is_never_detected(self):
        """10:00 价格 10.10，10:03 价格又回到 10.10；中间 10:01–10:02 曾跌到 9.40 触发止损位 9.50。
        没有观测到就是没发生——评估器只看得到 10:00 和 10:03 两次报价。"""
        directory = Path(tempfile.mkdtemp())
        seen = []

        def stop_loss(tick):
            q = tick['quotes']['sz000001']
            seen.append(float(q['last']))
            return [sig(float(q['last']) <= 9.50, key='stop', severity='urgent')]

        for when, price in ((at(10, 0), '10.10'), (at(10, 3), '10.10')):      # 9.40 那一刻没被轮询到
            ie.run_tick(Path('.'), ['sz000001'], now=when, directory=directory,
                        snapshot_fn=lambda s, w=when, p=price: snap(quote(last=p, when=w)),
                        calendar_fn=lambda d: 'open', evaluators=[stop_loss])
        self.assertEqual(seen, [10.10, 10.10])
        self.assertEqual((directory / 'events-2026-09-21.jsonl').exists(), False)
        state = json.loads((directory / 'state-2026-09-21.json').read_text())
        # 180 秒恰好等于 GAP_SECONDS，不算 gap（用 > 判定）；下一条测试覆盖真正的 gap。
        self.assertEqual(state['gaps'], [])
        self.assertEqual(state['extremes']['sz000001']['low'], 10.10)   # 极值只来自观测到的报价，没有 9.40

    def test_the_missed_interval_is_visible_as_a_gap_so_extremes_are_known_unobserved(self):
        directory = Path(tempfile.mkdtemp())
        for when in (at(10, 0), at(10, 5)):
            ie.run_tick(Path('.'), ['sz000001'], now=when, directory=directory,
                        snapshot_fn=lambda s, w=when: snap(quote(when=w)),
                        calendar_fn=lambda d: 'open', evaluators=[])
        state = json.loads((directory / 'state-2026-09-21.json').read_text())
        self.assertEqual(len(state['gaps']), 1)
        self.assertEqual(state['gaps'][0]['seconds'], 300)


if __name__ == '__main__':
    unittest.main()
