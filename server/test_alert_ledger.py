import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import alert_ledger as al
from collect_quotes import CST

DAY = '2026-09-21'


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + ('\n' if rows else ''), encoding='utf-8')


def ev(key='stop-sz000001', symbol='sz000001', kind='sentinel.stop_hit', severity='urgent', at='10:00:00',
      price='10.0', sha='h1'):
    return {'key': key, 'symbol': symbol, 'kind': kind, 'severity': severity,
            'detail': 'd', 'observed_at': '%sT%s+08:00' % (DAY, at), 'price': price, 'quote_at': at,
            'evidence': {'batch_sha256': sha, 'batch_fetched_at': at}}


def bar(symbol='sz000001', at='10:00:00', last='10.0'):
    return {'at': '%sT%s+08:00' % (DAY, at), 'symbol': symbol, 'quote_at': at, 'last': last}


def alert_rec(at='10:00:05', events=None, blocks=None, sent=True, text='body'):
    return {'at': '%sT%s+08:00' % (DAY, at), 'symbols': [b['symbol'] for b in (blocks or [])],
            'text': text, 'push': {'sent': sent}, 'blocks': blocks or [],
            'events': events if events is not None else []}


def block(symbol='sz000001', price='10.0', pushed=True, text='block-text'):
    return {'symbol': symbol, 'name': 't', 'urgent': True, 'text': text, 'pushed': pushed,
            'price': price, 'change_pct': '-1.0', 'events': [], 'flow': None, 'book': None}


class Fixture(unittest.TestCase):
    """al.sdir(directory) 在传了显式 directory 时把它原样当哨兵目录用（不像默认情况那样再拼
    一层 'sentinel' 子目录，参见 scenario_ledger.sentinel_dir）。所以 alerts 直接写在 self.dir
    根下；intraday 是完全独立的一个目录，同样按 intraday_dir 参数原样使用。"""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.intraday = Path(tempfile.mkdtemp())

    def events_path(self):
        return self.intraday / ('events-%s.jsonl' % DAY)

    def bars_path(self):
        return self.intraday / ('bars-%s.jsonl' % DAY)

    def alerts_path(self):
        return self.dir / ('alerts-%s.jsonl' % DAY)


class AuditEventCountTests(Fixture):
    def test_matching_counts_pass(self):
        _write_jsonl(self.events_path(), [ev()])
        _write_jsonl(self.alerts_path(), [alert_rec(events=[{'key': 'stop-sz000001', 'kind': 'sentinel.stop_hit'}])])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertEqual(r['checked'], 1)
        self.assertEqual(r['mismatches'], [])

    def test_engine_fired_but_alert_never_recorded_it(self):
        _write_jsonl(self.events_path(), [ev(), ev(at='10:05:00')])
        _write_jsonl(self.alerts_path(), [alert_rec(events=[{'key': 'stop-sz000001', 'kind': 'sentinel.stop_hit'}])])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertTrue(any('触发 2 次，alerts 留档 1 次' in m['why'] for m in r['mismatches']))

    def test_alert_has_a_key_the_engine_never_fired(self):
        _write_jsonl(self.events_path(), [])
        _write_jsonl(self.alerts_path(), [alert_rec(events=[{'key': 'ghost-sz000001', 'kind': 'sentinel.stop_hit'}])])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertTrue(any('engine 事件里没有' in m['why'] for m in r['mismatches']))

    def test_node_events_are_excluded_from_the_count_check(self):
        """节点信号从不推送，不该被当成"丢事件"。"""
        _write_jsonl(self.events_path(), [ev(kind='sentinel.node_0945', key='node0945-sz000001')])
        _write_jsonl(self.alerts_path(), [])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertEqual(r['checked'], 0)
        self.assertEqual(r['mismatches'], [])


class AuditPriceTests(Fixture):
    def test_matching_price_passes(self):
        _write_jsonl(self.bars_path(), [bar(at='10:00:00', last='10.00')])
        b = block(price='10.00')
        _write_jsonl(self.alerts_path(), [alert_rec(at='10:00:03', blocks=[b], text=b['text'])])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertEqual(r['mismatches'], [])

    def test_mismatched_price_is_flagged(self):
        _write_jsonl(self.bars_path(), [bar(at='10:00:00', last='10.00')])
        _write_jsonl(self.alerts_path(), [alert_rec(at='10:00:03', blocks=[block(price='11.00')])])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertTrue(any('对不上' in m['why'] for m in r['mismatches']))

    def test_a_bar_far_outside_the_tolerance_window_is_not_used(self):
        """两处时间戳允许几十秒的时钟偏差，但不能拿几分钟前的价格去质疑刚发生的告警。"""
        _write_jsonl(self.bars_path(), [bar(at='09:00:00', last='11.00')])
        b = block(price='10.00')
        _write_jsonl(self.alerts_path(), [alert_rec(at='10:00:03', blocks=[b], text=b['text'])])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertEqual(r['mismatches'], [])          # 找不到匹配的观测，不能判定，也不算错


class AuditPushConsistencyTests(Fixture):
    def test_pushed_block_missing_from_the_sent_text_is_flagged(self):
        rec = alert_rec(blocks=[block(pushed=True, text='应该在里面')], sent=True, text='别的内容')
        _write_jsonl(self.alerts_path(), [rec])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertTrue(any('文本没有出现' in m['why'] for m in r['mismatches']))

    def test_watch_only_block_leaking_into_the_sent_text_is_flagged(self):
        rec = alert_rec(blocks=[block(pushed=False, text='观察级')], sent=True, text='正文里混进了观察级')
        _write_jsonl(self.alerts_path(), [rec])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertTrue(any('却出现在推送文本里' in m['why'] for m in r['mismatches']))

    def test_normal_pushed_case_has_no_findings(self):
        rec = alert_rec(blocks=[block(pushed=True, text='正文')], sent=True, text='正文')
        _write_jsonl(self.alerts_path(), [rec])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertEqual(r['mismatches'], [])

    def test_old_records_without_the_pushed_field_are_skipped_not_flagged(self):
        rec = alert_rec(blocks=[{**block(text='旧记录'), 'pushed': None}], sent=True, text='旧记录')
        del rec['blocks'][0]['pushed']
        _write_jsonl(self.alerts_path(), [rec])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertEqual(r['mismatches'], [])


class AuditRearmTests(Fixture):
    def test_gap_shorter_than_rearm_is_flagged(self):
        # ma20_break 的重新武装间隔是 LEVEL_REARM_S（1800秒=30分钟），10:00 和 10:10 只隔 10 分钟。
        _write_jsonl(self.events_path(), [
            ev(key='ma20-sz000001', kind='sentinel.ma20_break', severity='action', at='10:00:00'),
            ev(key='ma20-sz000001', kind='sentinel.ma20_break', severity='action', at='10:10:00'),
        ])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertTrue(any('小于重新武装间隔' in m['why'] for m in r['mismatches']))

    def test_gap_longer_than_rearm_is_fine(self):
        _write_jsonl(self.events_path(), [
            ev(key='ma20-sz000001', kind='sentinel.ma20_break', severity='action', at='10:00:00'),
            ev(key='ma20-sz000001', kind='sentinel.ma20_break', severity='action', at='10:40:00'),
        ])
        # 满足事件计数一致（否则会被检查①误报），这里只关心检查④。
        _write_jsonl(self.alerts_path(), [alert_rec(events=[{'key': 'ma20-sz000001', 'kind': 'sentinel.ma20_break'}] * 2)])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertEqual(r['mismatches'], [])

    def test_unknown_kind_is_not_checked(self):
        _write_jsonl(self.events_path(), [
            ev(key='x-sz000001', kind='sentinel.something_new', severity='action', at='10:00:00'),
            ev(key='x-sz000001', kind='sentinel.something_new', severity='action', at='10:00:05'),
        ])
        _write_jsonl(self.alerts_path(), [alert_rec(events=[{'key': 'x-sz000001', 'kind': 'sentinel.something_new'}] * 2)])
        r = al.audit_day(self.dir, self.intraday, DAY)
        self.assertEqual(r['mismatches'], [])


class JudgeTests(unittest.TestCase):
    def test_sell_side_favorable_when_price_falls_first(self):
        self.assertEqual(al.judge_sell_side(10.0, 0.05, 1.0, [9.9, 9.4, 10.2]), 'favorable')

    def test_sell_side_unfavorable_when_price_rallies_first(self):
        self.assertEqual(al.judge_sell_side(10.0, 0.05, 1.0, [10.6, 9.0]), 'unfavorable')

    def test_sell_side_inconclusive_when_neither_threshold_is_reached(self):
        self.assertIsNone(al.judge_sell_side(10.0, 0.05, 1.0, [10.1, 9.9, 10.0]))

    def test_t_sell_favorable_when_buyback_level_is_reached(self):
        self.assertEqual(al.judge_t_sell(10.0, 0.05, [9.9]), 'favorable')

    def test_t_sell_unfavorable_when_price_keeps_climbing(self):
        self.assertEqual(al.judge_t_sell(10.0, 0.05, [10.6]), 'unfavorable')

    def test_t_buy_favorable_when_price_recovers(self):
        self.assertEqual(al.judge_t_buy(10.0, 0.05, [10.1]), 'favorable')

    def test_t_buy_unfavorable_when_price_keeps_falling(self):
        self.assertEqual(al.judge_t_buy(10.0, 0.05, [9.4]), 'unfavorable')

    def test_buy_signal_favorable_on_follow_through(self):
        self.assertEqual(al.judge_buy_signal(10.0, 0.05, [10.6]), 'favorable')

    def test_buy_signal_unfavorable_on_a_fake_breakout(self):
        self.assertEqual(al.judge_buy_signal(10.0, 0.05, [9.7]), 'unfavorable')

    def test_no_prices_is_inconclusive_not_an_error(self):
        self.assertIsNone(al.judge_event('sentinel.stop_hit', 10.0, 0.05, []))

    def test_unlabeled_kind_returns_none(self):
        self.assertIsNone(al.judge_event('sentinel.below_cost', 10.0, 0.05, [9.0]))


class LabelDayTests(Fixture):
    def test_only_delivered_events_are_labelled(self):
        """batch_sha256 精确匹配到那次告警是否真的送达；没送达的（webhook 失败）不计入。"""
        _write_jsonl(self.events_path(), [
            ev(key='stop-sz000001', kind='sentinel.stop_hit', severity='urgent', sha='ok'),
            ev(key='stop-sz000002', symbol='sz000002', kind='sentinel.stop_hit', severity='urgent', sha='fail'),
        ])
        _write_jsonl(self.alerts_path(), [alert_rec(events=[
            {'key': 'stop-sz000001', 'kind': 'sentinel.stop_hit', 'evidence': {'batch_sha256': 'ok'}},
        ], sent=True), alert_rec(at='10:01:00', events=[
            {'key': 'stop-sz000002', 'kind': 'sentinel.stop_hit', 'evidence': {'batch_sha256': 'fail'}},
        ], sent=False)])
        rows = al.label_day(self.dir, self.intraday, self.dir / 'history', DAY)
        self.assertEqual([r['symbol'] for r in rows], ['sz000001'])

    def test_watch_severity_events_are_not_labelled(self):
        _write_jsonl(self.events_path(), [ev(kind='sentinel.buy_signal', severity='watch', sha='ok')])
        _write_jsonl(self.alerts_path(), [alert_rec(events=[
            {'key': 'stop-sz000001', 'kind': 'sentinel.buy_signal', 'evidence': {'batch_sha256': 'ok'}}], sent=True)])
        rows = al.label_day(self.dir, self.intraday, self.dir / 'history', DAY)
        self.assertEqual(rows, [])

    def test_outcome_uses_only_bars_strictly_after_the_alert(self):
        _write_jsonl(self.events_path(), [ev(sha='ok', at='10:00:00')])
        _write_jsonl(self.alerts_path(), [alert_rec(events=[
            {'key': 'stop-sz000001', 'kind': 'sentinel.stop_hit', 'evidence': {'batch_sha256': 'ok'}}], sent=True)])
        _write_jsonl(self.bars_path(), [
            bar(at='09:30:00', last='8.00'),      # 发布之前的暴跌不算数（事后诸葛亮）
            bar(at='10:00:00', last='10.0'),      # 和告警同一刻，不是"之后"
            bar(at='10:05:00', last='9.9'),
        ])
        rows = al.label_day(self.dir, self.intraday, self.dir / 'history', DAY)
        self.assertEqual(rows[0]['bars_after'], 1)     # 只有 10:05 那一条算"之后"

    def test_no_local_history_falls_back_to_the_nominal_atr(self):
        _write_jsonl(self.events_path(), [ev(sha='ok')])
        _write_jsonl(self.alerts_path(), [alert_rec(events=[
            {'key': 'stop-sz000001', 'kind': 'sentinel.stop_hit', 'evidence': {'batch_sha256': 'ok'}}], sent=True)])
        _write_jsonl(self.bars_path(), [bar(at='10:05:00', last='9.5')])
        rows = al.label_day(self.dir, self.intraday, self.dir / 'nonexistent_history', DAY)
        self.assertTrue(rows[0]['atr_nominal'])


class SummarizeAndRenderTests(unittest.TestCase):
    def test_percentages_withheld_under_the_sample_floor(self):
        rows = [{'kind': 'sentinel.stop_hit', 'outcome': 'favorable'} for _ in range(5)]
        s = al.summarize(rows)
        self.assertIsNone(s['overall']['favorable_rate_pct'])
        self.assertEqual(s['overall']['favorable'], 5)

    def test_percentage_shown_once_the_floor_is_met(self):
        rows = ([{'kind': 'sentinel.stop_hit', 'outcome': 'favorable'} for _ in range(15)]
               + [{'kind': 'sentinel.stop_hit', 'outcome': 'unfavorable'} for _ in range(5)])
        s = al.summarize(rows)
        self.assertEqual(s['overall']['favorable_rate_pct'], 75.0)

    def test_inconclusive_rows_are_not_dropped_from_the_denominator_of_n(self):
        rows = [{'kind': 'k', 'outcome': None}, {'kind': 'k', 'outcome': 'favorable'}]
        s = al.summarize(rows)
        self.assertEqual((s['overall']['n'], s['overall']['concluded'], s['overall']['inconclusive']), (2, 1, 1))

    def test_daily_report_mentions_mechanism_and_labels(self):
        audit = {'checked': 3, 'mismatches': []}
        text = al.render_daily(DAY, audit, [{'kind': 'sentinel.stop_hit', 'outcome': 'favorable'}])
        self.assertIn('机制核对', text)
        self.assertIn('3 条已检查', text)
        self.assertIn('全部通过', text)
        self.assertIn('避损', text)

    def test_daily_report_surfaces_mismatch_count(self):
        audit = {'checked': 3, 'mismatches': [{'key': 'x', 'why': 'y'}]}
        text = al.render_daily(DAY, audit, [])
        self.assertIn('发现 1 处不一致', text)


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _write_day(self, day, outcomes, mismatches=0, checked=1):
        path = al.sdir(self.dir) / ('alert_audit-%s.json' % day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'day': day, 'audit': {'checked': checked, 'mismatches': [{}] * mismatches},
                                    'outcomes': outcomes}), encoding='utf-8')

    def test_merges_several_days(self):
        self._write_day('2026-09-19', [{'kind': 'sentinel.stop_hit', 'outcome': 'favorable'}])
        self._write_day('2026-09-21', [{'kind': 'sentinel.stop_hit', 'outcome': 'unfavorable'}], mismatches=1)
        w = al.weekly(self.dir, None, None, '2026-09-21', days=7)
        self.assertEqual(w['checked'], 2)
        self.assertEqual(w['mismatches'], 1)
        self.assertEqual((w['overall']['favorable'], w['overall']['unfavorable']), (1, 1))

    def test_missing_days_are_skipped_not_an_error(self):
        w = al.weekly(self.dir, None, None, '2026-09-21', days=7)
        self.assertEqual(w['overall']['n'], 0)

    def test_render_weekly_includes_limitations(self):
        self._write_day('2026-09-21', [])
        text = al.render_weekly(al.weekly(self.dir, None, None, '2026-09-21', days=1))
        self.assertIn('哨兵告警周报', text)
        self.assertIn('次日开盘', text)


if __name__ == '__main__':
    unittest.main()
