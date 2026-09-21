import unittest

import book_verdict as bv


def quote(last=10.0, prev=10.0, high=None, low=None, ratio=None, **kw):
    q = {'symbol': 'sz000001', 'last': str(last), 'previous_close': str(prev), 'open': str(kw.pop('open', prev)),
         'high': str(high if high is not None else last), 'low': str(low if low is not None else last),
         'volume_ratio': None if ratio is None else str(ratio), 'change_pct': str(round((last / prev - 1) * 100, 2))}
    q.update(kw)
    return q


def holding(**kw):
    return {'symbol': 'sz000001', 'name': '测试', 'shares': 1000, 'cost_price': 10.0, 'stop_price': 9.5,
            'target_price': 11.0, 't_base_shares': 0, 'levels': {'stop_pct': 0.05, 'target_pct': 0.1, 'nominal': False}, **kw}


# 均线都在现价下方、没破位；20 日低点/高点离现价很远。
CALM = {'ma20': 9.0, 'ma60': 8.0, 'high20_close': 12.0, 'low20_close': 8.0}
PULLBACK = {'return2_pct': -1.0, 'ma20_deviation_pct': 3.0, 'ma5_deviation_pct': -1.0, 'return3_pct': -2.0,
            'high20_ratio': 0.9, 'ma20': 9.7, 'ma60': 9.0}
BREAKOUT = {'return3_pct': 5.0, 'high20_ratio': 1.0, 'ma5_deviation_pct': 2.0, 'return2_pct': 3.0,
            'ma20_deviation_pct': 12.0, 'ma20': 9.0, 'ma60': 8.0}
W = {'symbol': 'sz000001', 'name': '测试'}


class WatchVerdictTests(unittest.TestCase):
    def verdict(self, facts, q, limits=None, pause=None):
        return bv.watch_verdict(W, q, facts, limits or {}, pause)

    def test_buy_signal_names_the_pattern_and_the_gates_that_passed(self):
        v = self.verdict(PULLBACK, quote(last=10.0, prev=9.8))
        self.assertEqual((v['action'], v['label'], v['track']), ('buy', '具备买入信号', '回调反弹'))
        self.assertTrue(any('MA20偏离' in r for r in v['reasons']))

    def test_no_signal_says_what_is_still_missing(self):
        v = self.verdict({**PULLBACK, 'ma20_deviation_pct': 9.5}, quote(last=10.0, prev=9.8))
        self.assertEqual(v['action'], 'wait')
        self.assertIn('还差', v['reasons'][0])
        self.assertIn('MA20偏离', v['reasons'][0])

    def test_pattern_met_but_limit_up_is_blocked_with_the_reason(self):
        v = self.verdict(PULLBACK, quote(last=10.0, prev=9.8), {'at_limit_up': True})
        self.assertEqual(v['action'], 'blocked')
        self.assertIn('涨停', v['reasons'][0])

    def test_pattern_met_but_market_paused_is_blocked(self):
        v = self.verdict(PULLBACK, quote(last=10.0, prev=9.8), pause=True)
        self.assertEqual(v['action'], 'blocked')
        self.assertIn('暂停', v['reasons'][0])

    def test_missing_history_is_reported_not_disguised_as_no_signal(self):
        v = self.verdict({}, quote())
        self.assertEqual(v['action'], 'nodata')
        self.assertIn('日线不足', v['reasons'][0])

    def test_verdict_and_alert_always_agree(self):
        """结论直接来自哨兵规则的信号：同样的输入，页面显示"具备买入信号"当且仅当哨兵的 buy-signal 触发。"""
        import sentinel_rules as sr
        for facts, q in ((PULLBACK, quote(last=10.0, prev=9.8)), (BREAKOUT, quote(last=10.0, prev=9.5, ratio=2.0, open=9.6)),
                         ({**PULLBACK, 'return2_pct': 2.0}, quote(last=10.0, prev=9.8))):
            sig = next(x for x in sr.watch_signals(W, q, facts, {}) if x['key'].startswith('buy-signal'))
            self.assertEqual(self.verdict(facts, q)['action'] == 'buy', sig['active'])


class HoldingVerdictTests(unittest.TestCase):
    def verdict(self, h, q, facts=CALM, limits=None):
        return bv.holding_verdict(h, q, facts, limits or {})

    def test_quiet_holding_is_simply_held_and_shows_distance_to_the_systems_levels(self):
        v = self.verdict(holding(), quote(last=10.2))
        self.assertEqual((v['action'], v['label']), ('hold', '继续持有'))
        self.assertEqual((v['stop_price'], v['target_price']), (9.5, 11.0))
        self.assertAlmostEqual(v['to_stop_pct'], 7.37, places=2)
        self.assertAlmostEqual(v['to_target_pct'], 7.84, places=2)

    def test_hitting_the_system_stop_means_sell_everything(self):
        v = self.verdict(holding(), quote(last=9.4))
        self.assertEqual((v['action'], v['label']), ('exit', '建议彻底卖出'))
        self.assertIn('系统止损位', v['reasons'][0])

    def test_breaking_the_20d_low_on_volume_and_nearing_limit_down_also_mean_exit(self):
        self.assertEqual(self.verdict(holding(stop_price=None), quote(last=7.9, ratio=2.0))['action'], 'exit')
        self.assertEqual(self.verdict(holding(stop_price=None), quote(last=10.0), limits={'to_limit_down_pct': 0.5})['action'], 'exit')

    def test_both_averages_broken_is_exit_only_when_already_under_water(self):
        """趋势走坏 + 已经亏损才一次性清掉；还在成本之上就是"先落袋一部分"，不然一只赚着钱的票会被喊"彻底卖出"。"""
        broken = {**CALM, 'ma20': 10.5, 'ma60': 11.0}
        under = self.verdict(holding(), quote(last=9.9), facts=broken)               # 9.9 < 成本 10.0
        self.assertEqual(under['action'], 'exit')
        self.assertIn('同时跌破 MA20 与 MA60', under['reasons'][0])
        above = self.verdict(holding(), quote(last=10.3), facts=broken)               # 10.3 > 成本，且未到止盈位
        self.assertEqual((above['action'], above['label']), ('reduce', '可暂时卖出'))
        self.assertIn('仍在成本价之上', above['reasons'][0])

    def test_one_broken_average_or_the_take_profit_level_means_sell_some(self):
        below20 = self.verdict(holding(), quote(last=9.9), facts={**CALM, 'ma20': 10.5})
        self.assertEqual((below20['action'], below20['label']), ('reduce', '可暂时卖出'))
        target = self.verdict(holding(), quote(last=11.1))
        self.assertEqual(target['action'], 'reduce')
        self.assertIn('系统止盈位', target['reasons'][0])
        self.assertEqual(self.verdict(holding(), quote(last=9.6, prev=10.0, ratio=3.0))['action'], 'reduce')   # 放量下跌 -4%

    ENV_SPIKE = {'market': 0.2, 'market_name': '沪深300', 'sector': 0.3, 'sector_name': '银行', 'sector_n': 30,
                 'flow': {'main': -3e7, 'main_30m': -1e7, 'as_of': '1030'}}
    WIDE_HIGH = dict(last=10.5, prev=10.0, high=10.6, low=9.6)          # 振幅 10%，区间位置 90%，涨 5%

    def test_t_window_needs_all_three_layers(self):
        v = self.verdict_env(holding(t_base_shares=500), quote(**self.WIDE_HIGH), self.ENV_SPIKE)
        self.assertEqual((v['action'], v['label']), ('t', '具备做T条件'))
        self.assertIn('可卖老仓 500 股', v['reasons'][0])
        self.assertIn('大盘（沪深300）+0.20%', v['context'])
        self.assertIn('板块（银行，抽样 30 只中位）+0.30%', v['context'])
        self.assertIn('主力近30分钟 -1000万', v['context'])
        # 空间：今天买的没有老仓 / 振幅不够
        self.assertEqual(self.verdict_env(holding(t_base_shares=0), quote(**self.WIDE_HIGH), self.ENV_SPIKE)['action'], 'hold')
        narrow = quote(last=10.15, prev=10.0, high=10.15, low=9.95)
        self.assertEqual(self.verdict_env(holding(t_base_shares=500), narrow, self.ENV_SPIKE)['action'], 'hold')

    def verdict_env(self, h, q, env, facts=CALM, limits=None):
        return bv.holding_verdict(h, q, facts, limits or {}, lambda: env)

    def test_a_vetoing_environment_explains_why_there_is_no_t(self):
        strong = {**self.ENV_SPIKE, 'market': 1.3, 'sector': 1.6, 'flow': {'main': 5e7, 'main_30m': 2e7, 'as_of': '1030'}}
        v = self.verdict_env(holding(t_base_shares=500), quote(**self.WIDE_HIGH), strong)
        self.assertEqual(v['action'], 'hold')
        self.assertIn('处于日内高位，但大盘', v['reasons'][1])
        self.assertIn('暂不做T', v['reasons'][1])

    def test_missing_environment_is_a_caveat_not_a_silent_price_only_signal(self):
        v = bv.holding_verdict(holding(t_base_shares=500), quote(**self.WIDE_HIGH), CALM, {}, None)
        self.assertEqual(v['action'], 'hold')
        self.assertTrue(any('暂不判断' in c for c in v['caveats']))

    def test_sell_signals_outrank_the_t_window(self):
        v = self.verdict_env(holding(t_base_shares=500), quote(last=9.4, prev=10.0, high=10.0, low=9.4), self.ENV_SPIKE)
        self.assertEqual(v['action'], 'exit')

    def test_caveats_say_when_a_signal_family_is_unavailable_or_levels_are_estimated(self):
        v = self.verdict(holding(levels={'nominal': True}), quote(last=10.2), facts={})
        self.assertEqual(v['action'], 'hold')
        self.assertEqual(len(v['caveats']), 2)
        self.assertNotIn('caveats', self.verdict(holding(), quote(last=10.2)))

    def test_nothing_reads_the_old_declared_fields(self):
        v = self.verdict(holding(hold_type='long'), quote(last=9.9), facts={**CALM, 'ma20': 10.5})
        self.assertEqual(v['action'], 'reduce')                                   # "长期"不再豁免 MA20


class RulesDocTests(unittest.TestCase):
    def text(self, doc):
        out = []
        for section in ('holding', 'watch'):
            for g in doc[section]['groups']:
                out += [g['label'], g['when'], g['intro']]
                for r in g['rules']:
                    out += [r['text']] + r.get('sub', [])
        return '\n'.join(out)

    def test_every_verdict_the_page_can_show_is_explained(self):
        doc = bv.rules_doc()
        self.assertEqual({g['action'] for g in doc['holding']['groups']}, set(bv.HOLDING_LABEL) - {'nodata'})
        self.assertEqual({g['action'] for g in doc['watch']['groups']}, set(bv.WATCH_LABEL))
        self.assertEqual([g['label'] for g in doc['holding']['groups']][:2], ['建议彻底卖出', '可暂时卖出'])   # 优先级顺序

    def test_the_numbers_come_from_the_constants_the_code_actually_uses(self):
        """改了阈值，说明自动跟着变——不会出现页面写 3%、代码是 4%。"""
        import sentinel_rules as sr
        from unittest.mock import patch
        before = self.text(bv.rules_doc())
        for needle in ('量比 ≥ 1.5', '距跌停价 ≤ 1%', '× 1.5，夹在 3%–8%', '振幅 ≥ 3%', '80% 以上', '封顶 12%', '近 3 日涨幅 ≥ 3%', '0–8%'):
            self.assertIn(needle, before)
        with patch.object(sr, 'VOLUME_RATIO_BREAK', 1.7), patch.object(sr, 'T_MIN_AMPLITUDE_PCT', 4.0), \
                patch.object(sr, 'T_OUTPERFORM_PP', 2.5):
            after = self.text(bv.rules_doc())
        self.assertIn('量比 ≥ 1.7', after)
        self.assertIn('日内振幅 ≥ 4%', after)
        self.assertIn('比板块多 2.5 个点', after)
        self.assertNotIn('量比 ≥ 1.5', after)

    def test_the_full_exit_rule_set_is_written_down(self):
        text = self.text(bv.rules_doc())
        for needle in ('触及系统止损位', '放量跌破 20 日低点', '逼近跌停', '同时跌破 MA20 与 MA60，并且现价低于成本价'):
            self.assertIn(needle, text)

    def test_states_that_thresholds_are_unvalidated(self):
        self.assertIn('没有经过前瞻验证', bv.rules_doc()['disclaimer'])


class MarketPauseTests(unittest.TestCase):
    def test_reads_the_strategys_market_score_against_its_pause_line(self):
        from unittest.mock import patch
        for score, expected in ((35, True), (40, False), (70, False)):
            agent = {'screen': {'market_score': score, 'market_score_pause': 40}}
            with patch('dashboard_export.latest', return_value=(None, agent)):
                self.assertIs(bv.market_pause('.'), expected)

    def test_unreadable_or_missing_score_is_unknown_not_paused(self):
        from unittest.mock import patch
        with patch('dashboard_export.latest', return_value=(None, None)):
            self.assertIsNone(bv.market_pause('.'))
        with patch('dashboard_export.latest', side_effect=ValueError('bad hash')):
            self.assertIsNone(bv.market_pause('.'))


if __name__ == '__main__':
    unittest.main()
