import random
import statistics
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import alpha_engine
import baseline as bl
from alpha_engine import immutable
from tushare_sync import read

NOW = datetime.fromisoformat('2026-09-25T15:35:00+08:00')


def weekdays(n, start=date(2026, 6, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def bar(day, o, c=None, v=1000):
    c = o if c is None else c
    return {'date': day, 'open': str(o), 'close': str(c), 'high': str(max(o, c) * 1.01), 'low': str(min(o, c) * 0.99),
            'volume_raw': str(v)}


class DrawTests(unittest.TestCase):
    POOL = ['sh6%05d' % i for i in range(200)]

    def test_reproducible_and_independent_of_input_order(self):
        a = bl.draw(self.POOL, '2026-09-18', 'universe')
        shuffled = list(self.POOL)
        random.Random(1).shuffle(shuffled)
        self.assertEqual(a, bl.draw(shuffled, '2026-09-18', 'universe'))
        self.assertEqual(len(a), bl.DRAW_N)
        self.assertEqual(len(set(a)), bl.DRAW_N)
        self.assertTrue(set(a) <= set(self.POOL))

    def test_different_days_and_baselines_draw_different_samples(self):
        base = bl.draw(self.POOL, '2026-09-18', 'universe')
        self.assertNotEqual(base, bl.draw(self.POOL, '2026-09-21', 'universe'))
        self.assertNotEqual(base, bl.draw(self.POOL, '2026-09-18', 'strong_industry'))

    def test_a_small_pool_is_taken_whole_and_an_empty_one_is_empty(self):
        self.assertEqual(bl.draw(self.POOL[:5], '2026-09-18', 'universe'), self.POOL[:5])
        self.assertEqual(bl.draw([], '2026-09-18', 'universe'), [])


def market(dates, prices):
    """prices: {symbol: (起始价, 每日涨幅)}；基准每天 +0.1%。"""
    benchmark = [bar(d, 4000 * 1.001 ** i, 4000 * 1.001 ** i) for i, d in enumerate(dates)]
    series = {s: [bar(d, p * (1 + g) ** i, p * (1 + g) ** i) for i, d in enumerate(dates)] for s, (p, g) in prices.items()}
    return series, benchmark


class LabelDrawTests(unittest.TestCase):
    def setUp(self):
        self.dates = weekdays(30)

    def test_aggregates_the_fixed_label_with_the_same_cost_and_benchmark_rule(self):
        series, bench = market(self.dates, {'a': (10, 0.02), 'b': (10, -0.02)})
        cutoff = self.dates[10]
        r = bl.label_draw(['a', 'b'], cutoff, series, bench)
        self.assertEqual((r['entry_day'], r['end_day']), (self.dates[11], self.dates[11 + bl.MATURITY]))   # 要等最长持有期走完
        self.assertEqual((r['n_drawn'], r['n_labeled'], r['wins']), (2, 2, 1))
        self.assertEqual(r['win_rate_pct'], 50.0)
        # a：入场日开盘 → 第3个间隔收盘，扣 0.5%
        a_open = 10 * 1.02 ** 11
        a_close = 10 * 1.02 ** 14
        expect_a = (a_close / a_open - 1) * 100 - 0.5
        b_open, b_close = 10 * 0.98 ** 11, 10 * 0.98 ** 14
        expect_b = (b_close / b_open - 1) * 100 - 0.5
        self.assertAlmostEqual(r['mean_return_pct'], statistics.mean([expect_a, expect_b]), places=3)

    def test_barrier_label_uses_each_stocks_own_atr_and_the_same_exit_rules(self):
        """屏障标签：次日开盘买入，止损/止盈按这只股票自己的 ATR 定。稳步上涨的票会在到期前先摸到目标；下跌的票先摸到止损。"""
        series, bench = market(self.dates, {'up': (10, 0.03), 'down': (10, -0.03)})
        r = bl.label_draw(['up', 'down'], self.dates[18], series, bench)
        self.assertEqual(r['barrier_n'], 2)
        self.assertGreater(r['barrier_net_opt'], -100)
        up = bl.barrier_outcome({b['date']: b for b in series['up']}, self.dates[18], self.dates)
        down = bl.barrier_outcome({b['date']: b for b in series['down']}, self.dates[18], self.dates)
        self.assertEqual(up['pess']['reason'], 'target')
        self.assertGreater(up['pess']['net_pct'], 0)
        self.assertEqual(down['pess']['reason'], 'stop')
        self.assertLess(down['pess']['net_pct'], 0)

    def test_barrier_label_needs_a_full_atr_window_and_the_full_holding_period(self):
        series, bench = market(self.dates, {'a': (10, 0.01)})
        bars = {b['date']: b for b in series['a']}
        self.assertIsNone(bl.barrier_outcome(bars, self.dates[10], self.dates))           # 截止日前不足 15 个交易日：没有 ATR
        self.assertIsNotNone(bl.barrier_outcome(bars, self.dates[18], self.dates))
        self.assertIsNone(bl.barrier_outcome(bars, self.dates[-3], self.dates))           # 持有期还没走完
        holes = dict(bars)
        del holes[self.dates[20]]
        self.assertIsNone(bl.barrier_outcome(holes, self.dates[18], self.dates))         # 中间缺一天：不跳过去算

    def test_unmatured_windows_return_none_not_a_partial_result(self):
        series, bench = market(self.dates, {'a': (10, 0.01)})
        self.assertIsNone(bl.label_draw(['a'], self.dates[-3], series, bench))
        self.assertIsNone(bl.label_draw(['a'], self.dates[-1 - bl.MATURITY], series, bench))     # 固定标签走完了，但屏障的持有期还没
        self.assertIsNone(bl.label_draw(['a'], self.dates[-1], series, bench))

    def test_symbols_without_data_are_counted_as_unlabeled_not_as_losses(self):
        series, bench = market(self.dates, {'a': (10, 0.02)})
        r = bl.label_draw(['a', 'ghost'], self.dates[10], series, bench)
        self.assertEqual((r['n_drawn'], r['n_labeled'], r['wins']), (2, 1, 1))
        self.assertEqual(r['win_rate_pct'], 100.0)

    def test_nothing_labelable_gives_empty_aggregates(self):
        series, bench = market(self.dates, {'a': (10, 0.02)})
        r = bl.label_draw(['ghost'], self.dates[10], series, bench)
        self.assertEqual((r['n_labeled'], r['win_rate_pct']), (0, None))


class PairedTests(unittest.TestCase):
    def test_below_the_cohort_gate_no_interval_is_given(self):
        r = bl._paired([1.0] * 14)
        self.assertEqual((r['days'], r['mean'], r['verdict']), (14, None, None))

    def test_verdicts_follow_the_interval_not_the_mean(self):
        noisy = [5.0, -4.0] * 8                                   # 均值 0.5，但噪声大 → 区间跨过 0
        self.assertEqual(bl._paired(noisy)['verdict'], 'indistinguishable')
        self.assertEqual(bl._paired([1.0, 1.2, 0.9, 1.1] * 4)['verdict'], 'better')
        self.assertEqual(bl._paired([-1.0, -1.2, -0.9, -1.1] * 4)['verdict'], 'worse')

    def test_positively_autocorrelated_differences_get_a_wider_interval_than_the_naive_one(self):
        """相邻截止日的 3 日窗口重叠 → 逐日差值正自相关。校正后的区间必须比"当作独立"的更宽，否则等于没校正。"""
        rng = random.Random(5)
        diffs, x = [], 0.0
        for _ in range(300):
            x = 0.8 * x + rng.gauss(0, 1)
            diffs.append(x)
        naive = 2 * 1.96 * statistics.stdev(diffs) / len(diffs) ** 0.5
        r = bl._paired(diffs)
        self.assertGreater(r['high'] - r['low'], 1.4 * naive)

    def test_uncorrelated_series_matches_the_plain_standard_error_closely(self):
        rng = random.Random(11)
        diffs = [rng.gauss(0, 1) for _ in range(400)]
        plain = 2 * 1.96 * statistics.stdev(diffs) / len(diffs) ** 0.5
        r = bl._paired(diffs)
        self.assertLess(abs((r['high'] - r['low']) - plain) / plain, 0.35)


def group(win, ret, exc, n, barrier=None):
    """一天的汇总。屏障标签（主指标）默认取和超额相同的数，让下面的断言能用同一套数字表达；
    barrier 传 None 表示这一天没有屏障标签（数据不够）。"""
    row = {'n_labeled': n, 'win_rate_pct': win, 'mean_return_pct': ret, 'mean_excess_pp': exc}
    b = exc if barrier is None else barrier
    row.update(barrier_n=n, barrier_net_pess=b, barrier_net_opt=b + 0.5, barrier_stop_pct=40.0, barrier_target_pct=10.0,
               barrier_expiry_pct=50.0)
    return row


def base_day(win, ret, exc, n=30):
    return group(win, ret, exc, n)


def strat_day(win, ret, exc, n=10):
    return {'top10': group(win, ret, exc, n), 'top3': group(win, ret, exc, 3 if n else 0)}


class SummarizeTests(unittest.TestCase):
    def days(self, n):
        return weekdays(n)

    def test_primary_result_is_top10_vs_strong_industry_on_excess(self):
        days = self.days(20)
        base = {(d, 'strong_industry'): base_day(40.0, 0.0, -1.0) for d in days}
        base.update({(d, 'universe'): base_day(35.0, -0.5, -1.5) for d in days})
        wobble = [0.1, -0.1, 0.05, -0.05] * 5
        strat = {d: strat_day(50.0, 1.0, 0.5 + w) for d, w in zip(days, wobble)}
        s = bl.summarize(base, strat, 'forward')
        self.assertEqual(s['primary_result'], s['baselines']['strong_industry']['vs']['top10']['mean_excess_pp'])
        self.assertEqual(s['primary_result']['verdict'], 'better')
        self.assertAlmostEqual(s['primary_result']['mean'], 1.5, places=2)              # 0.5 - (-1.0)
        self.assertEqual(s['baselines']['strong_industry']['base_rate']['win_rate_pct'], 40.0)
        self.assertEqual(s['baselines']['universe']['vs']['top10']['mean_excess_pp']['mean'], 2.0)

    def test_too_few_days_gives_counts_but_no_numbers(self):
        days = self.days(10)
        base = {(d, 'strong_industry'): base_day(40.0, 0.0, -1.0) for d in days}
        s = bl.summarize(base, {d: strat_day(50.0, 1.0, 0.5) for d in days}, 'forward')
        b = s['baselines']['strong_industry']
        self.assertIsNone(b['base_rate'])
        self.assertEqual((b['days'], b['labeled']), (10, 300))
        self.assertIsNone(b['vs']['top10']['strategy'])
        self.assertIsNone(s['primary_result'] and s['primary_result'].get('mean'))

    def test_baseline_days_with_nothing_labelable_do_not_count_as_days(self):
        days = self.days(21)
        base = {(d, 'strong_industry'): base_day(40.0, 0.0, -1.0) for d in days[:20]}
        base[(days[20], 'strong_industry')] = {'n_labeled': 0, 'win_rate_pct': None, 'mean_return_pct': None,
                                              'mean_excess_pp': None}
        b = bl.summarize(base, {d: strat_day(50.0, 1.0, 0.5) for d in days}, 'forward')['baselines']['strong_industry']
        self.assertEqual(b['days'], 20)
        self.assertEqual(b['base_rate']['win_rate_pct'], 40.0)

    def test_only_days_where_both_sides_exist_are_paired(self):
        days = self.days(20)
        base = {(d, 'strong_industry'): base_day(40.0, 0.0, -1.0) for d in days}
        strat = {d: strat_day(50.0, 1.0, 0.5) for d in days[:16]}               # 策略只在前 16 天有标签
        block = bl.summarize(base, strat, 'forward')['baselines']['strong_industry']['vs']['top10']
        self.assertEqual(block['days'], 16)

    def test_paused_days_with_no_strategy_picks_are_not_counted_as_zero(self):
        days = self.days(20)
        base = {(d, 'strong_industry'): base_day(40.0, 0.0, -1.0) for d in days}
        strat = {d: strat_day(50.0, 1.0, 0.5, n=0) for d in days}                # n_labeled=0：当天没选出票
        block = bl.summarize(base, strat, 'forward')['baselines']['strong_industry']['vs']['top10']
        self.assertEqual(block['days'], 0)

    def test_strategy_needs_enough_labels_too(self):
        days = self.days(16)
        base = {(d, 'strong_industry'): base_day(40.0, 0.0, -1.0) for d in days}
        strat = {d: strat_day(50.0, 1.0, 0.5, n=1) for d in days}                # 16 天但只有 16 条 < 30
        self.assertIsNone(bl.summarize(base, strat, 'forward')['baselines']['strong_industry']['vs']['top10']['strategy'])


class TradableTierTests(unittest.TestCase):
    def test_tradable_tier_follows_archive_only_not_rank_once_it_is_recorded(self):
        """收盘涨停的票只留档不成交，可成交名额顺延给排名更靠后的——所以"可成交那几条"不再等于"排名前 3"。"""
        forecasts = [{'id': 'p%d' % i, 'as_of': '2026-09-18'} for i in range(5)]
        outs = [{'prediction_id': 'p%d' % i, 'horizon': 3, 'selection_version': 'select-0.5', 'rank': i + 1,
                 'archive_only': i in (0, 4), 'win': True, 'return_pct': float(i), 'excess_pp': 0.0} for i in range(5)]
        day = bl.strategy_by_cutoff(outs, forecasts, 'select-0.5')['2026-09-18']
        self.assertEqual(day['top10']['n_labeled'], 5)
        self.assertEqual(day['top3']['n_labeled'], 3)                                     # 排名 2、3、4 可成交；1（涨停）和 5（名额满）不算
        self.assertEqual(day['top3']['mean_return_pct'], 2.0)


class BarrierSideTests(unittest.TestCase):
    """策略前 10 名和随机抽样必须套同一把尺子（同一套中性 ATR 出场规则），主指标才有意义。"""

    def setUp(self):
        self.dates = weekdays(40)
        self.series, self.bench = market(self.dates, {'up': (10, 0.03), 'down': (10, -0.03), 'flat': (10, 0.0)})
        self.cutoff = self.dates[20]

    def forecast(self, fid, symbol, rank, archive_only=False):
        return {'id': fid, 'symbol': symbol, 'as_of': self.cutoff, 'rank': rank, 'archive_only': archive_only,
                'selection_version': 'select-0.4'}

    def test_strategy_days_carry_barrier_results_computed_like_the_random_draws(self):
        fcs = [self.forecast('f1', 'up', 1), self.forecast('f2', 'down', 2), self.forecast('f3', 'flat', 3, archive_only=True)]
        day = bl.strategy_by_cutoff([], fcs, 'select-0.4', self.series, self.bench)[self.cutoff]
        self.assertEqual(day['top10']['barrier_n'], 3)
        self.assertEqual(day['top3']['barrier_n'], 2)                                        # 仅留档的不算"可成交"
        random_like = bl.label_draw(['up', 'down', 'flat'], self.cutoff, self.series, self.bench)
        self.assertEqual(day['top10']['barrier_net_pess'], random_like['barrier_net_pess'])  # 同一批票、同一把尺子：结果必须一致
        self.assertEqual(day['top10']['barrier_stop_pct'], random_like['barrier_stop_pct'])

    def test_barrier_needs_series_and_ignores_other_versions(self):
        fcs = [self.forecast('f1', 'up', 1), {**self.forecast('f2', 'down', 2), 'selection_version': 'select-0.3'}]
        without = bl.strategy_by_cutoff([], fcs, 'select-0.4')
        self.assertEqual(without, {})                                                        # 不传 series：没有屏障结果，也不瞎造
        day = bl.strategy_by_cutoff([], fcs, 'select-0.4', self.series, self.bench)[self.cutoff]
        self.assertEqual(day['top10']['barrier_n'], 1)

    def test_primary_metric_is_the_barrier_difference_not_the_fixed_label_excess(self):
        days = weekdays(20)
        base = {(d, 'strong_industry'): {**group(40.0, 0.0, -1.0, 30, barrier=0.0)} for d in days}
        wob = [0.05, -0.05, 0.03, -0.03] * 5
        strat = {d: {'top10': group(50.0, 1.0, 3.0 + w, 10, barrier=2.0 + w), 'top3': group(50.0, 1.0, 3.0, 3, barrier=2.0)}
                 for d, w in zip(days, wob)}
        s = bl.summarize(base, strat, 'forward')
        self.assertAlmostEqual(s['primary_result']['mean'], 2.0, places=2)                   # 屏障 2.0 − 0.0，而不是超额 3.0 − (−1.0) = 4.0
        self.assertEqual(s['primary_result']['verdict'], 'better')
        fixed = s['baselines']['strong_industry']['vs']['top10']['mean_excess_pp']
        self.assertAlmostEqual(fixed['mean'], 4.0, places=2)                                 # 固定标签的差值也在，只是次要

    def test_informational_baselines_pair_only_with_the_top10(self):
        days = weekdays(20)
        base = {(d, n): group(40.0, 0.0, 0.0, 30) for d in days for n in bl.NAMES}
        s = bl.summarize(base, {d: strat_day(50.0, 1.0, 1.0) for d in days}, 'forward')
        self.assertEqual(sorted(s['baselines']['weak_industry']['vs']), ['top10'])
        self.assertEqual(sorted(s['baselines']['universe']['vs']), ['top10', 'top3'])


class StrategySideTests(unittest.TestCase):
    def test_grouping_version_horizon_and_rank(self):
        forecasts = [{'id': 'p%d' % i, 'as_of': '2026-09-18'} for i in range(6)]
        outs = [{'prediction_id': 'p%d' % i, 'horizon': 3, 'selection_version': 'select-0.4', 'rank': i + 1,
                 'win': i % 2 == 0, 'return_pct': float(i), 'excess_pp': float(i) - 1} for i in range(5)]
        outs.append({'prediction_id': 'p5', 'horizon': 3, 'selection_version': 'select-0.3', 'rank': 1,
                     'win': True, 'return_pct': 99.0, 'excess_pp': 99.0})           # 旧版本：不并入
        outs.append({'prediction_id': 'p0', 'horizon': 10, 'selection_version': 'select-0.4', 'rank': 1,
                     'win': True, 'return_pct': 99.0, 'excess_pp': 99.0})           # 别的窗口：不并入
        outs.append({'prediction_id': 'p1', 'horizon': 3, 'selection_version': 'select-0.4', 'rank': None,
                     'win': True, 'return_pct': 99.0, 'excess_pp': 99.0})           # 没有排名：不并入
        day = bl.strategy_by_cutoff(outs, forecasts, 'select-0.4')['2026-09-18']
        self.assertEqual((day['top10']['n_labeled'], day['top3']['n_labeled']), (5, 3))       # 旧记录没有 archive_only：按排名前 3 算
        self.assertEqual(day['top10']['mean_return_pct'], 2.0)
        self.assertEqual(day['top3']['mean_return_pct'], 1.0)


def synthetic_world(n_dates=45):
    """两个行业各 20 只：A 行业涨得快（强势行业），B 行业涨得慢。"""
    dates = weekdays(n_dates)
    stocks, prices = [], {}
    rng = random.Random(7)
    for industry, drift in (('A', 0.006), ('B', -0.001)):
        for i in range(20):
            code = '6%s%04d' % ('0' if industry == 'A' else '1', i)
            stocks.append({'ts_code': code + '.SH', 'name': 'S' + code, 'industry': industry, 'list_status': 'L',
                           'exchange': 'SSE', 'list_date': '20100101'})
            prices['sh' + code] = (10 + rng.random(), drift + rng.uniform(-0.004, 0.004))
    series, benchmark = market(dates, prices)
    for bars in series.values():                                      # 让成交量有波动，避免全部同一值
        for j, b in enumerate(bars):
            b['volume_raw'] = str(1000 + (j * 37) % 200 + rng.randint(0, 100))
    return stocks, series, benchmark, dates


class RunDailyTests(unittest.TestCase):
    def setUp(self):
        self.history = Path(tempfile.mkdtemp())
        self.stocks, self.series, self.bench, self.dates = synthetic_world()

    def run_daily(self, cutoff_index, complete=True, series=None, benchmark=None):
        bench = benchmark or self.bench[:cutoff_index + 1 + 8]        # 比最长持有期多留几天
        return bl.run_daily(self.history, self.stocks, series or self.series, bench, self.dates[cutoff_index], complete,
                            [], [], NOW, 'select-0.4', immutable, read)

    def frozen(self):
        return {p.name: read(p) for p in sorted((self.history / 'baselines').glob('select-*.json'))}

    def test_draws_are_frozen_from_the_right_pools_and_never_redrawn(self):
        self.run_daily(30)
        frozen = self.frozen()
        self.assertEqual(sorted(frozen), ['select-0.4-%s-strong_industry.json' % self.dates[30],
                                          'select-0.4-%s-universe.json' % self.dates[30],
                                          'select-0.4-%s-weak_industry.json' % self.dates[30]])      # 只有两个行业：没有"中间行业"，空池不冻结
        weak = frozen['select-0.4-%s-weak_industry.json' % self.dates[30]]
        self.assertTrue(all(s.startswith('sh61') for s in weak['symbols']), weak['symbols'])        # 最弱行业 B
        strong = frozen['select-0.4-%s-strong_industry.json' % self.dates[30]]
        universe = frozen['select-0.4-%s-universe.json' % self.dates[30]]
        self.assertEqual(len(universe['symbols']), 30)
        self.assertTrue(all(s.startswith('sh60') for s in strong['symbols']), strong['symbols'])   # 只来自强势行业 A
        self.assertEqual(strong['kind'], 'forward')
        before = self.frozen()
        self.run_daily(30)                                             # 同一天重跑
        self.assertEqual(self.frozen(), before)

    def test_incomplete_data_freezes_nothing(self):
        self.run_daily(30, complete=False)
        self.assertEqual(self.frozen(), {})

    def test_results_appear_only_when_the_window_has_played_out(self):
        early = bl.run_daily(self.history, self.stocks, self.series, self.bench[:32], self.dates[30], True, [], [], NOW,
                             'select-0.4', immutable, read)
        self.assertEqual((early['frozen'], early['resolved']), (3, 0))          # 只走了 1 个间隔：还不能验收
        late = self.run_daily(30)                                               # 走完了
        self.assertEqual((late['frozen'], late['resolved']), (3, 3))
        out = sorted((self.history / 'baseline_outcomes').glob('*.json'))
        self.assertEqual(len(out), 3)
        recs = {read(p)['name']: read(p) for p in out}
        self.assertEqual((recs['universe']['entry_day'], recs['universe']['end_day']), (self.dates[31], self.dates[31 + bl.MATURITY]))
        self.assertEqual((recs['universe']['n_labeled'], recs['strong_industry']['n_labeled']), (30, 20))   # 强势行业只有 20 只，全取

    def test_resolved_results_are_frozen_too(self):
        self.run_daily(30)
        path = next((self.history / 'baseline_outcomes').glob('*strong_industry.json'))
        before = read(path)
        changed = {s: [{**b, 'close': '1'} for b in bars] for s, bars in self.series.items()}
        self.run_daily(30, series=changed)
        self.assertEqual(read(path), before)

    def test_summary_reports_gate_state_and_counts(self):
        s = self.run_daily(30)
        b = s['baselines']['strong_industry']
        self.assertEqual((b['days'], b['labeled']), (1, 20))                    # 强势行业池只有 20 只：全取
        self.assertIsNone(b['base_rate'])                                       # 1 个日期组 < 15


def multi_industry_world(n_dates=45, n_industries=12, per=6):
    dates = weekdays(n_dates)
    stocks, prices = [], {}
    rng = random.Random(3)
    for k in range(n_industries):
        drift = 0.007 - k * 0.001                                     # 行业 0 最强，行业 11 最弱
        for i in range(per):
            code = '6%02d%03d' % (k, i)
            stocks.append({'ts_code': code + '.SH', 'name': 'S' + code, 'industry': 'I%02d' % k, 'list_status': 'L',
                           'exchange': 'SSE', 'list_date': '20100101'})
            prices['sh' + code] = (10 + rng.random(), drift + rng.uniform(-0.002, 0.002))
    series, benchmark = market(dates, prices)
    for bars in series.values():
        for j, b in enumerate(bars):
            b['volume_raw'] = str(1000 + (j * 37) % 200 + rng.randint(0, 100))
    return stocks, series, benchmark, dates


class IndustryBucketTests(unittest.TestCase):
    def test_strong_mid_and_weak_pools_partition_the_universe(self):
        stocks, series, bench, dates = multi_industry_world()
        sets, env = bl.candidate_sets(stocks, series, bench, dates[30])
        universe, strong, mid, weak = (set(sets[k]) for k in ('universe', 'strong_industry', 'mid_industry', 'weak_industry'))
        self.assertTrue(strong and mid and weak)
        self.assertEqual(strong | mid | weak, universe)
        self.assertFalse(strong & mid or strong & weak or mid & weak)
        industry = {'sh' + s['ts_code'][:6]: s['industry'] for s in stocks}
        self.assertTrue(all(industry[c] in ('I00', 'I01', 'I02') for c in strong))          # 涨得最快的几个行业（行业间有噪声，邻近的可能换位）
        self.assertTrue(all(industry[c] in ('I09', 'I10', 'I11') for c in weak))            # 涨得最慢的几个行业
        self.assertGreaterEqual(len(mid), 6 * 6)

    def test_extra_baselines_are_informational_and_only_pair_with_the_top10(self):
        self.assertEqual(bl.TIERS_FOR['mid_industry'], ('top10',))
        self.assertEqual(bl.TIERS_FOR['weak_industry'], ('top10',))
        self.assertEqual(bl.PRIMARY[:2], ('top10', 'strong_industry'))                       # 主指标不受它们影响


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.history = Path(tempfile.mkdtemp())
        self.world = synthetic_world(60)

    def go(self, days):
        stocks, series, bench, _ = self.world
        return bl.backfill(self.history, days, NOW, immutable, read, world=(stocks, series, bench))

    def test_backfill_writes_marked_records_and_is_resumable(self):
        self.assertEqual(self.go(3), 3)
        files = sorted((self.history / 'baselines' / 'backfill').glob('*.json'))
        self.assertEqual(len(files), 3)
        rec = read(files[0])
        self.assertEqual(rec['kind'], 'backfill')
        self.assertIn('幸存者', rec['limitations'])
        self.assertEqual(sorted(rec['baselines']), ['mid_industry', 'strong_industry', 'universe', 'weak_industry'])
        self.assertIn('strategy', rec)
        self.assertEqual(self.go(3), 0)                                          # 已存在的跳过
        self.assertEqual(self.go(5), 2)                                          # 只补新增的两天

    def test_backfill_never_touches_the_forward_folders(self):
        self.go(3)
        self.assertEqual(list((self.history / 'baselines').glob('select-*.json')), [])
        self.assertFalse((self.history / 'baseline_outcomes').exists())

    def test_last_cutoffs_are_only_those_whose_window_has_played_out(self):
        self.go(500)
        dates = self.world[3]
        newest = max(p.stem for p in (self.history / 'baselines' / 'backfill').glob('*.json'))
        self.assertLessEqual(dates.index(newest), len(dates) - bl.HORIZON - 2)

    def test_summary_of_backfill_is_labelled_and_separate(self):
        self.go(30)
        s = bl.summarize_backfill(self.history, read)
        self.assertEqual(s['kind'], 'backfill')
        self.assertIn('幸存者', s['limitations'])


class RenderTests(unittest.TestCase):
    def summary(self, verdict='indistinguishable', with_data=True):
        days = weekdays(20)
        base_exc = 0.0 if verdict == 'indistinguishable' else -1.0
        base = {(d, n): base_day(40.0, 0.0, base_exc) for d in days for n in bl.NAMES}
        wob = [0.5, -0.5, 0.4, -0.4] * 5 if verdict == 'indistinguishable' else [0.02, -0.02, 0.01, -0.01] * 5
        strat = {d: strat_day(50.0, 1.0, (0.0 if verdict == 'indistinguishable' else 1.0) + w) for d, w in zip(days, wob)}
        s = bl.summarize(base, strat if with_data else {}, 'forward')
        s['backfill'] = None
        return s

    def test_headline_is_the_preregistered_primary_metric_and_says_what_it_means(self):
        text = '\n'.join(bl.render(self.summary('indistinguishable')))
        self.assertIn('目前无法区分策略与随机', text)
        self.assertIn('预先定死', text.replace('看数据之前就定死的', '预先定死'))
        self.assertIn('底数不是 50%', text)
        better = '\n'.join(bl.render(self.summary('better')))
        self.assertIn('策略优于随机', better)

    def test_small_samples_render_without_numbers_and_without_crashing(self):
        text = '\n'.join(bl.render(self.summary(with_data=False)))
        self.assertIn('样本不足', text)
        self.assertNotIn('None', text)


class EngineWiringTests(unittest.TestCase):
    def test_report_carries_the_baseline_and_failure_is_contained(self):
        from test_engine_wiring import World, weekdays as wd
        world = World(wd(70))
        r = world.run('20260919000000-1')
        self.assertIn('baseline', r)
        with patch.object(bl, 'run_daily', side_effect=RuntimeError('boom')):
            r2 = world.run('20260919000001-1')
        self.assertIsNone(r2['baseline'])
        self.assertTrue(any('随机基线' in i and 'boom' in i for i in r2['issues']))
        self.assertEqual(len(r2['forecasts']), 2)

    def test_render_includes_the_section(self):
        from test_engine_wiring import World, weekdays as wd
        r = World(wd(70)).run('20260919000000-1')
        r['baseline'] = RenderTests().summary()
        self.assertIn('## 随机基线', alpha_engine.render(r))


if __name__ == '__main__':
    unittest.main()
