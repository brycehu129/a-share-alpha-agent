"""选股版本 / 执行版本拆分，以及"样本不许跨版本混池"的回归测试。

这组测试守的是一个静默的错：验收记录里原本没有版本字段，样本池只靠 horizon 分组。
0.2(10天)/0.3(3天) 碰巧没混，但 0.4 仍是3天窗口——不修的话 0.3 和 0.4 的样本会
进同一个池子，而两版的筛选规则已经不同，算出来的胜率哪个版本都不代表。
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, date
from pathlib import Path

import alpha_engine as ae
from alpha_engine import (cutoff_slots, resolve, selection_version_of, split_short_pools,
                          tagged)
from collect_quotes import CST
from shortterm_model import ARCHIVE_SIZE, EXECUTION_VERSION, SELECTION_VERSION


def bars(n=40, start='2026-08-03', growth=0.001):
    day, rows = date.fromisoformat(start), []
    while len(rows) < n:
        if day.weekday() < 5:
            p = 10 * (1 + growth) ** len(rows)
            rows.append({'date': day.isoformat(), 'open': str(p), 'close': str(p),
                         'high': str(p * 1.02), 'low': str(p * 0.98), 'volume_raw': '100000'})
        day += timedelta(days=1)
    return rows


def forecast(fid='f1', version=None, selection=None, execution=None, rank=None,
             as_of='2026-08-07', paper=False, hold=3, eligible='2026-08-10'):
    f = {'id': fid, 'symbol': 'sz000001', 'name': 't', 'bucket': 'breakout',
         'strategy_type': 'breakout', 'regime': '趋势', 'as_of': as_of, 'eligible_from': eligible,
         'paper_eligible': paper, 'probability': {'probability': None},
         'policy': {'hold_sessions': hold}}
    if version:
        f['version'] = version
    if selection:
        f['selection_version'] = selection
    if execution:
        f['execution_version'] = execution
    if rank is not None:
        f['rank'] = rank
    return f


class SelectionVersionOfTests(unittest.TestCase):
    def test_new_records_use_the_explicit_field(self):
        self.assertEqual(selection_version_of(forecast(selection='select-0.4', version='ignored')), 'select-0.4')

    def test_legacy_records_fall_back_to_their_version(self):
        """0.2/0.3 的旧记录只有 version——当时选股和执行不分家，那就是它的选股版本。"""
        self.assertEqual(selection_version_of(forecast(version='alpha-shadow-0.3-shortterm')),
                         'alpha-shadow-0.3-shortterm')


class PoolSplitTests(unittest.TestCase):
    def test_same_horizon_different_selection_version_never_share_a_pool(self):
        outcomes = [
            {'horizon': 3, 'selection_version': SELECTION_VERSION, 'win': True},
            {'horizon': 3, 'selection_version': SELECTION_VERSION, 'win': False},
            {'horizon': 3, 'selection_version': 'alpha-shadow-0.3-shortterm', 'win': True},
            {'horizon': 10, 'selection_version': 'alpha-shadow-0.2-observed', 'win': True},
        ]
        live, other = split_short_pools(outcomes)
        self.assertEqual(len(live), 2)
        self.assertEqual(len(other), 1)                       # 0.3 的3天样本单独计数
        self.assertTrue(all(o['horizon'] == 3 for o in live + other))  # 10天的既不在这也不在那

    def test_outcome_without_version_field_is_not_silently_counted_as_current(self):
        live, other = split_short_pools([{'horizon': 3, 'win': True}])
        self.assertEqual((len(live), len(other)), (0, 1))


class TaggingTests(unittest.TestCase):
    def test_tagging_fills_in_missing_fields_without_overwriting_present_ones(self):
        legacy = tagged({'horizon': 3}, forecast(version='alpha-shadow-0.3-shortterm'))
        self.assertEqual(legacy['selection_version'], 'alpha-shadow-0.3-shortterm')
        self.assertEqual(legacy['execution_version'], 'legacy')
        self.assertIsNone(legacy['rank'])
        already = tagged({'horizon': 3, 'selection_version': 'X', 'rank': 4},
                         forecast(selection='select-0.4', rank=9))
        self.assertEqual((already['selection_version'], already['rank']), ('X', 4))


class ResolveStampsVersionsTests(unittest.TestCase):
    """resolve() 生成的新验收记录必须带版本和排名；读回磁盘上的旧记录时只在内存里补标签。"""

    def setUp(self):
        self.history = Path(tempfile.mkdtemp())
        self.bench = bars()
        self.now = datetime(2026, 9, 1, 18, 0, tzinfo=CST)

    def test_new_outcome_is_stamped_and_persisted_with_versions(self):
        f = forecast(selection=SELECTION_VERSION, execution=EXECUTION_VERSION, rank=7)
        out = resolve([f], {'sz000001': self.bench}, self.bench, self.now, self.history)
        self.assertTrue(out)
        for o in out:
            self.assertEqual(o['selection_version'], SELECTION_VERSION)
            self.assertEqual(o['execution_version'], EXECUTION_VERSION)
            self.assertEqual(o['rank'], 7)
        on_disk = json.loads((self.history / 'outcomes' / (f['id'] + '-3.json')).read_text())
        self.assertEqual(on_disk['payload']['selection_version'], SELECTION_VERSION)
        self.assertEqual(on_disk['payload']['rank'], 7)

    def test_horizons_follow_the_hold_period(self):
        out = resolve([forecast(selection=SELECTION_VERSION, hold=3)], {'sz000001': self.bench},
                      self.bench, self.now, self.history)
        self.assertEqual(sorted(o['horizon'] for o in out), [1, 2, 3])

    def test_legacy_outcome_on_disk_is_tagged_in_memory_but_never_rewritten(self):
        """验收记录不可改写。旧记录没有版本字段，只能在读出来之后往内存副本里补。"""
        f = forecast(version='alpha-shadow-0.3-shortterm')
        first = resolve([f], {'sz000001': self.bench}, self.bench, self.now, self.history)
        path = self.history / 'outcomes' / (f['id'] + '-3.json')
        # 模拟一条 0.3 时代写下的旧记录：从磁盘内容里抹掉版本字段（连同校验和一起重算）
        envelope = json.loads(path.read_text())
        for k in ('selection_version', 'execution_version', 'rank'):
            envelope['payload'].pop(k, None)
        import hashlib
        envelope['sha256'] = hashlib.sha256(json.dumps(
            envelope['payload'], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path.write_text(json.dumps(envelope, ensure_ascii=False))
        before = path.read_text()

        again = resolve([f], {'sz000001': self.bench}, self.bench, self.now, self.history)
        three = next(o for o in again if o['horizon'] == 3)
        self.assertEqual(three['selection_version'], 'alpha-shadow-0.3-shortterm')  # 内存里补上了
        self.assertEqual(three['execution_version'], 'legacy')
        self.assertEqual(path.read_text(), before)                                    # 磁盘一字未动
        self.assertEqual(len(first), len(again))


class CutoffSlotsTests(unittest.TestCase):
    """同一截止日一天会被重跑好几次（整点任务 + 每小时补偿检查）。不设上限的话留档条数
    会悄悄超过 ARCHIVE_SIZE、允许成交的计划会超过 max_positions。"""

    def test_counts_only_the_current_selection_version_and_cutoff(self):
        fs = [
            forecast('a', selection=SELECTION_VERSION, as_of='2026-09-18', paper=True),
            forecast('b', selection=SELECTION_VERSION, as_of='2026-09-18', paper=False),
            forecast('c', selection=SELECTION_VERSION, as_of='2026-09-17', paper=True),   # 别的截止日
            forecast('d', version='alpha-shadow-0.3-shortterm', as_of='2026-09-18', paper=True),  # 旧版本
        ]
        self.assertEqual(cutoff_slots(fs, '2026-09-18'), (2, 1))
        self.assertEqual(cutoff_slots(fs, '2026-09-17'), (1, 1))
        self.assertEqual(cutoff_slots(fs, '2026-09-19'), (0, 0))

    def test_archive_size_and_trade_slots_are_distinct_limits(self):
        self.assertGreater(ARCHIVE_SIZE, ae.POLICY['max_positions'])
        self.assertEqual(ae.POLICY['max_positions'], 3)


if __name__ == '__main__':
    unittest.main()
