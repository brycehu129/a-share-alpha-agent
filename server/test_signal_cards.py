import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import alpha_engine
import signal_cards as sc
import wecom_push
from exec_spec import build_spec
from test_engine_wiring import World, candidate, news, weekdays

NOW = datetime.fromisoformat('2026-09-21T08:45:00+08:00')


def report_with_plans(**world_kw):
    world = World(weekdays(70))
    return world.run('20260919000000-1', **world_kw)


class CardTests(unittest.TestCase):
    def test_a_card_answers_what_where_stop_target_days_size_and_news(self):
        r = report_with_plans()
        text = sc.build_message(r, NOW)
        for needle in ('sh600000', '突破', '入场', '作废', '止损', '目标', '最长持有 5 个交易日', '仓位', '公告', '盈亏平衡胜率'):
            self.assertIn(needle, text)
        f = next(f for f in r['forecasts'] if f['strategy_type'] == 'breakout')
        self.assertIn('%.2f–%.2f' % (f['plan_levels']['entry_low'], f['plan_levels']['entry_high']), text)
        self.assertIn('止损：−5.2%', text)
        self.assertIn('09:30–14:30', text)

    def test_pullback_card_says_not_to_chase_and_has_a_one_sided_zone(self):
        text = sc.build_message(report_with_plans(), NOW)
        self.assertIn('不追高', text)
        self.assertIn('MA20', text)

    def test_every_message_states_the_evidence_status_and_that_it_is_a_hypothesis(self):
        text = sc.build_message(report_with_plans(), NOW)
        self.assertIn('证据状态：前瞻样本不足', text)
        self.assertIn('研究假设', text)
        self.assertIn('不构成投资建议', text)
        r = report_with_plans()
        r['baseline'] = {'primary_result': {'verdict': 'worse', 'days': 20}, 'resolved': 40}
        self.assertIn('前瞻上策略不如随机（20 个日期组）', sc.build_message(r, NOW))
        r['baseline']['primary_result']['verdict'] = 'better'
        self.assertIn('前瞻上策略优于随机', sc.build_message(r, NOW))
        r['baseline']['primary_result']['verdict'] = 'indistinguishable'
        self.assertIn('目前无法区分', sc.build_message(r, NOW))

    def test_news_check_summary_and_removed_stocks_are_listed(self):
        def announce(symbols, since, now):
            return {s: news('block', '关于股东减持股份计划的公告') if s == 'sh600000' else news('clear') for s in symbols}
        text = sc.build_message(report_with_plans(announce_fn=announce), NOW)
        self.assertIn('夜间公告核查：查了', text)
        self.assertIn('已剔除', text)
        self.assertIn('关于股东减持股份计划', text)

    def test_clear_with_new_items_lists_them_and_clear_without_says_no_announcements(self):
        def announce(symbols, since, now):
            return {s: {'status': 'clear', 'items': [{'time': '2026-09-19T20:00:00+08:00', 'title': '某某:关于变更审计机构的公告',
                                                      'level': 'info', 'reason': '', 'columns': []}], 'error': None} for s in symbols}
        text = sc.build_message(report_with_plans(announce_fn=announce), NOW)
        self.assertIn('夜间无高风险公告，新发布的公告供参考：', text)
        self.assertIn('关于变更审计机构的公告', text)
        empty = sc.build_message(report_with_plans(announce_fn=lambda s, a, b: {x: news('clear') for x in s}), NOW)
        self.assertIn('（也没有新公告）', empty)

    def test_flagged_announcement_titles_are_shown_on_the_card(self):
        def announce(symbols, since, now):
            return {s: news('flag', '收到深交所问询函') for s in symbols}
        text = sc.build_message(report_with_plans(announce_fn=announce), NOW)
        self.assertIn('夜间有需留意的公告', text)
        self.assertIn('收到深交所问询函', text)

    def test_unverified_news_is_never_presented_as_clear(self):
        def announce(symbols, since, now):
            return {s: news('unverified') for s in symbols}
        text = sc.build_message(report_with_plans(announce_fn=announce), NOW)
        self.assertIn('公告未核验', text)
        self.assertNotIn('夜间无高风险公告', text)

    def test_only_todays_new_tradable_plans_get_cards(self):
        r = report_with_plans()
        r['new_forecast_ids'] = r['new_forecast_ids'][:1]                 # 只有一条是这一轮新冻结的
        text = sc.build_message(r, NOW)
        self.assertEqual(text.count('入场：'), 1)

    def test_no_tradable_plan_says_so_with_reasons(self):
        def raw(history, codes, now, cutoff=None):
            return {c: {'bars': [{'date': cutoff, 'open': '10', 'high': '10', 'low': '10', 'close': '10', 'volume_raw': '1'}]} for c in codes}
        r = report_with_plans(candidates=([candidate('sh600000', 'breakout', 60)], []))
        for f in r['forecasts']:
            f['paper_eligible'] = False
            f['plan_reasons'] = ['市场评分低于40']
        text = sc.build_message(r, NOW)
        self.assertIn('今天没有允许成交的计划', text)
        self.assertIn('市场评分低于40', text)

    def test_sizing_line_names_the_binding_limit(self):
        r = report_with_plans()
        self.assertIn('被单只上限 30% 卡住，风险预算本来允许', sc.build_message(r, NOW))
        for f in r['forecasts']:                                       # 换成高波动：止损 8%，风险预算先到
            f['exec_spec'] = build_spec('breakout', atr_pct=0.08)
            f['plan_levels'] = alpha_engine.plan_levels(f['exec_spec'], f['reference_price'], f['policy'])
        text = sc.build_message(r, NOW)
        self.assertIn('被单笔风险预算卡住', text)


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = patch.dict(os.environ, {'CONFIG_PATH': os.path.join(self.tmp, 'config.json')})
        env.start()
        self.addCleanup(env.stop)

    def test_no_webhook_means_a_clear_message_not_an_exception(self):
        self.assertIn('未配置', sc.push(report_with_plans()))

    def test_message_is_chunked_within_the_wecom_limit_and_sent(self):
        wecom_push.save_config(os.environ['CONFIG_PATH'], wecom_push.WEBHOOK_PREFIX + '?key=abc')
        sent = []
        with patch('wecom_push.send_wecom_message', side_effect=lambda url, text: sent.append(text)):
            r = report_with_plans(candidates=([candidate('sh6000%02d' % i, 'breakout', 90 - i) for i in range(3)],
                                              [candidate('sh6100%02d' % i, 'pullback', 80 - i) for i in range(3)]))
            result = sc.push(r)
        self.assertIn('已推送', result)
        self.assertTrue(sent)
        self.assertTrue(all(len(t.encode('utf-8')) <= 2000 for t in sent))


if __name__ == '__main__':
    unittest.main()
