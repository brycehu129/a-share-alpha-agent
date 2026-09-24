import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scenario_ledger
import sentinel_view as sv

DAY = '2026-09-21'


def scenario_row(**kw):
    return {'id': 'r1', 'day': DAY, 'symbol': 'sz000001', 'name': '测试', 'node': 'sentinel.stop_hit',
            'issued_at': '2026-09-21T10:00:30+08:00', 'price_at_issue': 9.4, 'action_hint': 'wait', 'confidence': 3,
            'is_holding': True, 'scenario': {'label': '反抽', 'direction': 'up', 'trigger_price': 9.7,
                                             'trigger_condition': 'c', 'target_low': 9.9, 'target_high': 10.0,
                                             'invalidate_price': 9.3}, **kw}


class DayParamTests(unittest.TestCase):
    def test_path_traversal_and_junk_fall_back_to_today(self):
        """日期直接拼进文件名，?day=../../etc 就是路径穿越。"""
        today = sv.safe_day(None)
        for bad in ('../../etc/passwd', '2026-09-21/../x', '2026-13-45', '<script>', '', '20260921', '2026-09-2'):
            self.assertEqual(sv.safe_day(bad), today, bad)
        self.assertEqual(sv.safe_day('2026-09-21'), '2026-09-21')

    def test_symbol_param_only_accepts_sh_sz_stock_codes(self):
        self.assertEqual(sv.safe_symbol('sz000001'), 'sz000001')
        for bad in (None, '', '../x', 'sz00000', 'hkHSI', 'bj830799', 'sz000001;rm', 'SZ000001', 5):
            self.assertIsNone(sv.safe_symbol(bad), bad)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sdir = scenario_ledger.sentinel_dir(self.dir)
        self.sdir.mkdir(parents=True, exist_ok=True)
        self.idir = Path(tempfile.mkdtemp())                       # 盘中引擎的留存目录，别读到真实数据
        env = patch.dict(os.environ, {'INTRADAY_DIR': str(self.idir)})
        env.start()
        self.addCleanup(env.stop)

    def test_empty_day_returns_empty_lists_without_error(self):
        p = sv.sentinel_payload(DAY, self.dir)
        self.assertEqual((p['alerts'], p['scenarios']), ([], []))
        self.assertNotIn('charts', p)                                  # 分时图已取消
        self.assertEqual((p['ai_calls'], p['pending']), (0, 0))

    def test_alert_text_is_plain_data_the_frontend_renders_as_text(self):
        (self.sdir / ('alerts-%s.jsonl' % DAY)).write_text(json.dumps(
            {'at': '2026-09-21T10:00:00+08:00', 'text': '<script>alert(1)</script>触及止损', 'push': {'sent': True}}) + '\n')
        p = sv.sentinel_payload(DAY, self.dir)
        self.assertEqual(p['alerts'][0]['text'], '<script>alert(1)</script>触及止损')
        self.assertTrue(p['alerts'][0]['sent'])

    def test_unsent_alert_carries_the_reason(self):
        (self.sdir / ('alerts-%s.jsonl' % DAY)).write_text(json.dumps(
            {'at': '2026-09-21T10:00:00+08:00', 'text': 'x', 'push': {'sent': False, 'reason': '没配 webhook'}}) + '\n')
        a = sv.sentinel_payload(DAY, self.dir)['alerts'][0]
        self.assertFalse(a['sent'])
        self.assertEqual(a['reason'], '没配 webhook')

    def test_scenarios_carry_the_reconcile_outcome(self):
        (self.sdir / ('scenarios-%s.jsonl' % DAY)).write_text(json.dumps(scenario_row()) + '\n')
        (self.sdir / ('reconcile-%s.json' % DAY)).write_text(json.dumps(
            {'results': [{'id': 'r1', 'outcome': 'triggered_and_hit'}]}))
        s = sv.sentinel_payload(DAY, self.dir)['scenarios'][0]
        self.assertEqual(s['outcome'], '触发且命中')
        self.assertEqual((s['symbol'], s['direction'], s['confidence']), ('sz000001', 'up', 3))

    def test_judgments_group_one_analysis_into_one_summary_row(self):
        rows = [
            scenario_row(id='r1', issued_at='2026-09-21T10:00:30+08:00', price_at_issue=10.0,
                         action_hint='t_sell_high', scenario={'label': '上攻', 'direction': 'up', 'trigger_price': 10.2,
                                                              'trigger_condition': 'c', 'target_low': 10.4, 'target_high': 10.6,
                                                              'invalidate_price': 9.8}, triggers=['sentinel.stop_hit', 'sentinel.node_0945']),
            scenario_row(id='r2', issued_at='2026-09-21T10:00:30+08:00', price_at_issue=10.0,
                         action_hint='t_sell_high', scenario={'label': '转弱', 'direction': 'down', 'trigger_price': 9.8,
                                                              'trigger_condition': 'c', 'target_low': 9.5, 'target_high': 9.7,
                                                              'invalidate_price': 10.2}, triggers=['sentinel.stop_hit', 'sentinel.node_0945'])]
        (self.sdir / ('scenarios-%s.jsonl' % DAY)).write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')
        (self.sdir / ('reconcile-%s.json' % DAY)).write_text(json.dumps(
            {'results': [{'id': 'r1', 'outcome': 'triggered_and_hit'}, {'id': 'r2', 'outcome': 'not_triggered'}]}))
        j = sv.sentinel_payload(DAY, self.dir)['judgments'][0]
        self.assertEqual((j['symbol'], j['action_hint'], j['scenario_count']), ('sz000001', '高位做T', 2))
        self.assertIn('触及止损位 + 09:45 节点', j['source'])
        self.assertIn('上破 10.20 看 10.40–10.60', j['glance'])
        self.assertIn('下破 9.80 转弱', j['glance'])
        self.assertEqual(j['outcome'], '1触发且命中 / 1未触发')

    def test_unreconciled_scenarios_say_so_instead_of_guessing(self):
        (self.sdir / ('scenarios-%s.jsonl' % DAY)).write_text(json.dumps(scenario_row()) + '\n')
        self.assertEqual(sv.sentinel_payload(DAY, self.dir)['scenarios'][0]['outcome'], '尚未对账')

    def test_a_half_written_line_does_not_crash(self):
        (self.sdir / ('alerts-%s.jsonl' % DAY)).write_text(
            json.dumps({'at': '2026-09-21T10:00:00+08:00', 'text': 'ok', 'push': {}}) + '\n{"at": "2026-09')
        self.assertEqual([a['text'] for a in sv.sentinel_payload(DAY, self.dir)['alerts']], ['ok'])

    def test_api_route_rejects_a_traversal_day_and_falls_back_to_today(self):
        import api
        import api_pages  # noqa: F401  注册路由
        status, payload = api.dispatch('GET', '/api/sentinel', {'day': '../../etc/passwd'})
        self.assertEqual(status, 200)
        self.assertEqual(payload['day'], sv.safe_day(None))


def alert_line(at='2026-09-21T10:00:00+08:00', blocks=None, symbols=None, text='x', sent=True):
    rec = {'at': at, 'text': text, 'push': {'sent': sent}, 'symbols': symbols or [b['symbol'] for b in blocks or []]}
    if blocks is not None:
        rec['blocks'] = blocks
    return json.dumps(rec, ensure_ascii=False) + '\n'


def block(symbol='sz000001', urgent=False, text='【哨兵】测试 sz000001  现价 9.40', **kw):
    return {'symbol': symbol, 'name': '测试', 'urgent': urgent, 'text': text, 'price': '9.4', 'change_pct': '-6.0',
            'events': [{'kind': 'sentinel.stop_hit', 'severity': 'urgent', 'detail': '触及止损'}],
            'flow': {'main': -1e8}, 'book': {'outer_pct': 40.0}, **kw}


class PerSymbolTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sdir = scenario_ledger.sentinel_dir(self.dir)
        self.sdir.mkdir(parents=True, exist_ok=True)
        self.idir = Path(tempfile.mkdtemp())
        self.path = self.sdir / ('alerts-%s.jsonl' % DAY)

    def payload(self, symbol='sz000001'):
        return sv.symbol_payload(symbol, DAY, self.dir, self.idir)

    def test_only_this_symbols_blocks_come_back_newest_first_with_the_flow_at_alert_time(self):
        self.path.write_text(
            alert_line('2026-09-21T09:30:01+08:00', [block(), block('sz000002', text='【哨兵】乙 sz000002')])
            + alert_line('2026-09-21T10:08:01+08:00', [block(urgent=True, text='【紧急】测试 sz000001')], sent=False))
        a = self.payload()['alerts']
        self.assertEqual([x['time'] for x in a], ['10:08:01', '09:30:01'])
        self.assertTrue(a[0]['urgent'])
        self.assertFalse(a[0]['sent'])
        self.assertEqual(a[1]['flow'], {'main': -1e8})
        self.assertEqual(a[1]['events'][0]['detail'], '触及止损')
        self.assertEqual(len(self.payload('sz000002')['alerts']), 1)
        self.assertEqual(self.payload('sz000009')['alerts'], [])

    def test_old_records_without_blocks_fall_back_to_symbols_and_the_pushed_text(self):
        text = ('【紧急】甲 sz000001  现价 9.40（-6.00%）\n• 触及止损\n\n'
                '【哨兵】乙 sz000002  现价 10.5（+1.00%）\n• 触及目标\n\n研究参考，不构成投资建议')
        self.path.write_text(alert_line(symbols=['sz000001', 'sz000002'], text=text))
        a1, a2 = self.payload()['alerts'][0], self.payload('sz000002')['alerts'][0]
        self.assertIn('触及止损', a1['text'])
        self.assertNotIn('触及目标', a1['text'])                     # 只切出这只股票的那一段
        self.assertTrue(a1['urgent'])
        self.assertIn('触及目标', a2['text'])
        self.assertFalse(a2['urgent'])
        self.assertEqual((a1['events'], a1['flow']), ([], None))

    def test_alert_counts_per_symbol_for_the_button_badge(self):
        self.path.write_text(alert_line(blocks=[block(), block('sz000002')])
                             + alert_line(blocks=[block(urgent=True)]) + alert_line(symbols=['sz000002'], text='旧记录'))
        c = sv.alert_counts(DAY, self.dir)
        self.assertEqual(c['sz000001'], {'count': 2, 'urgent': 1})
        self.assertEqual(c['sz000002'], {'count': 2, 'urgent': 0})
        self.assertEqual(sv.alert_counts('2026-09-18', self.dir), {})

    def test_alert_counts_survive_a_corrupt_record(self):
        self.path.write_text(alert_line(blocks=[block()]) + '{"broken": tru\n' + json.dumps({'symbols': None}) + '\n')
        self.assertEqual(sv.alert_counts(DAY, self.dir)['sz000001']['count'], 1)

    def test_scenarios_are_filtered_to_the_symbol(self):
        (self.sdir / ('scenarios-%s.jsonl' % DAY)).write_text(
            json.dumps(scenario_row()) + '\n' + json.dumps(scenario_row(id='r2', symbol='sz000002')) + '\n')
        self.assertEqual([s['symbol'] for s in self.payload()['scenarios']], ['sz000001'])

    def test_judgments_are_filtered_to_the_symbol(self):
        (self.sdir / ('scenarios-%s.jsonl' % DAY)).write_text(
            json.dumps(scenario_row()) + '\n' + json.dumps(scenario_row(id='r2', symbol='sz000002')) + '\n')
        self.assertEqual([s['symbol'] for s in self.payload()['judgments']], ['sz000001'])


class CollectionAndFlowTableTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sdir = scenario_ledger.sentinel_dir(self.dir)
        self.sdir.mkdir(parents=True, exist_ok=True)
        self.idir = Path(tempfile.mkdtemp())

    def write(self, name, rows):
        (self.idir / name).write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

    def test_recorded_flow_table_is_every_half_hour_plus_the_last_row_with_the_outer_pct_then(self):
        flows = [{'at': '2026-09-21T%s:15+08:00' % t, 'symbol': 'sz000001', 'as_of': t.replace(':', ''), 'main': i * 1e6,
                  'xlarge': 0.0, 'large': 0.0, 'mid': 0.0, 'small': 0.0}
                 for i, t in enumerate(('09:55', '10:00', '10:05', '10:30', '10:35'))]
        flows.append({'at': '2026-09-21T10:40:15+08:00', 'symbol': 'sz000001', 'error': '限流'})
        self.write('flow-%s.jsonl' % DAY, flows)
        self.write('bars-%s.jsonl' % DAY, [
            {'at': '2026-09-21T10:00:01+08:00', 'symbol': 'sz000001', 'outer_vol': '60', 'inner_vol': '40'},
            {'at': '2026-09-21T10:30:01+08:00', 'symbol': 'sz000001', 'outer_vol': '30', 'inner_vol': '70'},
            {'at': '2026-09-21T10:30:01+08:00', 'symbol': 'sz000002', 'outer_vol': '99', 'inner_vol': '1'}])
        t = sv.recorded_table('sz000001', DAY, self.idir)
        self.assertEqual([r['t'] for r in t], ['10:00', '10:30', '10:35'])        # 出错的那条不算；最后一行保留
        self.assertEqual([r['outer_pct'] for r in t], [60.0, 30.0, 30.0])          # 只取该时刻及之前的最近一条
        self.assertEqual(sv.recorded_table('sz000009', DAY, self.idir), [])

    def test_collection_counts_ticks_flow_runs_and_this_symbols_observations(self):
        ticks = [{'at': '2026-09-21T10:0%d:00+08:00' % i, 'ran': True} for i in range(3)]
        ticks.append({'at': '2026-09-21T10:03:00+08:00', 'ran': False, 'skipped': '上一轮仍在运行'})
        self.write('ticks-%s.jsonl' % DAY, ticks)
        (self.idir / ('state-%s.json' % DAY)).write_text(json.dumps(
            {'gaps': [], 'extremes': {'sz000001': {'n': 3}}}))
        self.write('flow-%s.jsonl' % DAY, [{'at': 'a1', 'symbol': 'sz000001', 'main': 1.0},
                                           {'at': 'a1', 'symbol': 'sz000002', 'error': '限流'}])
        c = sv.collection(DAY, 'sz000001', self.idir)
        self.assertEqual((c['ticks'], c['observations'], c['flow_ok'], c['flow_error'], c['flow_runs']), (3, 3, 1, 0, 1))
        self.assertEqual(c['skips'], [{'reason': '上一轮仍在运行', 'count': 1}])
        self.assertEqual(sv.collection(DAY, 'sz000002', self.idir)['flow_last_error'], '限流')
        whole = sv.collection(DAY, None, self.idir)
        self.assertEqual((whole['ticks'], whole['symbols']), (3, 1))

    def test_day_payload_includes_collection_and_never_fails_because_of_it(self):
        p = sv.sentinel_payload('2026-09-18', self.dir, self.idir)
        self.assertEqual((p['collection']['ticks'], p['collection']['expected']), (0, 243))
        with patch.object(sv, 'collection', side_effect=OSError('disk')):
            self.assertIsNone(sv.sentinel_payload(DAY, self.dir, self.idir)['collection'])


if __name__ == '__main__':
    unittest.main()
