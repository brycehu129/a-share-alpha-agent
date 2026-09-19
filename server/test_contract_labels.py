import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import contract_labels as cl
from alpha_engine import immutable
from exec_spec import ROUND_TRIP_COST_PCT
from spec_fixtures import build_spec      # 固定数字的冻结规格（判定逻辑与 ATR 参数无关），见 spec_fixtures.py
from tushare_sync import read

BO = build_spec('breakout')
PB = build_spec('pullback')


def bar(o, h=None, l=None, c=None):
    h = max(o, h or o)
    l = min(o, l or o)
    return {'open': str(o), 'high': str(h), 'low': str(l), 'close': str(o if c is None else c)}


class BreakoutEntryTests(unittest.TestCase):
    """ref=10 → 确认线 10.05，追高上限 10.30，作废线 9.70。"""

    def entry(self, **kw):
        return cl.simulate_entry(BO['entry'], 10.0, bar(**kw), None)

    def test_open_inside_the_zone_fills_at_the_open(self):
        e = self.entry(o=10.1, h=10.5, l=10.0)
        self.assertEqual((e['state'], e['fill'], e['fill_kind']), ('filled', 10.1, 'open'))

    def test_gap_above_the_ceiling_is_a_chase_unless_price_comes_back(self):
        e = self.entry(o=10.5, h=10.8, l=10.4)
        self.assertEqual((e['state'], e['reason']), ('not_filled', 'chase'))
        self.assertAlmostEqual(e['near_miss_pct'], (10.4 / 10.3 - 1) * 100, places=2)
        back = self.entry(o=10.5, h=10.8, l=10.25)
        self.assertEqual((back['state'], back['fill'], back['fill_kind']), ('filled', 10.3, 'level'))

    def test_confirmation_crossed_fills_at_the_confirm_level_not_the_high(self):
        e = self.entry(o=10.0, h=10.4, l=9.95)
        self.assertEqual((e['state'], e['fill'], e['fill_kind']), ('filled', 10.05, 'level'))

    def test_never_confirmed_records_how_close_it_got(self):
        e = self.entry(o=10.0, h=10.04, l=9.9)
        self.assertEqual((e['state'], e['reason']), ('not_filled', 'never_confirmed'))
        self.assertAlmostEqual(e['near_miss_pct'], (10.05 / 10.04 - 1) * 100, places=2)

    def test_crossing_both_confirm_and_void_in_one_bar_is_ambiguous_not_guessed(self):
        self.assertEqual(self.entry(o=10.0, h=10.06, l=9.69)['state'], 'ambiguous')

    def test_void_before_confirm_and_void_at_open(self):
        self.assertEqual(self.entry(o=10.0, h=10.04, l=9.69)['reason'], 'void_before_confirm')
        self.assertEqual(self.entry(o=9.6, h=10.5, l=9.5)['reason'], 'void_at_open')


class PullbackEntryTests(unittest.TestCase):
    """ref=10 → 上限 10.10，作废线 9.70，且不能跌破 MA20。"""

    def entry(self, ma20=9.9, **kw):
        return cl.simulate_entry(PB['entry'], 10.0, bar(**kw), ma20)

    def test_fills_at_open_when_within_the_no_chase_limit(self):
        e = self.entry(o=10.0, h=10.2, l=9.95)
        self.assertEqual((e['state'], e['fill'], e['fill_kind']), ('filled', 10.0, 'open'))

    def test_open_below_ma20_is_a_void(self):
        self.assertEqual(self.entry(o=9.8, h=10.0, l=9.7)['state'], 'voided')

    def test_gap_up_fills_only_if_price_falls_back_to_the_limit(self):
        e = self.entry(o=10.3, h=10.4, l=10.05)
        self.assertEqual((e['state'], e['fill']), ('filled', 10.1))
        self.assertEqual(self.entry(o=10.3, h=10.4, l=10.2)['reason'], 'chase')

    def test_unverifiable_or_unreachable_ma20_never_fills(self):
        self.assertEqual(self.entry(ma20=None, o=10.0)['reason'], 'ma20_unavailable')
        self.assertEqual(self.entry(ma20=10.5, o=10.0)['reason'], 'zone_empty')


class ExitTests(unittest.TestCase):
    """突破：止损 2.5%、止盈 7%、day1 规则、持有 3 天、保本武装 3%。fill=10 → 止损 9.75、止盈 10.7、保本武装 10.3。"""
    X = BO['exit']
    B0 = bar(10, 10.2, 9.9, 10.1)

    def pair(self, later, bar0=None, fill=10.0, kind='open', ma20=None, exit_spec=None):
        return cl._exit_pair(exit_spec or self.X, fill, kind, bar0 or self.B0, later, ma20)

    def quiet(self, n=3):
        return [bar(10.1, 10.2, 10.0, 10.1) for _ in range(n)]

    def test_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        p = self.pair([bar(9.6, 9.7, 9.5)])
        self.assertEqual((p['pess']['reason'], p['pess']['price']), ('stop', 9.6))
        self.assertFalse(p['ambiguous'])

    def test_intraday_stop_fills_at_the_stop_level(self):
        p = self.pair([bar(10.0, 10.1, 9.7, 9.8)])
        self.assertEqual((p['pess']['reason'], p['pess']['price']), ('stop', 9.75))
        self.assertAlmostEqual(p['pess']['net_pct'], -2.5 - ROUND_TRIP_COST_PCT, places=3)

    def test_stop_and_target_in_one_bar_gives_a_range_and_says_it_is_ambiguous(self):
        p = self.pair([bar(10.0, 10.8, 9.7, 10.0)])
        self.assertEqual((p['pess']['reason'], p['opt']['reason']), ('stop', 'target'))
        self.assertTrue(p['ambiguous'])
        self.assertLess(p['pess']['net_pct'], 0)
        self.assertGreater(p['opt']['net_pct'], 0)

    def test_gap_up_through_the_target_fills_at_the_open(self):
        p = self.pair([bar(10.8, 10.9, 10.7)])
        self.assertEqual((p['pess']['reason'], p['pess']['price']), ('target', 10.8))
        self.assertFalse(p['ambiguous'])

    def test_day1_rule_exits_at_the_next_open_when_the_entry_day_closed_at_or_below_entry(self):
        p = self.pair(self.quiet(), bar0=bar(10, 10.2, 9.9, 9.95))
        self.assertEqual((p['pess']['reason'], p['pess']['k']), ('time_stop_day1', 1))
        self.assertEqual(self.pair(self.quiet())['pess']['reason'], 'hold_expiry')     # 收盘 10.1 > 10：不触发

    def test_holds_three_sessions_then_exits_at_the_open_of_the_next(self):
        p = self.pair(self.quiet(3))
        self.assertEqual((p['pess']['reason'], p['pess']['k'], p['pess']['price']), ('hold_expiry', 3, 10.1))

    def test_unfinished_trades_are_pending_not_guessed(self):
        self.assertIsNone(self.pair(self.quiet(2)))
        self.assertIsNone(self.pair([]))

    def test_the_entry_day_itself_can_never_stop_you_out(self):
        """T+1：入场当天的低点再深也不触发止损。"""
        p = self.pair(self.quiet(), bar0=bar(10, 10.2, 8.0, 10.1))
        self.assertEqual(p['pess']['reason'], 'hold_expiry')

    def test_breakeven_stop_after_arming_on_the_entry_day(self):
        b0 = bar(10, 10.35, 9.95, 10.3)                 # 入场当天冲到 10.35 ≥ 武装线 10.3
        p = self.pair([bar(10.2, 10.25, 10.02, 10.1)] + self.quiet(2), bar0=b0)
        self.assertEqual(p['pess']['reason'], 'breakeven_stop')
        self.assertAlmostEqual(p['pess']['price'], 10 * (1 + ROUND_TRIP_COST_PCT / 100), places=3)

    def test_level_fills_only_count_entry_day_arming_in_one_of_the_two_orderings(self):
        """触发价位成交的先后不明：入场当天的高点算不算"入场之后"，两种推演结果不同——保本止损一旦武装就会更早在保本价离场。"""
        b0 = bar(10, 10.35, 9.95, 10.3)
        p = self.pair([bar(10.2, 10.25, 10.02, 10.1)] + self.quiet(2), bar0=b0, kind='level')
        self.assertEqual({p['pess']['reason'], p['opt']['reason']}, {'breakeven_stop', 'hold_expiry'})
        self.assertTrue(p['ambiguous'])

    def test_pessimistic_is_always_the_lower_result_whatever_the_mode_that_produced_it(self):
        """"悲观/乐观"是结果的上下界，不是推演模式的名字：乐观推演里先武装保本反而会更早离场、净收益更低，此时它就是下界。"""
        b0 = bar(10, 10.35, 9.95, 10.3)
        p = self.pair([bar(10.2, 10.25, 10.02, 10.1)] + self.quiet(2), bar0=b0, kind='level')
        self.assertLessEqual(p['pess']['net_pct'], p['opt']['net_pct'])
        self.assertEqual(p['pess']['reason'], 'breakeven_stop')                # 保本离场（≈0）比持有到期（+1%）低
        self.assertEqual(p['opt']['reason'], 'hold_expiry')
        for later in ([bar(10.0, 10.8, 9.7, 10.0)], [bar(9.6, 9.7, 9.5)], [bar(10.8, 10.9, 10.7)]):
            q = self.pair(later)
            self.assertLessEqual(q['pess']['net_pct'], q['opt']['net_pct'])

    def test_pullback_stop_is_the_higher_of_percentage_and_ma20(self):
        x = PB['exit']
        p = self.pair([bar(10.0, 10.1, 9.85, 9.9)], exit_spec=x, ma20=9.9)
        self.assertEqual((p['pess']['reason'], p['pess']['price']), ('stop', 9.9))       # 不是 9.7
        p2 = self.pair([bar(10.0, 10.1, 9.85, 9.9)] + self.quiet(2), exit_spec=x, ma20=9.0)
        self.assertNotEqual(p2['pess']['reason'], 'stop')

    def test_net_return_deducts_the_round_trip_cost(self):
        self.assertAlmostEqual(cl._net(10, 10.7), 7 - ROUND_TRIP_COST_PCT, places=3)


DATES = ['2026-09-%02d' % d for d in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 21, 22, 23, 24, 25, 28, 29, 30)]


def history_bars(as_of, closes=10.0, extra=None):
    """as_of 之前 20 根都收在 closes；extra = {日期: bar}"""
    bars = {}
    for d in DATES:
        if d <= as_of:
            bars[d] = bar(closes)
    bars.update(extra or {})
    return bars


def forecast(fid='select-0.4-2026-09-18-sh600000', track='breakout', spec=None, **kw):
    f = {'id': fid, 'symbol': fid.rsplit('-', 1)[1], 'strategy_type': track, 'as_of': '2026-09-18',
         'eligible_from': '2026-09-19', 'reference_price': 10.0, 'rank': 1, 'archive_only': False,
         'selection_version': 'select-0.4', 'execution_version': 'exec-0.2',
         'execution_mode': 'conditional-intraday-v1', 'exec_spec': spec or build_spec(track)}
    f.update(kw)
    return f


class LabelPlanTests(unittest.TestCase):
    def bars(self, after):
        return history_bars('2026-09-18', extra=dict(zip(DATES[DATES.index('2026-09-18') + 1:], after)))

    def test_complete_breakout_produces_all_three_layers(self):
        after = [bar(10.1, 10.3, 10.0, 10.2)] + [bar(10.2, 10.3, 10.1, 10.2)] * 3
        r = cl.label_plan(forecast(), self.bars(after), DATES)
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['entry_day'], '2026-09-21')
        self.assertEqual(r['contract']['entry']['state'], 'filled')
        self.assertEqual(r['contract']['exit']['pess']['reason'], 'hold_expiry')
        self.assertEqual(r['counterfactual']['fill'], 10.1)
        self.assertIn('limitations', r)
        self.assertEqual(r['exec_spec_revision'], 0)

    def test_untriggered_plan_still_gets_a_counterfactual(self):
        after = [bar(10.0, 10.02, 9.9, 9.95)] + [bar(9.95, 10.0, 9.9, 9.95)] * 3
        r = cl.label_plan(forecast(), self.bars(after), DATES)
        self.assertEqual(r['contract']['entry']['state'], 'not_filled')
        self.assertNotIn('exit', r['contract'])
        self.assertIn('net_pct', r['counterfactual']['exit']['pess'])

    def test_pending_until_the_holding_period_has_played_out(self):
        after = [bar(10.1, 10.3, 10.0, 10.2)] + [bar(10.2, 10.3, 10.1, 10.2)] * 2      # 只走了 D0 + 2 天
        self.assertIsNone(cl.label_plan(forecast(), self.bars(after), DATES))
        self.assertIsNone(cl.label_plan(forecast(), self.bars([]), DATES))

    def test_a_missing_day_stops_the_walk_instead_of_skipping_over_it(self):
        bars = self.bars([bar(10.1, 10.3, 10.0, 10.2)] + [bar(10.2, 10.3, 10.1, 10.2)] * 3)
        del bars['2026-09-22']
        self.assertIsNone(cl.label_plan(forecast(), bars, DATES))

    def test_a_gap_followed_by_a_crash_does_not_get_mistaken_for_the_first_day_after_entry(self):
        """缺了 D1、D2 大跌：跳过缺口的话 D2 会被当成"入场后第 1 天"直接判成止损退出。"""
        bars = self.bars([bar(10.1, 10.3, 10.0, 10.2), bar(10.2, 10.3, 10.1, 10.2), bar(9.0, 9.1, 8.9, 9.0), bar(9.0)])
        del bars['2026-09-22']
        self.assertIsNone(cl.label_plan(forecast(), bars, DATES))

    def test_adjustment_drift_is_unusable_not_forced(self):
        after = [bar(10.1, 10.3, 10.0, 10.2)] + [bar(10.2, 10.3, 10.1, 10.2)] * 3
        r = cl.label_plan(forecast(reference_price=11.0), self.bars(after), DATES)
        self.assertEqual(r['status'], 'unusable')
        self.assertIn('除权', r['reason'])

    def test_levels_are_relative_to_the_series_close_not_the_raw_reference(self):
        """前复权序列和未复权参考价差在容差内时，以序列收盘为基准。"""
        after = [bar(10.1, 10.3, 10.0, 10.2)] + [bar(10.2, 10.3, 10.1, 10.2)] * 3
        r = cl.label_plan(forecast(reference_price=10.03), self.bars(after), DATES)
        self.assertEqual(r['ref_close'], 10.0)

    def test_pullback_needs_twenty_closes_for_ma20(self):
        after = [bar(10.0, 10.1, 9.95, 10.0)] * 4
        short = {d: b for d, b in self.bars(after).items() if d >= '2026-09-04'}
        r = cl.label_plan(forecast(track='pullback', fid='select-0.4-2026-09-18-sh600001'), short, DATES)
        self.assertEqual(r['contract']['entry']['reason'], 'ma20_unavailable')

    def test_plans_without_a_frozen_spec_are_skipped(self):
        self.assertIsNone(cl.label_plan({**forecast(), 'exec_spec': None}, {}, DATES))


class ResolveAllTests(unittest.TestCase):
    def setUp(self):
        self.history = Path(tempfile.mkdtemp())
        self.after = [bar(10.1, 10.3, 10.0, 10.2)] + [bar(10.2, 10.3, 10.1, 10.2)] * 3
        self.series = {'sh600000': list(self.bars().values())}
        self.now = datetime.fromisoformat('2026-09-25T15:35:00+08:00')

    def bars(self):
        b = history_bars('2026-09-18', extra=dict(zip(DATES[DATES.index('2026-09-18') + 1:], self.after)))
        return {d: {**v, 'date': d} for d, v in b.items()}

    def go(self, forecasts, series=None):
        bench = [{'date': d} for d in DATES]
        return cl.resolve_all(forecasts, series or self.series, bench, self.now, self.history, immutable, read)

    def test_records_are_written_once_and_read_back_unchanged(self):
        first = self.go([forecast()])
        self.assertEqual(len(first), 1)
        self.assertTrue((self.history / 'contract_labels' / (forecast()['id'] + '.json')).exists())
        changed = {'sh600000': [{**b, 'close': '99', 'open': '99', 'high': '99', 'low': '99'} for b in self.series['sh600000']]}
        second = self.go([forecast()], changed)
        self.assertEqual(second[0]['contract'], first[0]['contract'])
        self.assertEqual(second[0]['generated_at'], first[0]['generated_at'])

    def test_legacy_plans_without_conditional_mode_are_ignored(self):
        self.assertEqual(self.go([forecast(execution_mode=None)]), [])
        self.assertEqual(self.go([forecast(exec_spec=None)]), [])

    def test_pending_plans_write_nothing(self):
        self.after = self.after[:2]
        self.series = {'sh600000': list(self.bars().values())}
        self.assertEqual(self.go([forecast()]), [])
        self.assertFalse((self.history / 'contract_labels').exists())


def record(i, track='breakout', tier='top3', state='filled', as_of=None, pess=1.0, opt=1.0, cf=(1.0, 1.0),
           sel='select-0.4', exe='exec-0.2', status='ok', reason='never_confirmed', ambiguous=False):
    contract = {'entry': {'state': state, 'reason': reason if state != 'filled' else 'in_zone_at_open'}}
    if state == 'filled':
        contract['exit'] = {'pess': {'reason': 'stop' if pess < 0 else 'target', 'net_pct': pess},
                            'opt': {'reason': 'target', 'net_pct': opt}, 'ambiguous': ambiguous}
    return {'plan_id': 'p%d' % i, 'symbol': 'sh6%05d' % i, 'track': track, 'rank': 1 if tier == 'top3' else 6,
            'archive_only': tier != 'top3', 'selection_version': sel, 'execution_version': exe,
            'as_of': as_of or '2026-08-%02d' % (i % 28 + 1), 'entry_day': '2026-09-%02d' % (i % 28 + 1), 'status': status, 'contract': contract,
            'counterfactual': {'exit': {'pess': {'net_pct': cf[0]}, 'opt': {'net_pct': cf[1]}}},
            'limitations': cl.LIMITATIONS}


def outcome(i, win=True, ret=1.0, day='2026-08-05'):
    return {'prediction_id': 'p%d' % i, 'horizon': 3, 'win': win, 'return_pct': ret, 'entry_day': day}


class SummaryTests(unittest.TestCase):
    def test_small_samples_show_counts_but_no_rates_or_means(self):
        recs = [record(i) for i in range(5)]
        s = cl.summarize(recs, [outcome(i) for i in range(5)], 'select-0.4')
        g = s['groups']['breakout/top3']
        self.assertEqual((g['plans'], g['contract']['filled'], g['fixed']['n']), (5, 5, 5))
        self.assertIsNone(g['contract']['result'])
        self.assertIsNone(g['counterfactual']['result'])
        self.assertIsNone(g['fixed']['win_rate_pct'])
        self.assertIsNone(g['contract']['fill_rate_pct'])
        text = '\n'.join(cl.render(s))
        self.assertIn('样本不足', text)
        self.assertNotRegex(text, r'\d%~\d')

    def test_gate_is_inclusive_and_reports_pessimistic_and_optimistic_bounds(self):
        recs = [record(i, as_of='2026-08-%02d' % (i % 15 + 1), pess=-1.0 if i % 2 else 2.0, opt=2.0) for i in range(30)]
        g = cl.summarize(recs, [], 'select-0.4')['groups']['breakout/top3']
        band = g['contract']['result']
        self.assertEqual(band['win_pess_pct'], 50.0)
        self.assertEqual(band['win_opt_pct'], 100.0)
        self.assertLess(band['mean_pess_pct'], band['mean_opt_pct'])
        recs29 = recs[:29]
        self.assertIsNone(cl.summarize(recs29, [], 'select-0.4')['groups']['breakout/top3']['contract']['result'])

    def test_cohort_gate_needs_enough_distinct_days(self):
        recs = [record(i, as_of='2026-08-%02d' % (i % 14 + 1)) for i in range(30)]           # 14 天 < 15
        self.assertIsNone(cl.summarize(recs, [], 'select-0.4')['groups']['breakout/top3']['contract']['result'])

    def test_versions_never_pool(self):
        recs = [record(i) for i in range(4)] + [record(10 + i, sel='select-0.3') for i in range(3)] \
            + [record(20 + i, exe='exec-0.2.r1') for i in range(2)]
        s = cl.summarize(recs, [], 'select-0.4', 'exec-0.2')
        self.assertEqual(s['groups']['breakout/top3']['plans'], 4)
        self.assertEqual(s['other_versions'], 5)

    def test_unusable_records_are_counted_not_used(self):
        recs = [record(1), record(2, status='unusable')]
        s = cl.summarize(recs, [], 'select-0.4')
        self.assertEqual((s['unusable'], s['groups']['breakout/top3']['plans']), (1, 1))

    def test_tiers_and_tracks_are_separate_groups(self):
        recs = [record(1), record(2, tier='rank4_10'), record(3, track='pullback')]
        self.assertEqual(sorted(cl.summarize(recs, [], 'select-0.4')['groups']),
                         ['breakout/rank4_10', 'breakout/top3', 'pullback/top3'])

    def test_untriggered_plans_feed_the_too_strict_question(self):
        recs = [record(i, state='not_filled', as_of='2026-08-%02d' % (i % 15 + 1), cf=(3.0, 4.0)) for i in range(30)]
        g = cl.summarize(recs, [], 'select-0.4')['groups']['breakout/top3']
        self.assertEqual(g['contract']['filled'], 0)
        self.assertEqual(g['counterfactual']['untriggered_n'], 30)
        self.assertEqual(g['counterfactual']['untriggered_result']['mean_pess_pct'], 3.0)
        self.assertEqual(g['contract']['not_triggered_reasons'], {'never_confirmed': 30})

    def test_ambiguous_entries_are_excluded_from_fill_statistics(self):
        recs = [record(1), record(2, state='ambiguous', reason='crossed_both_confirm_and_void')]
        g = cl.summarize(recs, [], 'select-0.4')['groups']['breakout/top3']
        self.assertEqual((g['contract']['filled'], g['contract']['not_triggered'], g['contract']['ambiguous_entry']), (1, 0, 1))

    def test_render_with_no_matured_labels_says_so(self):
        text = '\n'.join(cl.render(cl.summarize([], [], 'select-0.4')))
        self.assertIn('还没有到期', text)
        self.assertIn('研究标签', text)


if __name__ == '__main__':
    unittest.main()
