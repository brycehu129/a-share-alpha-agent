"""alpha_engine.run() 的接线测试：批准的规格修订、exec-0.2 账户快照、证据三层，是否真的进了冻结的记录和报告。

run() 依赖很多数据源，这里用真实的目录结构 + 少量替身（选股、回看校准、游资、原始价）搭一个能跑通的小环境，
保留 run() 自己的全部逻辑：冻结预测、排名、版本打标、证据三层、账户快照。以前这条链只用一次真实数据回放验证过，
改到这里悄悄坏掉没人发现。"""
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

REV = (2, {'breakout': {'exit.stop_pct': 0.02}})
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
            'liquidity_pct': 50, 'probability': {'probability': None, 'n': 0, 'cohorts': 0, 'low': None, 'high': None}}


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

    def run(self, run_id, exec_revision=None, exec_account=None, eligible=None):
        cutoff = self.days[-1]
        screened = {'complete': True, 'market_score': 70, 'market_score_pause': 40, 'regime': '趋势', 'cutoff': cutoff,
                    'listed': 2, 'eligible': 2, 'valid': 2, 'coverage_pct': 100,
                    'breakout': {'candidates': [candidate('sh600000', 'breakout', 60)], 'exclusion_counts': {}},
                    'pullback': {'candidates': [candidate('sh600001', 'pullback', 55)], 'exclusion_counts': {}}}
        samples = {'samples': [], 'limitations': []}
        patches = [
            patch.object(alpha_engine, 'screen_short', return_value=screened),
            patch.object(alpha_engine, 'walk_forward', return_value=dict(samples)),
            patch.object(alpha_engine, 'walk_forward_short', return_value=dict(samples)),
            patch.object(alpha_engine, 'load_hotmoney', return_value=({}, {'hm_detail': False, 'limit_list_d': False})),
            patch.object(alpha_engine, 'raw_bars',
                         side_effect=lambda history, codes, now, cutoff=None: {c: {'bars': [bar(cutoff)]} for c in codes}),
        ]
        if eligible:
            patches.append(patch.object(alpha_engine, 'eligible_from', return_value=eligible))
        for p in patches:
            p.start()
        try:
            report = alpha_engine.run(self.history, run_id, exec_revision, exec_account)
        finally:
            for p in patches:
                p.stop()
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
        self.assertEqual(r['execution_version'], 'exec-0.2.r2')
        self.assertEqual(by_track['breakout']['exec_spec']['exit']['stop_pct'], 0.02)
        self.assertEqual(by_track['pullback']['exec_spec']['exit']['stop_pct'], 0.03)
        for f in r['forecasts']:
            self.assertEqual((f['execution_version'], f['exec_spec']['revision']), ('exec-0.2.r2', 2))

    def test_without_a_revision_the_original_spec_is_frozen(self):
        r = self.world.run('20260919000000-1')
        self.assertEqual(r['execution_version'], 'exec-0.2')
        self.assertEqual({f['exec_spec']['exit']['stop_pct'] for f in r['forecasts']}, {0.025, 0.03})
        self.assertEqual({f['execution_version'] for f in r['forecasts']}, {'exec-0.2'})

    def test_the_spec_is_frozen_into_the_record_on_disk(self):
        self.world.run('20260919000000-1', REV)
        on_disk = self.world.frozen()
        self.assertEqual(len(on_disk), 2)
        breakout = next(f for f in on_disk.values() if f['strategy_type'] == 'breakout')
        self.assertEqual((breakout['exec_spec']['exit']['stop_pct'], breakout['execution_version']), (0.02, 'exec-0.2.r2'))

    def test_a_later_revision_never_rewrites_or_conflicts_with_frozen_plans(self):
        """同一截止日重跑：已冻结的计划保持旧规格（immutable），不能因为中途批准了新修订就报冲突或被改写。"""
        self.world.run('20260919000000-1')
        before = self.world.frozen()
        r = self.world.run('20260919000000-2', REV)                 # 没有抛冲突
        self.assertEqual(self.world.frozen(), before)
        self.assertEqual(r['execution_version'], 'exec-0.2.r2')


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
        self.assertTrue(any('exec-0.2 账户快照失败' in i for i in r['issues']))
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
        self.assertIn('exec-0.2 虚拟账户', text)
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
        self.assertEqual(r['evidence']['execution_version'], 'exec-0.2.r2')
        self.assertEqual(sorted(r['evidence']['groups']), ['breakout/top3', 'pullback/top3'])
        rec = read(next(f for f in files if 'sh600000' in f.name))
        self.assertEqual((rec['track'], rec['exec_spec_revision'], rec['method']), ('breakout', 2, 'daily-bar-v1'))

    def test_samples_frozen_under_another_execution_version_are_not_pooled(self):
        _, r = self.matured_world(None, REV)                        # 冻结时是 exec-0.2，现在的执行版本是 r2
        self.assertEqual(r['evidence']['groups'], {})
        self.assertEqual(r['evidence']['other_versions'], 2)


if __name__ == '__main__':
    unittest.main()
