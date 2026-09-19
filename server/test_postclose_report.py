import unittest

import postclose_report as pc
import webapp_views


def report(**kw):
    base = {
        'id': '20260918160000-1', 'version': pc.VERSION, 'status': 'ready',
        'generated_at': '2026-09-18T16:00:00+08:00', 'calendar': 'open',
        'session_label': '盘后', 'issues': [],
        'market': {'indices': [{'symbol': 'sh000001', 'label': '上证指数', 'last': '3911',
                                'change_pct': '0.94', 'quote_date': '2026-09-18',
                                'timezone_verified': True}],
                   'overseas': [], 'breadth': {'note': '不可用'}, 'industries': {'rows': []},
                   'caveats': []},
        'check': {'rows': [{'symbol': 'sh600000', 'name': '测试', 'roles': ['holding'],
                            'quote': {'last': '10', 'change_pct': '1.0'},
                            'price_facts': {'ma5_deviation_pct': 1.0, 'ma20_deviation_pct': 2.0,
                                            'distance_to_high20_pct': -3.0},
                            'limit_facts': {}, 'issues': [],
                            'holding': {'shares': 100, 'cost_price': 9.0, 'market_value': 1000.0,
                                        'unrealized_pnl': 100.0, 'unrealized_pct': 11.11}}],
                  'caveats': []},
        'ai': None, 'ai_meta': {'status': 'unavailable', 'error': '未配置 ANTHROPIC_API_KEY'},
    }
    base.update(kw)
    return base


class RenderTests(unittest.TestCase):
    def test_renders_without_ai(self):
        """AI 没跑成也要出报告：规则层的浮动盈亏、门槛、入场带本身就有用。"""
        text = pc.render(report())
        self.assertIn('我的持仓', text)
        self.assertIn('浮动盈亏 100.0', text)
        self.assertIn('不构成投资建议', text)

    def test_ai_verdict_is_labelled_as_unverified(self):
        text = pc.render(report(
            ai={'market': {'tone': 'risk_on', 'summary': '放量上涨', 'key_points': ['宽度好'],
                           'tomorrow_watch': []},
                'holdings': [{'symbol': 'sh600000', 'name': '测试', 'verdict': 'hold',
                              'confidence': 3, 'reason': '仍在均线上方', 'risks': '量能不足',
                              'key_levels': {'entry_zone': '—', 'stop': '9.0', 'target': '12.0'}}],
                'candidates': [], 'data_caveats': []},
            ai_meta={'status': 'ok', 'model': 'claude-opus-5', 'effort': 'high',
                     'prompt_version': 'p1', 'input_tokens': 100, 'output_tokens': 50}))
        self.assertIn('继续持有', text)
        self.assertIn('依据强度 3/5', text)
        self.assertIn('未经任何前瞻验证', text)

    def test_out_of_band_plan_is_called_out(self):
        r = report()
        r['check']['rows'][0]['roles'] = ['candidate']
        r['check']['rows'][0]['plan'] = {'reference_price': 10.0, 'entry_low': 9.7,
                                         'entry_high': 10.3, 'gap_pct': 6.0, 'in_band': False}
        self.assertIn('已超出入场带', pc.render(r))


class PushTests(unittest.TestCase):
    def test_summary_stays_short_and_points_at_the_full_report(self):
        text = pc.summarize_for_push(report())
        self.assertIn('/postclose', text)
        self.assertIn('非投资建议', text)
        self.assertLess(len(text.encode('utf-8')), pc.WECOM_CHUNK_BYTES)

    def test_chunking_respects_the_byte_limit(self):
        chunks = pc.chunk_text('\n'.join('第%d行内容内容内容' % i for i in range(400)), limit=200)
        self.assertTrue(all(len(c.encode('utf-8')) <= 200 for c in chunks))

    def test_oversized_single_line_is_split_not_dropped(self):
        chunks = pc.chunk_text('长' * 500, limit=120)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.encode('utf-8')) <= 120 for c in chunks))

    def test_too_many_chunks_are_truncated_with_a_notice(self):
        chunks = pc.chunk_text('\n'.join('行%d' % i for i in range(5000)), limit=100, max_chunks=3)
        self.assertEqual(len(chunks), 3)
        self.assertIn('已截断', chunks[-1])


class MarkdownTests(unittest.TestCase):
    def test_report_markdown_survives_html_rendering(self):
        html = webapp_views.markdown_to_html(pc.render(report()))
        self.assertIn('<h1>', html)
        self.assertIn('<table>', html)

    def test_content_is_escaped_before_formatting(self):
        html = webapp_views.markdown_to_html('# <script>alert(1)</script>')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_table_alignment_comes_from_the_separator_row(self):
        html = webapp_views.markdown_to_html('| 指数 | 涨幅 |\n|---|---:|\n| 上证 | 1% |')
        # 右对齐只给分隔行标了 `---:` 的那一列；早先按表头文字猜会把"指数"误判。
        self.assertEqual(html.count('class="num"'), 2)
        self.assertIn('<th>指数</th>', html)


if __name__ == '__main__':
    unittest.main()
