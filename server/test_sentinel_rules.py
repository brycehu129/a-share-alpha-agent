import unittest

import sentinel_rules as sr


def quote(last=10.0, prev=10.0, high=None, low=None, ratio=None, change=None, **kw):
    q = {'symbol': 'sz000001', 'last': str(last), 'previous_close': str(prev),
         'high': str(high if high is not None else last), 'low': str(low if low is not None else last),
         'volume_ratio': None if ratio is None else str(ratio),
         'change_pct': str(change if change is not None else round((last / prev - 1) * 100, 2))}
    q.update(kw)
    return q


def holding(**kw):
    return {'symbol': 'sz000001', 'name': '测试', 'shares': 1000, 'cost_price': 10.0, **kw}


def active(signals):
    return {x['key'].split(':')[0] if not x['key'].startswith('node') else x['key'].rsplit(':', 1)[0]
            for x in signals if x['active']}


def by_key(signals, prefix):
    return next((x for x in signals if x['key'].startswith(prefix)), None)


FACTS = {'ma20': 10.5, 'ma60': 11.0, 'high20_close': 12.0, 'low20_close': 9.0}


class HoldingSignalTests(unittest.TestCase):
    def sigs(self, h, q, facts=FACTS, limits=None):
        return sr.holding_signals(h, q, facts, limits or {})

    def test_below_cost_only_when_actually_below(self):
        self.assertIn('below-cost', active(self.sigs(holding(), quote(last=9.9))))
        self.assertNotIn('below-cost', active(self.sigs(holding(), quote(last=10.1))))

    def test_trapped_holdings_get_back_to_cost_instead_of_below_cost(self):
        """"套牢待解"的持仓低于成本价是常态，每天推"跌破成本价"只会让人不再看；
        真正的事件是"回到成本价"。"""
        h = holding(hold_type='trapped')
        under = self.sigs(h, quote(last=8.0))
        self.assertIsNone(by_key(under, 'below-cost'))
        self.assertFalse(by_key(under, 'back-to-cost')['active'])
        self.assertTrue(by_key(self.sigs(h, quote(last=10.05)), 'back-to-cost')['active'])

    def test_stop_and_target_only_exist_if_the_user_declared_them(self):
        """系统不知道你为什么买，就推导不出什么时候该卖——不填就不触发，绝不代填默认值。"""
        none = self.sigs(holding(), quote(last=5.0))
        self.assertIsNone(by_key(none, 'stop'))
        self.assertIsNone(by_key(none, 'target'))
        declared = self.sigs(holding(stop_price=9.5, target_price=12.0), quote(last=9.4))
        self.assertTrue(by_key(declared, 'stop')['active'])
        self.assertEqual(by_key(declared, 'stop')['severity'], 'urgent')
        self.assertEqual(by_key(declared, 'stop')['level'], {'price': 9.5, 'direction': 'down'})
        self.assertFalse(by_key(declared, 'target')['active'])
        self.assertTrue(by_key(self.sigs(holding(target_price=12.0), quote(last=12.1)), 'target')['active'])

    def test_levels_you_set_yourself_have_a_long_rearm_so_hovering_does_not_spam(self):
        """回放里茅台在止损位 1250 附近磨蹭，一天触发了 4 次紧急推送——都是真实穿越，但已是刷屏。"""
        s = self.sigs(holding(stop_price=9.5, target_price=12.0), quote(last=9.4))
        for prefix in ('stop', 'target', 'below-cost'):
            self.assertEqual(by_key(s, prefix)['rearm_s'], sr.LEVEL_REARM_S, prefix)
        self.assertGreaterEqual(sr.LEVEL_REARM_S, 1800)

    def test_touching_the_stop_exactly_counts(self):
        self.assertTrue(by_key(self.sigs(holding(stop_price=9.5), quote(last=9.5)), 'stop')['active'])

    def test_long_term_holders_do_not_get_ma20_noise_but_do_get_ma60(self):
        long_ = self.sigs(holding(hold_type='long'), quote(last=10.2))
        self.assertIsNone(by_key(long_, 'ma20-break'))
        self.assertTrue(by_key(long_, 'ma60-break')['active'])            # 10.2 < MA60 11.0
        self.assertTrue(by_key(self.sigs(holding(hold_type='swing'), quote(last=10.2)), 'ma20-break')['active'])

    def test_ma60_signal_is_absent_when_history_is_too_short(self):
        self.assertIsNone(by_key(self.sigs(holding(), quote(last=9), facts={'ma20': 10.5}), 'ma60-break'))

    def test_state_signals_carry_across_days_event_signals_do_not(self):
        s = self.sigs(holding(stop_price=9.5), quote(last=9.0), limits={'to_limit_down_pct': 0.5})
        for prefix in ('below-cost', 'stop', 'ma20-break', 'ma60-break'):
            self.assertTrue(by_key(s, prefix)['carry'], prefix)
        for prefix in ('near-limit-down', 'vol-surge-down', 'low20-break'):
            self.assertFalse(by_key(s, prefix)['carry'], prefix)

    def test_heavy_volume_selloff(self):
        s = self.sigs(holding(), quote(last=9.6, ratio=3.0, change=-4.0))
        self.assertTrue(by_key(s, 'vol-surge-down')['active'])
        self.assertFalse(by_key(self.sigs(holding(), quote(last=9.6, ratio=3.0, change=-1.0)), 'vol-surge-down')['active'])
        self.assertFalse(by_key(self.sigs(holding(), quote(last=9.6, ratio=1.2, change=-4.0)), 'vol-surge-down')['active'])

    def test_break_below_20d_low_needs_volume(self):
        self.assertTrue(by_key(self.sigs(holding(), quote(last=8.9, ratio=2.0)), 'low20-break')['active'])
        self.assertFalse(by_key(self.sigs(holding(), quote(last=8.9, ratio=1.0)), 'low20-break')['active'])

    def test_missing_volume_ratio_never_triggers_volume_signals(self):
        s = self.sigs(holding(), quote(last=8.0, ratio=None, change=-8))
        self.assertFalse(by_key(s, 'vol-surge-down')['active'])
        self.assertFalse(by_key(s, 'low20-break')['active'])

    def test_limit_proximity(self):
        s = self.sigs(holding(), quote(), limits={'to_limit_down_pct': 0.8, 'to_limit_up_pct': 20})
        self.assertTrue(by_key(s, 'near-limit-down')['active'])
        self.assertFalse(by_key(s, 'near-limit-up')['active'])


class WatchSignalTests(unittest.TestCase):
    def test_buy_zone_only_when_the_user_declared_one(self):
        w = {'symbol': 'sz000001', 'name': '测试'}
        self.assertIsNone(by_key(sr.watch_signals(w, quote(last=10), FACTS, {}), 'buy-zone'))
        w.update(buy_low=9.5, buy_high=10.5)
        self.assertTrue(by_key(sr.watch_signals(w, quote(last=10), FACTS, {}), 'buy-zone')['active'])
        self.assertFalse(by_key(sr.watch_signals(w, quote(last=10.6), FACTS, {}), 'buy-zone')['active'])
        self.assertFalse(by_key(sr.watch_signals(w, quote(last=9.4), FACTS, {}), 'buy-zone')['active'])

    def test_high20_breakout_needs_volume(self):
        w = {'symbol': 'sz000001'}
        self.assertTrue(by_key(sr.watch_signals(w, quote(last=12.5, ratio=2.0), FACTS, {}), 'high20-break')['active'])
        self.assertFalse(by_key(sr.watch_signals(w, quote(last=12.5, ratio=1.0), FACTS, {}), 'high20-break')['active'])
        self.assertFalse(by_key(sr.watch_signals(w, quote(last=11.5, ratio=3.0), FACTS, {}), 'high20-break')['active'])


class NodeTests(unittest.TestCase):
    def test_node_fires_only_when_just_crossed_and_not_too_late(self):
        due = {'0945': 20, '1305': None, '1430': 900}
        out = sr.node_signals('sz000001', '测试', lambda h: due[h])
        self.assertEqual([x['key'] for x in out], ['node:0945:sz000001'])       # 1430 迟到 15 分钟，不发
        self.assertTrue(all(x['active'] for x in out))

    def test_nothing_when_no_node_is_due(self):
        self.assertEqual(sr.node_signals('sz000001', '测试', lambda h: None), [])


class DayFactsTests(unittest.TestCase):
    MINUTE = {'vwap': 10.0, 'high_close': 10.4, 'low_close': 9.7}

    def test_extremes_prefer_the_quotes_real_intraday_high_low(self):
        f = sr.day_facts(quote(last=10.3, prev=10.0, high=10.6, low=9.6), self.MINUTE)
        self.assertEqual((f['day_high'], f['day_low']), (10.6, 9.6))          # 分钟收盘价的区间更窄，不能冒充
        self.assertAlmostEqual(f['amplitude_pct'], 10.0)
        self.assertAlmostEqual(f['position_in_range'], 0.7)

    def test_flat_day_has_no_position(self):
        self.assertIsNone(sr.day_facts(quote(last=10, high=10, low=10), {'vwap': 10, 'high_close': 10, 'low_close': 10})['position_in_range'])


class SwingTTests(unittest.TestCase):
    def day(self, last, high=10.6, low=9.6, vwap=10.0):
        return sr.day_facts(quote(last=last, prev=10.0, high=high, low=low), {'vwap': vwap, 'high_close': high, 'low_close': low})

    def sigs(self, h, q, day):
        return {x['key'].split(':')[0]: x for x in sr.t_signals(h, q, day)}

    def test_sell_high_needs_base_amplitude_position_and_above_vwap(self):
        h = holding(t_base_shares=500)
        s = self.sigs(h, quote(last=10.5), self.day(10.5))
        self.assertTrue(s['t-sell-high']['active'])
        self.assertFalse(s['t-buy-low']['active'])

    def test_buy_low_mirror(self):
        s = self.sigs(holding(t_base_shares=500), quote(last=9.7), self.day(9.7))
        self.assertTrue(s['t-buy-low']['active'])
        self.assertFalse(s['t-sell-high']['active'])

    def test_no_declared_base_means_no_t_prompts_at_all(self):
        s = self.sigs(holding(t_base_shares=0), quote(last=10.5), self.day(10.5))
        self.assertFalse(s['t-sell-high']['active'] or s['t-buy-low']['active'])

    def test_small_amplitude_means_no_room_after_costs(self):
        """往返成本约 0.31%，日内振幅不足 3% 就没有做T的空间。"""
        # 现价放在区间最高点、且高于均价线：位置条件和均价线条件都满足，只有 2% 的振幅这一条能拦住它。
        # （早先夹具里现价只在区间 75% 处，本来就不够"高位"，删掉振幅检查测试也照样通过。）
        q = quote(last=10.15)
        day = self.day(10.15, high=10.15, low=9.95)
        self.assertGreaterEqual(day['position_in_range'], sr.T_SELL_POSITION)
        self.assertLess(day['amplitude_pct'], sr.T_MIN_AMPLITUDE_PCT)
        s = self.sigs(holding(t_base_shares=500), q, day)
        self.assertFalse(s['t-sell-high']['active'] or s['t-buy-low']['active'])

    def test_high_position_below_vwap_is_not_a_sell_high(self):
        s = self.sigs(holding(t_base_shares=500), quote(last=10.5), self.day(10.5, vwap=10.8))
        self.assertFalse(s['t-sell-high']['active'])

    def test_prompt_states_it_is_only_a_position_hint(self):
        s = self.sigs(holding(t_base_shares=500), quote(last=10.5), self.day(10.5))
        self.assertIn('不知道你是否已执行', s['t-sell-high']['detail'])


class SignalShapeTests(unittest.TestCase):
    def test_every_signal_is_accepted_by_the_engine(self):
        """规则输出必须能被引擎的 normalize_signal 接受，否则一条坏信号会让整个评估器被隔离。"""
        import intraday_engine as ie
        h = holding(stop_price=9.5, target_price=12, t_base_shares=500, hold_type='swing')
        q = quote(last=9.4, ratio=3.0, change=-4.0, high=10.6, low=9.3)
        day = sr.day_facts(q, {'vwap': 10.0, 'high_close': 10.6, 'low_close': 9.3})
        every = (sr.holding_signals(h, q, FACTS, {'to_limit_down_pct': 0.5, 'to_limit_up_pct': 20})
                 + sr.t_signals(h, q, day) + sr.node_signals('sz000001', '测试', lambda x: 10 if x == '0945' else None)
                 + sr.watch_signals({'symbol': 'sz000002', 'buy_low': 9, 'buy_high': 10}, q, FACTS, {}))
        self.assertGreater(len(every), 10)
        for s in every:
            ie.normalize_signal(s)
        self.assertEqual(len({s['key'] for s in every}), len(every))                 # key 不重复


if __name__ == '__main__':
    unittest.main()
