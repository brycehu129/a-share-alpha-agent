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

    def test_stop_and_target_come_from_the_systems_levels_not_the_user(self):
        """止损/止盈位由系统按 ATR 从成本价算出（book_levels.enrich），规则只读 h 里的这两个价位。
        没有就不触发——绝不代填默认值。"""
        none = self.sigs(holding(), quote(last=5.0))
        self.assertIsNone(by_key(none, 'stop'))
        self.assertIsNone(by_key(none, 'target'))
        levels = {'stop_pct': 0.05, 'target_pct': 0.075, 'nominal': False}
        armed = holding(stop_price=9.5, target_price=12.0, levels=levels)
        hit = self.sigs(armed, quote(last=9.4))
        self.assertTrue(by_key(hit, 'stop')['active'])
        self.assertEqual(by_key(hit, 'stop')['severity'], 'urgent')
        self.assertEqual(by_key(hit, 'stop')['level'], {'price': 9.5, 'direction': 'down'})
        self.assertIn('系统止损位', by_key(hit, 'stop')['detail'])
        self.assertFalse(by_key(hit, 'target')['active'])
        self.assertTrue(by_key(self.sigs(armed, quote(last=12.1)), 'target')['active'])

    def test_estimated_levels_are_labelled_as_such(self):
        h = holding(stop_price=9.5, levels={'stop_pct': 0.05, 'nominal': True})
        self.assertIn('典型波动估算', by_key(self.sigs(h, quote(last=9.4)), 'stop')['detail'])

    def test_levels_have_a_long_rearm_so_hovering_does_not_spam(self):
        """回放里茅台在止损位 1250 附近磨蹭，一天触发了 4 次紧急推送——都是真实穿越，但已是刷屏。"""
        s = self.sigs(holding(stop_price=9.5, target_price=12.0), quote(last=9.4))
        for prefix in ('stop', 'target', 'below-cost'):
            self.assertEqual(by_key(s, prefix)['rearm_s'], sr.LEVEL_REARM_S, prefix)
        self.assertGreaterEqual(sr.LEVEL_REARM_S, 1800)

    def test_touching_the_stop_exactly_counts(self):
        self.assertTrue(by_key(self.sigs(holding(stop_price=9.5), quote(last=9.5)), 'stop')['active'])

    def test_every_holding_gets_the_ma20_and_ma60_breaks(self):
        """不再有"长期"这类自我声明来豁免 MA20：破位提示对所有持仓一视同仁。"""
        s = self.sigs(holding(), quote(last=10.2))
        self.assertTrue(by_key(s, 'ma20-break')['active'])
        self.assertTrue(by_key(s, 'ma60-break')['active'])            # 10.2 < MA20 10.5 < MA60 11.0
        self.assertIsNone(by_key(s, 'back-to-cost'))

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
    W = {'symbol': 'sz000001', 'name': '测试'}
    # 回调反弹形态：近2日不涨、偏离 MA20 在 0–8%、当日收阳。
    PULLBACK_FACTS = {'return2_pct': -1.0, 'ma20_deviation_pct': 3.0, 'ma5_deviation_pct': -1.0, 'return3_pct': -2.0,
                      'high20_ratio': 0.9, 'ma20': 9.7, 'ma60': 9.0}
    # 突破形态：近3日≥3%、接近20日新高、MA5 偏离 0–6%。
    BREAKOUT_FACTS = {'return3_pct': 5.0, 'high20_ratio': 1.0, 'ma5_deviation_pct': 2.0, 'return2_pct': 3.0,
                      'ma20_deviation_pct': 12.0, 'ma20': 9.0, 'ma60': 8.0}

    def buy(self, facts, q, limits=None, pause=None):
        return by_key(sr.watch_signals(self.W, q, facts, limits or {}, pause), 'buy-signal')

    def test_buy_signal_fires_when_the_pullback_gates_all_pass(self):
        s = self.buy(self.PULLBACK_FACTS, quote(last=10.0, prev=9.8, **{'open': '9.8'}))
        self.assertTrue(s['active'])
        self.assertIn('回调反弹', s['detail'])

    def test_buy_signal_fires_for_the_breakout_track_too(self):
        s = self.buy(self.BREAKOUT_FACTS, quote(last=10.0, prev=9.5, ratio=2.0, **{'open': '9.6'}))
        self.assertTrue(s['active'])
        self.assertIn('突破', s['detail'])

    def test_one_failed_gate_means_no_signal(self):
        facts = {**self.PULLBACK_FACTS, 'ma20_deviation_pct': 9.0}       # 偏离 MA20 超过 8%
        self.assertFalse(self.buy(facts, quote(last=10.0, prev=9.8, **{'open': '9.8'}))['active'])
        self.assertFalse(self.buy(self.PULLBACK_FACTS, quote(last=10.0, prev=10.2, **{'open': '10.2'}))['active'])   # 收阴

    def test_no_daily_history_means_no_buy_signal_at_all(self):
        """缺数据就说缺数据：没有日线算不出形态，不能当成"没信号"混过去，也不产生这条信号。"""
        self.assertIsNone(self.buy({}, quote(last=10.0)))

    def test_a_limit_up_stock_or_a_paused_market_blocks_the_signal(self):
        q = quote(last=10.0, prev=9.8, **{'open': '9.8'})
        self.assertFalse(self.buy(self.PULLBACK_FACTS, q, {'at_limit_up': True})['active'])
        self.assertFalse(self.buy(self.PULLBACK_FACTS, q, {'at_limit_down': True})['active'])
        self.assertFalse(self.buy(self.PULLBACK_FACTS, q, pause=True)['active'])
        self.assertTrue(self.buy(self.PULLBACK_FACTS, q, pause=False)['active'])
        self.assertTrue(self.buy(self.PULLBACK_FACTS, q, pause=None)['active'])      # 读不到大盘评分：不据此拦截

    def test_volume_ratio_is_reference_only_not_a_gate(self):
        """腾讯量比盘中系统性偏小，live_check 明确只作参考；这里不能把它当成买入门槛。"""
        q = quote(last=10.0, prev=9.5, ratio=0.3, **{'open': '9.6'})
        self.assertTrue(self.buy(self.BREAKOUT_FACTS, q)['active'])

    def test_nothing_reads_the_old_declared_buy_zone(self):
        w = {**self.W, 'buy_low': 9.5, 'buy_high': 10.5}
        self.assertIsNone(by_key(sr.watch_signals(w, quote(last=10), FACTS, {}), 'buy-zone'))

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


FLOW_OUT = {'main': -3e7, 'main_30m': -1e7, 'as_of': '1030'}
FLOW_IN = {'main': 5e7, 'main_30m': 2e7, 'as_of': '1030'}


def env(market=0.0, sector=0.0, flow=None):
    return {'market': market, 'market_name': '沪深300', 'sector': sector, 'sector_name': '银行', 'sector_n': 30, 'flow': flow}


class SwingTTests(unittest.TestCase):
    """做T 三层：空间（老仓+振幅）→ 位置（日内高/低位）→ 环境（大盘/板块/资金流：有否决就不做，还要至少一个支持项）。"""

    def day(self, last, high=10.6, low=9.6, vwap=10.0):
        return sr.day_facts(quote(last=last, prev=10.0, high=high, low=low), {'vwap': vwap, 'high_close': high, 'low_close': low})

    def ev(self, h, q, day, e=None, limits=None):
        return sr.t_evaluate(h, q, day, (lambda: e) if e is not None else None, limits or {})

    def sigs(self, h, q, day, e=None, limits=None):
        ev = self.ev(h, q, day, e, limits)
        return {x['key'].split(':')[0]: x for x in sr.t_signals(h, q, day, evaluation=ev)}

    HIGH = quote(last=10.5, change=5.0)          # 区间位置 90%
    LOW = quote(last=9.7, change=-3.0)           # 区间位置 10%

    def test_sell_high_when_the_stock_spikes_alone_while_its_sector_does_not(self):
        s = self.sigs(holding(t_base_shares=500), self.HIGH, self.day(10.5), env(market=0.2, sector=0.5))
        self.assertTrue(s['t-sell-high']['active'])
        self.assertIn('比板块多涨', s['t-sell-high']['detail'])
        self.assertFalse(s['t-buy-low']['active'])

    def test_sell_high_is_supported_by_money_leaving_or_a_weak_market(self):
        self.assertTrue(self.sigs(holding(t_base_shares=500), self.HIGH, self.day(10.5), env(sector=4.0, flow=FLOW_OUT))['t-sell-high']['active'])
        self.assertTrue(self.sigs(holding(t_base_shares=500), self.HIGH, self.day(10.5), env(market=-0.8, sector=4.0))['t-sell-high']['active'])

    def test_no_sell_when_market_and_sector_are_both_strong_and_money_is_still_flowing_in(self):
        """顺势上涨里先卖，多半卖飞。"""
        e = env(market=1.2, sector=1.5, flow=FLOW_IN)
        ev = self.ev(holding(t_base_shares=500), self.HIGH, self.day(10.5), e)
        self.assertFalse(ev['ok'])
        self.assertIn('同步走强', ev['veto'][0])
        self.assertFalse(self.sigs(holding(t_base_shares=500), self.HIGH, self.day(10.5), e)['t-sell-high']['active'])

    def test_strong_resonance_without_flow_data_is_treated_conservatively(self):
        ev = self.ev(holding(t_base_shares=500), self.HIGH, self.day(10.5), env(market=1.2, sector=1.5, flow=None))
        self.assertFalse(ev['ok'])
        self.assertIn('保守', ev['veto'][0])

    def test_no_sell_near_the_limit_up(self):
        ev = self.ev(holding(t_base_shares=500), self.HIGH, self.day(10.5), env(sector=0.0), limits={'to_limit_up_pct': 2.0})
        self.assertFalse(ev['ok'])
        self.assertIn('涨停', ev['veto'][0])

    def test_position_alone_is_not_enough_a_neutral_environment_gives_no_signal(self):
        """在高位、环境也没否决，但没有任何支持项（个股没有明显强于板块、资金没撤、大盘不弱）：不触发。"""
        ev = self.ev(holding(t_base_shares=500), self.HIGH, self.day(10.5), env(market=0.3, sector=4.5))
        self.assertEqual((ev['ok'], ev['veto'], ev['support']), (False, [], []))

    def test_buy_low_needs_a_market_and_sector_that_are_not_falling(self):
        s = self.sigs(holding(t_base_shares=500), self.LOW, self.day(9.7), env(market=0.1, sector=0.0, flow=FLOW_IN))
        self.assertTrue(s['t-buy-low']['active'])
        self.assertFalse(s['t-sell-high']['active'])
        self.assertIn('承接', s['t-buy-low']['detail'])

    def test_no_buy_back_into_a_systemic_selloff_or_a_collapsing_sector_or_outflow(self):
        for e, word in ((env(market=-1.8, sector=0.0), '系统性'), (env(market=0.0, sector=-2.5), '板块整体杀跌'),
                        (env(market=0.0, sector=0.0, flow=FLOW_OUT), '没有承接')):
            ev = self.ev(holding(t_base_shares=500), self.LOW, self.day(9.7), e)
            self.assertFalse(ev['ok'], e)
            self.assertIn(word, ev['veto'][0])

    def test_no_buy_near_the_limit_down(self):
        ev = self.ev(holding(t_base_shares=500), self.LOW, self.day(9.7), env(), limits={'to_limit_down_pct': 1.0})
        self.assertFalse(ev['ok'])

    def test_no_environment_data_means_no_signal_and_says_so_instead_of_falling_back_to_price_only(self):
        for fn in (None, lambda: {'market': None, 'sector': None, 'flow': None}):
            ev = sr.t_evaluate(holding(t_base_shares=500), self.HIGH, self.day(10.5), fn, {})
            self.assertFalse(ev['ok'])
            self.assertIn('暂不判断', ev['unavailable'])

    def test_environment_is_only_fetched_when_the_price_position_is_met(self):
        """环境要联网，懒取：没有底仓/振幅不够/不在高低位时一次都不该调用。"""
        calls = []
        fn = lambda: calls.append(1) or env()
        sr.t_evaluate(holding(t_base_shares=0), self.HIGH, self.day(10.5), fn, {})                         # 没有老仓
        sr.t_evaluate(holding(t_base_shares=500), quote(last=10.15), self.day(10.15, high=10.15, low=9.95), fn, {})   # 振幅 2%
        sr.t_evaluate(holding(t_base_shares=500), quote(last=10.1), self.day(10.1), fn, {})                # 区间位置 55%
        self.assertEqual(calls, [])
        sr.t_evaluate(holding(t_base_shares=500), self.HIGH, self.day(10.5), fn, {})
        self.assertEqual(calls, [1])

    def test_no_sellable_base_means_no_t_at_all(self):
        """今天没有可卖的老仓（全是今天买的，T+1）就不能先卖后买。"""
        s = self.sigs(holding(t_base_shares=0), self.HIGH, self.day(10.5), env(sector=0.0, flow=FLOW_OUT))
        self.assertFalse(s['t-sell-high']['active'] or s['t-buy-low']['active'])

    def test_small_amplitude_means_no_room_after_costs(self):
        """往返成本约 0.31%，日内振幅不足 3% 就没有做T的空间。"""
        day = self.day(10.15, high=10.15, low=9.95)
        self.assertGreaterEqual(day['position_in_range'], sr.T_SELL_POSITION)
        self.assertLess(day['amplitude_pct'], sr.T_MIN_AMPLITUDE_PCT)
        s = self.sigs(holding(t_base_shares=500), quote(last=10.15), day, env(sector=0.0, flow=FLOW_OUT))
        self.assertFalse(s['t-sell-high']['active'] or s['t-buy-low']['active'])

    def test_works_without_minute_data_so_the_page_and_the_sentinel_agree(self):
        """页面上没有分时数据；均价线有才写进说明，不参与判定。"""
        no_vwap = sr.day_facts(quote(last=10.5, prev=10.0, high=10.6, low=9.6, change=5.0))
        self.assertIsNone(no_vwap['vwap'])
        e = env(sector=0.0, flow=FLOW_OUT)
        s = self.sigs(holding(t_base_shares=500), self.HIGH, no_vwap, e)
        self.assertTrue(s['t-sell-high']['active'])
        self.assertNotIn('均价线', s['t-sell-high']['detail'])
        self.assertIn('均价线', self.sigs(holding(t_base_shares=500), self.HIGH, self.day(10.5), e)['t-sell-high']['detail'])

    def test_prompt_states_it_is_only_a_position_hint(self):
        s = self.sigs(holding(t_base_shares=500), self.HIGH, self.day(10.5), env(sector=0.0, flow=FLOW_OUT))
        self.assertIn('不知道你是否已执行', s['t-sell-high']['detail'])


class SignalShapeTests(unittest.TestCase):
    def test_every_signal_is_accepted_by_the_engine(self):
        """规则输出必须能被引擎的 normalize_signal 接受，否则一条坏信号会让整个评估器被隔离。"""
        import intraday_engine as ie
        h = holding(stop_price=9.5, target_price=12, t_base_shares=500)
        q = quote(last=9.4, ratio=3.0, change=-4.0, high=10.6, low=9.3)
        day = sr.day_facts(q, {'vwap': 10.0, 'high_close': 10.6, 'low_close': 9.3})
        every = (sr.holding_signals(h, q, FACTS, {'to_limit_down_pct': 0.5, 'to_limit_up_pct': 20})
                 + sr.t_signals(h, q, day, env_fn=lambda: env(sector=0.0, flow=FLOW_OUT)) + sr.node_signals('sz000001', '测试', lambda x: 10 if x == '0945' else None)
                 + sr.watch_signals({'symbol': 'sz000002'}, q, {**WatchSignalTests.PULLBACK_FACTS, **FACTS}, {}))
        self.assertGreater(len(every), 10)
        for s in every:
            ie.normalize_signal(s)
        self.assertEqual(len({s['key'] for s in every}), len(every))                 # key 不重复


if __name__ == '__main__':
    unittest.main()
