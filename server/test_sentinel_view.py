import json
import tempfile
import unittest
from pathlib import Path

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


class SvgSafetyTests(unittest.TestCase):
    def test_rejects_scripts_handlers_and_external_links(self):
        for bad in ('<svg><script>alert(1)</script></svg>', '<svg onload="x()"></svg>', '<svg><a href="http://x"></a></svg>',
                    '<div>not svg</div>', '<svg><image href="x"/></svg>'):
            self.assertIsNone(sv._safe_svg(bad), bad)
        self.assertIsNotNone(sv._safe_svg('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'))


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sdir = scenario_ledger.sentinel_dir(self.dir)
        self.sdir.mkdir(parents=True, exist_ok=True)

    def test_empty_day_returns_empty_lists_without_error(self):
        p = sv.sentinel_payload(DAY, self.dir)
        self.assertEqual((p['alerts'], p['scenarios'], p['charts']), ([], [], []))
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

    def test_unreconciled_scenarios_say_so_instead_of_guessing(self):
        (self.sdir / ('scenarios-%s.jsonl' % DAY)).write_text(json.dumps(scenario_row()) + '\n')
        self.assertEqual(sv.sentinel_payload(DAY, self.dir)['scenarios'][0]['outcome'], '尚未对账')

    def test_a_half_written_line_does_not_crash(self):
        (self.sdir / ('alerts-%s.jsonl' % DAY)).write_text(
            json.dumps({'at': '2026-09-21T10:00:00+08:00', 'text': 'ok', 'push': {}}) + '\n{"at": "2026-09')
        self.assertEqual([a['text'] for a in sv.sentinel_payload(DAY, self.dir)['alerts']], ['ok'])

    def test_unsafe_chart_is_not_returned(self):
        (self.sdir / 'charts').mkdir()
        (self.sdir / 'charts' / 'sz000001-20260921-100000.svg').write_text('<svg><script>x()</script></svg>')
        self.assertEqual(sv.sentinel_payload(DAY, self.dir)['charts'], [])

    def test_safe_chart_is_returned_for_inline_rendering(self):
        (self.sdir / 'charts').mkdir()
        (self.sdir / 'charts' / 'sz000001-20260921-100000.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        charts = sv.sentinel_payload(DAY, self.dir)['charts']
        self.assertEqual(len(charts), 1)
        self.assertIn('<rect/>', charts[0]['svg'])

    def test_api_route_rejects_a_traversal_day_and_falls_back_to_today(self):
        import api
        import api_pages  # noqa: F401  注册路由
        status, payload = api.dispatch('GET', '/api/sentinel', {'day': '../../etc/passwd'})
        self.assertEqual(status, 200)
        self.assertEqual(payload['day'], sv.safe_day(None))


if __name__ == '__main__':
    unittest.main()
