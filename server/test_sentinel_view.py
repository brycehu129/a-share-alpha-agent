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


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sdir = scenario_ledger.sentinel_dir(self.dir)
        self.sdir.mkdir(parents=True, exist_ok=True)

    def test_empty_day_renders_without_error(self):
        html = sv.render_sentinel_page(DAY, self.dir)
        self.assertIn('这一天没有告警', html)
        self.assertIn('不构成投资建议', html)

    def test_alert_text_is_escaped(self):
        (self.sdir / ('alerts-%s.jsonl' % DAY)).write_text(json.dumps(
            {'at': '2026-09-21T10:00:00+08:00', 'text': '<script>alert(1)</script>触及止损', 'push': {'sent': True}}) + '\n')
        html = sv.render_sentinel_page(DAY, self.dir)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_scenarios_show_reconcile_outcome_and_the_honest_caveats(self):
        (self.sdir / ('scenarios-%s.jsonl' % DAY)).write_text(json.dumps(scenario_row()) + '\n')
        (self.sdir / ('reconcile-%s.json' % DAY)).write_text(json.dumps(
            {'results': [{'id': 'r1', 'outcome': 'triggered_and_hit'}]}))
        html = sv.render_sentinel_page(DAY, self.dir)
        self.assertIn('触发且命中', html)
        self.assertIn('不是胜率', html)
        self.assertIn('不等于交易盈利', html)

    def test_unreconciled_scenarios_say_so_instead_of_guessing(self):
        (self.sdir / ('scenarios-%s.jsonl' % DAY)).write_text(json.dumps(scenario_row()) + '\n')
        self.assertIn('尚未对账', sv.render_sentinel_page(DAY, self.dir))

    def test_a_half_written_line_does_not_crash_the_page(self):
        (self.sdir / ('alerts-%s.jsonl' % DAY)).write_text(
            json.dumps({'at': '2026-09-21T10:00:00+08:00', 'text': 'ok', 'push': {}}) + '\n{"at": "2026-09')
        self.assertIn('ok', sv.render_sentinel_page(DAY, self.dir))

    def test_unsafe_chart_is_not_inlined(self):
        (self.sdir / 'charts').mkdir()
        (self.sdir / 'charts' / 'sz000001-20260921-100000.svg').write_text('<svg><script>x()</script></svg>')
        self.assertNotIn('<script>x()', sv.render_sentinel_page(DAY, self.dir))

    def test_safe_chart_is_inlined_with_the_when_it_was_drawn_caveat(self):
        (self.sdir / 'charts').mkdir()
        (self.sdir / 'charts' / 'sz000001-20260921-100000.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        html = sv.render_sentinel_page(DAY, self.dir)
        self.assertIn('<rect/>', html)
        self.assertIn('不是现在的走势', html)

    def test_page_is_wired_into_the_nav(self):
        self.assertIn('/sentinel', sv.render_sentinel_page(DAY, self.dir))


if __name__ == '__main__':
    unittest.main()
