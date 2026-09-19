"""alpha_engine.run() 的接线测试：批准的规格修订、exec-0.3 账户快照、证据三层，是否真的进了冻结的记录和报告。

run() 依赖很多数据源，这里用真实的目录结构 + 少量替身（选股、回看校准、游资、原始价）搭一个能跑通的小环境，
保留 run() 自己的全部逻辑：冻结预测、排名、版本打标、证据三层、账户快照。以前这条链只用一次真实数据回放验证过，
改到这里悄悄坏掉没人发现。"""
import contextlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import alpha_engine
import conditional_exec as ce
import contract_labels
from collect_quotes import CST
from tushare_sync import read, save

REV = (2, {'breakout': {'exit.stop_atr_mult': 1.25}})
NOW = datetime.now(CST)


def weekdays(n, end_offset=1):
    out, d = [], NOW.date() - timedelta(days=end_offset)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def bar(day, price=10.0):
    return {'date': day, 'open': str(price), 'high': str(price * 1.01), 'low': str(price * 0.99),
            'close': str(price), 'volume_raw': '1000'}


def candidate(symbol, track, raw):
    return {'symbol': symbol, 'name': symbol, 'industry': 'x', 'score': 80, 'raw_score': raw, 'strategy_type': track,
            'liquidity_pct': 50, 'atr14': 0.035, 'probability': {'probability': None, 'n': 0, 'cohorts': 0, 'low': None, 'high': None}}


class World:
    """一个能让 run() 跑通的最小历史目录。days 是基准/个股日线的日期。"""

    def __init__(self, days):
        self.history = Path(tempfile.mkdtemp())
        self.write_series(days)
        save(self.history / 'tushare_data/stock_basic.json', {
            'rows': [{'exchange': 'SSE', 'list_status': 'L', 'ts_code': c} for c in ('600000.SH', '600001.SH')],
            'fetched_at': NOW.isoformat()})

    def write_series(self, days):
        root = self.history / 'alpha_data/series'
        save(root / 'sh000300.json', {'bars': [bar(d, 4000) for d in days], 'fetched_at': NOW.isoformat()})
        for code in ('sh600000', 'sh600001'):
            save(root / (code + '.json'), {'bars': [bar(d) for d in days], 'fetched_at': NOW.isoformat()})
        self.days = days

    @contextlib.contextmanager
    def patched(self, eligible=None, raw_fn=None, candidates=None):
        """run()/main() 依赖太多数据源，这里把选股、回看校准、游资、原始价换成替身，保留其余全部逻辑。"""
        import contextlib
        cutoff = self.days[-1]
        screened = {'complete': True, 'market_score': 70, 'market_score_pause': 40, 'regime': '趋势', 'cutoff': cutoff,
                    'listed': 2, 'eligible': 2, 'valid': 2, 'coverage_pct': 100,
                    'breakout': {'candidates': [candidate('sh600000', 'breakout', 60)], 'exclusion_counts': {}},
                    'pullback': {'candidates': [candidate('sh600001', 'pullback', 55)], 'exclusion_counts': {}}}
        if candidates is not None:
            screened['breakout']['candidates'], screened['pullback']['candidates'] = candidates
        samples = {'samples': [], 'limitations': []}
        patches = [
            patch.object(alpha_engine, 'screen_short', return_value=screened),
            patch.object(alpha_engine, 'walk_forward', return_value=dict(samples)),
            patch.object(alpha_engine, 'walk_forward_short', return_value=dict(samples)),
            patch.object(alpha_engine, 'load_hotmoney', return_value=({}, {'hm_detail': False, 'limit_list_d': False})),
            patch.object(alpha_engine, 'raw_bars', side_effect=raw_fn or (
                lambda history, codes, now, cutoff=None: {c: {'bars': [bar(cutoff)]} for c in codes})),
        ]
        if eligible:
            patches.append(patch.object(alpha_engine, 'eligible_from', return_value=eligible))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield

    def run(self, run_id, exec_revision=None, exec_account=None, eligible=None, announce_fn=None, freeze=True,
            raw_fn=None, candidates=None):
        with self.patched(eligible, raw_fn, candidates):
            report = alpha_engine.run(self.history, run_id, exec_revision, exec_account, announce_fn, freeze)
        return report

    def frozen(self):
        return {p.stem: read(p) for p in sorted((self.history / 'predictions').glob('*.json'))}


def ledger(equity=101250.0):
    led = ce.new_ledger(NOW)
    led.update(equity=equity, cash=equity)
    return led


class FreezingTests(unittest.TestCase):
    def setUp(self):
        self.world = World(weekdays(70))

    def test_approved_revision_reaches_only_its_own_track_and_the_report_tag(self):
        r = self.world.run('20260919000000-1', REV)
        by_track = {f['strategy_type']: f for f in r['forecasts']}
        self.assertEqual(r['execution_version'], 'exec-0.3.r2')
        self.assertAlmostEqual(by_track['breakout']['exec_spec']['exit']['stop_pct'], 0.04375, places=3)     # 1.25 × ATR 3.5%
        self.assertAlmostEqual(by_track['pullback']['exec_spec']['exit']['stop_pct'], 0.0525, places=4)      # 没批准过的 track 不变：1.5 × 3.5%
        self.assertEqual(by_track['breakout']['exec_spec']['exit']['stop_atr_mult'], 1.25)
        for f in r['forecasts']:
            self.assertEqual((f['execution_version'], f['exec_spec']['revision']), ('exec-0.3.r2', 2))

    def test_without_a_revision_the_original_spec_is_frozen(self):
        r = self.world.run('20260919000000-1')
        self.assertEqual(r['execution_version'], 'exec-0.3')
        self.assertEqual({f['exec_spec']['exit']['stop_pct'] for f in r['forecasts']}, {0.0525})           # 1.5 × 各自的 ATR 3.5%
        self.assertEqual({f['exec_spec']['atr_pct'] for f in r['forecasts']}, {0.035})
        self.assertEqual({f['execution_version'] for f in r['forecasts']}, {'exec-0.3'})

    def test_the_spec_is_frozen_into_the_record_on_disk(self):
        self.world.run('20260919000000-1', REV)
        on_disk = self.world.frozen()
        self.assertEqual(len(on_disk), 2)
        breakout = next(f for f in on_disk.values() if f['strategy_type'] == 'breakout')
        self.assertAlmostEqual(breakout['exec_spec']['exit']['stop_pct'], 0.04375, places=3)
        self.assertEqual(breakout['execution_version'], 'exec-0.3.r2')

    def test_a_later_revision_never_rewrites_or_conflicts_with_frozen_plans(self):
        """同一截止日重跑：已冻结的计划保持旧规格（immutable），不能因为中途批准了新修订就报冲突或被改写。"""
        self.world.run('20260919000000-1')
        before = self.world.frozen()
        r = self.world.run('20260919000000-2', REV)                 # 没有抛冲突
        self.assertEqual(self.world.frozen(), before)
        self.assertEqual(r['execution_version'], 'exec-0.3.r2')


def news(status, title='测试公告', level=None):
    items = [{'time': '2026-09-19T20:30:00+08:00', 'title': title, 'level': level or {'block': 'block', 'flag': 'flag'}.get(status, 'info'),
              'reason': '测试', 'columns': []}] if status in ('block', 'flag') else []
    return {'status': status, 'items': items, 'error': '接口超时' if status == 'unverified' else None}


class PremarketSelectionTests(unittest.TestCase):
    """选股改在盘前：夜间公告核查、收盘涨停不占名额、收盘那两轮不冻结计划。"""

    def setUp(self):
        self.world = World(weekdays(70))

    def cands(self, n_break=3, n_pull=3):
        return ([candidate('sh6000%02d' % i, 'breakout', 90 - i) for i in range(n_break)],
                [candidate('sh6100%02d' % i, 'pullback', 80 - i) for i in range(n_pull)])

    def ids(self, r):
        return sorted(f['symbol'] for f in r['forecasts'])

    def test_closing_runs_freeze_nothing_but_still_report(self):
        r = self.world.run('20260919000000-1', freeze=False)
        self.assertEqual((r['forecasts'], r['new_forecast_ids']), ([], []))
        self.assertIsNone(r['baseline'] and r['baseline'].get('frozen') or None)          # 基线抽样也不冻结
        self.assertEqual(self.world.frozen(), {})

    def test_freezing_is_off_even_when_prices_for_every_candidate_are_available(self):
        """收盘那两轮：即使参考价都拿得到，也绝不新冻结计划——不能靠"恰好取不到价"来碰巧不冻结。"""
        every = lambda history, codes, now, cutoff=None: {c: {'bars': [bar(cutoff)]} for c in ('sh600000', 'sh600001')}
        r = self.world.run('20260919000000-1', freeze=False, raw_fn=every)
        self.assertEqual((r['forecasts'], r['new_forecast_ids']), ([], []))
        self.assertEqual(list((self.world.history / 'predictions').glob('*.json')) if (self.world.history / 'predictions').exists() else [], [])

    def test_blocked_candidates_are_removed_and_replaced_by_the_next_ranked(self):
        calls = []

        def announce(symbols, since, now):
            calls.append((list(symbols), since.isoformat()))
            return {s: news('block', '关于股东减持股份计划的公告') if s == 'sh600000' else news('clear') for s in symbols}
        r = self.world.run('20260919000000-1', candidates=self.cands(), announce_fn=announce)
        self.assertNotIn('sh600000', self.ids(r))
        self.assertEqual(len(r['forecasts']), 5)                                        # 6 只里剔除 1 只，其余仍占名额
        self.assertNotIn('sh600000', [c['symbol'] for c in r['candidates']])
        self.assertEqual(r['news_check']['blocked'][0]['symbol'], 'sh600000')
        self.assertEqual((r['news_check']['checked'], r['news_check']['clear']), (6, 5))
        # 核查的是"截止日 15:00 之后"的公告
        self.assertTrue(calls[0][1].endswith('T15:00:00+08:00'))
        self.assertEqual(calls[0][1][:10], r['screen']['cutoff'])

    def test_a_blocked_stock_does_not_take_a_trade_slot(self):
        def announce(symbols, since, now):
            return {s: news('block') if s in ('sh600000', 'sh600001', 'sh600002') else news('clear') for s in symbols}
        r = self.world.run('20260919000000-1', candidates=self.cands(3, 3), announce_fn=announce)
        self.assertEqual(self.ids(r), ['sh610000', 'sh610001', 'sh610002'])              # 突破 3 只全被剔除，回调 3 只递补
        self.assertEqual(sum(f['paper_eligible'] for f in r['forecasts']), 3)

    def test_flagged_and_unverified_are_annotated_but_not_removed(self):
        def announce(symbols, since, now):
            return {'sh600000': news('flag', '收到深交所问询函'), 'sh600001': news('unverified')}
        r = self.world.run('20260919000000-1', announce_fn=announce)
        by = {f['symbol']: f for f in r['forecasts']}
        self.assertEqual(set(by), {'sh600000', 'sh600001'})
        self.assertTrue(any('需要留意的公告' in x for x in by['sh600000']['plan_reasons']))
        self.assertTrue(any('未核验' in x and '接口超时' in x for x in by['sh600001']['plan_reasons']))
        self.assertTrue(by['sh600000']['paper_eligible'] and by['sh600001']['paper_eligible'])   # 没有被当作"确认有风险"
        self.assertEqual(by['sh600000']['news']['status'], 'flag')
        self.assertEqual((r['news_check']['flagged'], r['news_check']['unverified']), (1, 1))

    def test_the_report_text_says_what_was_checked_and_removed(self):
        def announce(symbols, since, now):
            return {s: news('block', '关于股东减持股份计划的公告') if s == 'sh600000' else news('unverified') for s in symbols}
        text = alpha_engine.render(self.world.run('20260919000000-1', announce_fn=announce))
        self.assertIn('## 夜间公告核查（选股前）', text)
        self.assertIn('未核验 1', text)
        self.assertIn('已剔除 sh600000 sh600000', text)
        self.assertIn('关于股东减持股份计划的公告', text)

    def test_announcement_source_failure_is_contained(self):
        def boom(symbols, since, now):
            raise RuntimeError('network down')
        r = self.world.run('20260919000000-1', announce_fn=boom)
        self.assertEqual(len(r['forecasts']), 2)                                         # 选股照常
        self.assertTrue(any('夜间公告核查整体失败' in i for i in r['issues']))
        self.assertTrue(all(f['news'] is None for f in r['forecasts']))

    def test_without_an_announcement_function_nothing_is_claimed_about_news(self):
        r = self.world.run('20260919000000-1')
        self.assertIsNone(r['news_check'])

    def test_a_stock_that_closed_at_the_limit_is_archived_but_never_tradable(self):
        cutoff = self.world.days[-1]
        prev = self.world.days[-2]

        def raw(history, codes, now, cutoff=None):
            out = {}
            for c in codes:
                closes = (10.0, 11.0) if c == 'sh600000' else (10.0, 10.2)
                out[c] = {'bars': [bar(prev, closes[0]), bar(cutoff, closes[1])]}
            return out
        r = self.world.run('20260919000000-1', raw_fn=raw)
        by = {f['symbol']: f for f in r['forecasts']}
        self.assertTrue(by['sh600000']['archive_only'] and not by['sh600000']['paper_eligible'])
        self.assertTrue(any('收盘涨停' in x for x in by['sh600000']['plan_reasons']))
        self.assertTrue(by['sh600001']['paper_eligible'])
        self.assertEqual(cutoff, r['screen']['cutoff'])

    def test_a_limit_up_stock_hands_its_trade_slot_to_the_next_candidate(self):
        """名额是按"可成交"顺延的，不是按排名前 3：第 1 名涨停 → 第 2、3、4 名可成交。"""
        cands = self.cands(3, 3)
        baseline = self.world.run('20260919000000-1', candidates=cands)
        order = [f['symbol'] for f in sorted(baseline['forecasts'], key=lambda f: f['rank'])]
        first = order[0]
        world = World(weekdays(70))
        cutoff, prev = world.days[-1], world.days[-2]

        def raw(history, codes, now, cutoff=None):
            return {c: {'bars': [bar(prev, 10.0), bar(cutoff, 11.0 if c == first else 10.1)]} for c in codes}
        r = world.run('20260919000000-1', candidates=self.cands(3, 3), raw_fn=raw)
        by_rank = sorted(r['forecasts'], key=lambda f: f['rank'])
        self.assertEqual([f['symbol'] for f in by_rank], order)                       # 排名不变
        self.assertEqual([f['paper_eligible'] for f in by_rank], [False, True, True, True, False, False])
        self.assertTrue(by_rank[0]['archive_only'] and 'limit' not in by_rank[1]['plan_reasons'])

    def test_limit_up_is_judged_by_board(self):
        rows = lambda a, b: [bar('d0', a), bar('d1', b)]
        self.assertTrue(alpha_engine.closed_limit_up(rows(10, 11), 'd1', 'sh600000'))
        self.assertFalse(alpha_engine.closed_limit_up(rows(10, 10.9), 'd1', 'sh600000'))       # 9% 不算
        self.assertFalse(alpha_engine.closed_limit_up(rows(10, 11), 'd1', 'sz300001'))         # 创业板 20% 才算
        self.assertTrue(alpha_engine.closed_limit_up(rows(10, 12), 'd1', 'sz300001'))
        self.assertTrue(alpha_engine.closed_limit_up(rows(10, 12), 'd1', 'sh688001'))
        self.assertFalse(alpha_engine.closed_limit_up([bar('d1', 11)], 'd1', 'sh600000'))      # 没有前一日：不确定就不剔除
        self.assertFalse(alpha_engine.closed_limit_up([], 'd1', 'sh600000'))

    def test_a_missing_atr_means_no_tradable_plan(self):
        b, p = self.cands(1, 1)
        b[0]['atr14'] = None
        r = self.world.run('20260919000000-1', candidates=(b, p))
        by = {f['symbol']: f for f in r['forecasts']}
        self.assertFalse(by['sh600000']['paper_eligible'])
        self.assertTrue(any('ATR' in x for x in by['sh600000']['plan_reasons']))
        self.assertEqual(by['sh600000']['exec_spec']['levels'], 'nominal')                # 标明是典型值，不是它自己的波动

    def test_only_the_first_three_tradable_candidates_take_trade_slots(self):
        r = self.world.run('20260919000000-1', candidates=self.cands(4, 4))
        self.assertEqual(sum(f['paper_eligible'] for f in r['forecasts']), 3)
        self.assertEqual(len(r['forecasts']), 8)
        self.assertTrue(all(f['archive_only'] for f in r['forecasts'] if not f['paper_eligible']))

    def test_new_plans_carry_the_numbers_a_person_needs_to_act_on(self):
        r = self.world.run('20260919000000-1')
        f = next(f for f in r['forecasts'] if f['strategy_type'] == 'breakout')
        lv = f['plan_levels']
        ref = f['reference_price']
        self.assertAlmostEqual(lv['stop_pct'], 0.0525, places=4)
        self.assertAlmostEqual(lv['target_pct'], 0.0788, places=3)
        self.assertEqual(lv['hold_sessions'], 5)
        self.assertAlmostEqual(lv['entry_low'], ref * 1.005, places=2)
        self.assertAlmostEqual(lv['entry_high'], ref * 1.03, places=2)
        self.assertAlmostEqual(lv['void_price'], ref * (1 - 0.0525), places=2)
        self.assertAlmostEqual(lv['stop_price_at_reference'], ref * (1 - 0.0525), places=2)
        self.assertEqual(lv['window'], ['09:30', '14:30'])
        self.assertEqual(lv['sizing']['binding'], 'weight_cap')
        self.assertEqual(lv['sizing']['position_pct'], 30.0)
        self.assertEqual(sorted(r['new_forecast_ids']), sorted(x['id'] for x in r['forecasts']))

    def test_rerun_creates_nothing_new_and_says_so(self):
        self.world.run('20260919000000-1')
        again = self.world.run('20260919000001-1')
        self.assertEqual(again['new_forecast_ids'], [])
        self.assertEqual(len(again['forecasts']), 2)


class MainEntryTests(unittest.TestCase):
    """真正被 cron 调用的入口。它在必需的流程里，而且曾经因为漏了一行 import 只有真跑才会崩。"""

    def setUp(self):
        self.world = World(weekdays(70))
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('REPORT_SLOT', None)
        self.pushed = []
        p1 = patch('announcements.check_many', side_effect=lambda symbols, since, now: {s: news('clear') for s in symbols})
        p2 = patch('signal_cards.push', side_effect=lambda report: self.pushed.append(list(report['new_forecast_ids'])) or '已推送 1 段')
        p3 = patch('conditional_exec.read_ledger', return_value=None)
        p4 = patch('proposals.active', return_value=(0, {}))
        for p in (p1, p2, p3, p4):
            p.start()
            self.addCleanup(p.stop)

    def main(self, *args, when='08:40:00', run_id='20260920000000-1'):
        now = datetime.fromisoformat('2026-09-21T%s+08:00' % when)
        with self.world.patched():
            code = alpha_engine.main(['--history', str(self.world.history), '--run-id', run_id, *args], now=now)
        return code

    def saved(self):
        return sorted((self.world.history / 'agent').glob('*.json'))

    def test_premarket_run_freezes_plans_saves_the_report_and_pushes_the_cards(self):
        os.environ['REPORT_SLOT'] = 'premarket'
        self.assertEqual(self.main(), 0)
        self.assertEqual(len(self.world.frozen()), 2)
        self.assertEqual(len(self.saved()), 1)
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(len(self.pushed[0]), 2)

    def test_no_slot_at_a_premarket_hour_also_freezes(self):
        self.main()
        self.assertEqual(len(self.world.frozen()), 2)

    def test_closing_runs_freeze_nothing_and_push_nothing(self):
        os.environ['REPORT_SLOT'] = 'close'
        self.main(when='15:35:00')
        self.assertEqual(self.world.frozen(), {})
        self.assertEqual(self.pushed, [])
        self.assertEqual(len(self.saved()), 1)                                  # 验收/估值/证据的报告照常保存

    def test_after_the_executors_deadline_nothing_is_frozen_unless_forced(self):
        """09:20 之后冻结的计划当天已不能成交，且固定标签会从"后天"算起，混进错位样本。"""
        os.environ['REPORT_SLOT'] = 'premarket'
        self.main(when='10:17:00')
        self.assertEqual(self.world.frozen(), {})
        self.main('--freeze', when='10:17:00', run_id='20260920000001-1')
        self.assertEqual(len(self.world.frozen()), 2)

    def test_no_freeze_flag_wins_over_the_default(self):
        self.main('--no-freeze')
        self.assertEqual(self.world.frozen(), {})

    def test_a_failing_push_or_ledger_read_cannot_fail_the_required_step(self):
        with patch('signal_cards.push', side_effect=RuntimeError('wecom down')):
            self.assertEqual(self.main(), 0)
        self.assertEqual(len(self.world.frozen()), 2)
        with patch('conditional_exec.read_ledger', side_effect=ValueError('corrupt')):
            self.assertEqual(self.main(run_id='20260920000002-1'), 0)

    def test_the_same_run_id_cannot_be_archived_twice(self):
        self.main()
        with self.assertRaises(ValueError):
            self.main()

    def test_bad_run_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            self.main(run_id='../evil')


class OptionalLayersTests(unittest.TestCase):
    def setUp(self):
        self.world = World(weekdays(70))

    def test_evidence_failure_is_contained_and_does_not_stop_candidate_generation(self):
        with patch.object(contract_labels, 'resolve_all', side_effect=RuntimeError('boom')):
            r = self.world.run('20260919000000-1')
        self.assertIsNone(r['evidence'])
        self.assertTrue(any('证据三层' in i and 'boom' in i for i in r['issues']))
        self.assertEqual(len(r['forecasts']), 2)                    # 必需的部分照常产出

    def test_account_snapshot_failure_is_contained(self):
        r = self.world.run('20260919000000-1', None, {'not': 'a ledger'})
        self.assertIsNone(r['exec02'])
        self.assertTrue(any('盘中条件执行账户快照失败' in i for i in r['issues']))
        self.assertEqual(len(r['forecasts']), 2)

    def test_no_ledger_means_no_account_section_not_an_empty_account(self):
        self.assertIsNone(self.world.run('20260919000000-1')['exec02'])

    def test_snapshot_is_attached_and_the_curve_continues_from_the_previous_report(self):
        first = self.world.run('20260919000000-1', None, ledger(100000.0))
        self.assertEqual(first['exec02']['equity'], 100000.0)
        self.assertEqual(len(first['exec02']['curve']), 1)
        alpha_engine.immutable(self.world.history / 'agent' / '20260919000000-1.json', first)
        second = self.world.run('20260919000001-1', None, ledger(101250.0))
        self.assertEqual(second['exec02']['equity'], 101250.0)
        # 同一天重跑覆盖当天的点，而不是叠出两个点
        self.assertEqual([c['date'] for c in second['exec02']['curve']], [NOW.date().isoformat()])
        self.assertEqual(second['exec02']['curve'][-1]['equity'], 101250.0)

    def test_earlier_days_on_the_curve_are_carried_forward(self):
        first = self.world.run('20260919000000-1', None, ledger(100000.0))
        first['exec02']['curve'] = [{'date': '2026-01-05', 'equity': 99000.0, 'nav': 0.99}] + first['exec02']['curve']
        alpha_engine.immutable(self.world.history / 'agent' / '20260919000000-1.json', first)
        second = self.world.run('20260919000001-1', None, ledger(101250.0))
        self.assertEqual([c['date'] for c in second['exec02']['curve']], ['2026-01-05', NOW.date().isoformat()])

    def test_report_renders_with_both_optional_layers(self):
        r = self.world.run('20260919000000-1', None, ledger())
        text = alpha_engine.render(r)
        self.assertIn('盘中条件执行虚拟账户', text)
        self.assertIn('证据三层', text)


class EvidenceEndToEndTests(unittest.TestCase):
    """先在"较早的截止日"冻结计划，再补上后面的日线重跑，标签真的会被算出来、写盘、并入报告。"""

    def matured_world(self, first_revision, second_revision):
        all_days = weekdays(70)
        early = all_days[:-8]                                       # 第一轮的截止日比现在早 8 个交易日
        world = World(early)
        world.run('20260919000000-1', first_revision, eligible=all_days[-7])
        world.write_series(all_days)
        return world, world.run('20260919000001-1', second_revision, eligible=all_days[-7])

    def test_labels_are_computed_persisted_and_summarised(self):
        world, r = self.matured_world(REV, REV)
        files = sorted((world.history / 'contract_labels').glob('*.json'))
        self.assertEqual(len(files), 2)
        self.assertEqual(r['evidence']['total_records'], 2)
        self.assertEqual(r['evidence']['execution_version'], 'exec-0.3.r2')
        self.assertEqual(sorted(r['evidence']['groups']), ['breakout/top3', 'pullback/top3'])
        rec = read(next(f for f in files if 'sh600000' in f.name))
        self.assertEqual((rec['track'], rec['exec_spec_revision'], rec['method']), ('breakout', 2, 'daily-bar-v1'))

    def test_samples_frozen_under_another_execution_version_are_not_pooled(self):
        _, r = self.matured_world(None, REV)                        # 冻结时是 exec-0.3，现在的执行版本是 r2
        self.assertEqual(r['evidence']['groups'], {})
        self.assertEqual(r['evidence']['other_versions'], 2)


if __name__ == '__main__':
    unittest.main()
