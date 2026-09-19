import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, date
from pathlib import Path

import conditional_exec as ce
import exec_spec
import intraday_engine as ie
from collect_quotes import CST
from exec_spec import EXEC_MODE, build_spec, breakeven_win_rate, entry_zone
from shortterm_model import SHORT_POLICY

DATES = ['2026-09-17', '2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23',
         '2026-09-24', '2026-09-25', '2026-09-28']
D1, D2, D3, D4 = '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24'


def at(day, h, m, s=0):
    y, mo, d = (int(x) for x in day.split('-'))
    return datetime(y, mo, d, h, m, s, tzinfo=CST)


def bars(n=30, close=10.0, end='2026-09-18'):
    """截至上一交易日的日线，收盘价恒为 close（MA20 = close）。"""
    day, rows = date.fromisoformat(end), []
    while len(rows) < n:
        if day.weekday() < 5:
            rows.append({'date': day.isoformat(), 'open': str(close), 'close': str(close),
                         'high': str(close), 'low': str(close), 'volume_raw': '1000'})
        day -= timedelta(days=1)
    return list(reversed(rows))


def quote(symbol='sz000001', last=10.0, day=D1, h=10, m=0, prev_close=10.0, limit_up=11.0,
          limit_down=9.0, age=5):
    when = at(day, h, m)
    return {'symbol': symbol, 'market': 'cn', 'name': symbol, 'last': str(last),
            'previous_close': str(prev_close), 'open': str(last), 'high': str(last), 'low': str(last),
            'change_pct': '0', 'quote_date': day, 'quote_at': when.isoformat(), 'age_seconds': age,
            'is_index': False, 'limit_up': str(limit_up), 'limit_down': str(limit_down),
            'batch_sha256': 'abc123', 'batch_fetched_at': when.isoformat()}


def forecast(symbol='sz000001', track='breakout', ref=10.0, rank=1, paper=True, fid=None,
             created='2026-09-18T18:30:00+08:00', as_of='2026-09-18', eligible='2026-09-20',
             mode=EXEC_MODE):
    return {'id': fid or 'select-0.4-2026-09-18-' + symbol, 'symbol': symbol, 'name': symbol,
            'strategy_type': track, 'reference_price': ref, 'rank': rank, 'paper_eligible': paper,
            'created_at': created, 'as_of': as_of, 'eligible_from': eligible,
            'execution_mode': mode, 'exec_spec': build_spec(track)}


def stub_position(symbol, track='breakout', entry_day='2026-09-18'):
    """字段齐全的占位持仓——真实账本里的持仓一定带 spec/止损位等。"""
    return {'id': 'stub-' + symbol, 'symbol': symbol, 'name': symbol, 'track': track, 'shares': 100,
            'entry_day': entry_day, 'entry_price': 10.0, 'cost': 1000.0, 'stop_base': 9.0,
            'target_price': 12.0, 'breakeven_price': 10.05, 'breakeven_armed': False,
            'peak_price': 10.0, 'mark': 10.0, 'spec': build_spec(track)}


class Harness:
    def __init__(self, forecasts=None, series=None, dates=DATES):
        self.dir = Path(tempfile.mkdtemp())
        self.forecasts = forecasts if forecasts is not None else [forecast()]
        self.series = series if series is not None else {}
        self.ex = ce.Executor(Path('.'), self.dir, plans_fn=lambda prev: self.forecasts,
                              dates_fn=lambda: dates, series_fn=lambda s: self.series.get(s, bars()))

    def tick(self, q, now=None, dry_run=False):
        q = q if isinstance(q, list) else [q]
        now = now or datetime.fromisoformat(q[0]['quote_at'])
        if self.ex.ledger is None or now.date().isoformat() != self.ex.ledger.get('_day'):
            self.ex.prepare(now, dry_run)
            self.ex.ledger['_day'] = now.date().isoformat()
        return self.ex.step({'now': now, 'quotes': {x['symbol']: x for x in q}, 'dry_run': dry_run})

    @property
    def ledger(self):
        return self.ex.ledger

    def new_day(self):
        self.ex.ledger = None      # 模拟下一天新起一个进程：重新从磁盘加载账本


# ---------------------------------------------------------------------------

class SpecTests(unittest.TestCase):
    def test_breakeven_win_rates_pin_the_risk_reward_structure(self):
        """改止损止盈时不能无意破坏盈亏比：突破 29.6%，回调 41.4%（与旧的统一 −3%/+5% 相同）。"""
        self.assertAlmostEqual(breakeven_win_rate(exec_spec.SPECS['breakout']['exit']), 29.6, delta=0.05)
        self.assertAlmostEqual(breakeven_win_rate(exec_spec.SPECS['pullback']['exit']), 41.4, delta=0.05)

    def test_unknown_track_is_an_error_not_a_default_spec(self):
        with self.assertRaises(KeyError):
            build_spec('mystery')

    def test_build_spec_returns_an_independent_copy(self):
        a = build_spec('breakout')
        a['exit']['stop_pct'] = 0.99
        self.assertEqual(exec_spec.SPECS['breakout']['exit']['stop_pct'], 0.025)

    def test_entry_zones(self):
        lo, hi, void = entry_zone(exec_spec.SPECS['breakout']['entry'], 10.0)
        self.assertEqual((round(lo, 4), round(hi, 4), round(void, 4)), (10.05, 10.3, 9.7))
        lo, hi, void = entry_zone(exec_spec.SPECS['pullback']['entry'], 10.0)
        self.assertEqual((lo, round(hi, 4), round(void, 4)), (None, 10.1, 9.7))


class AdmitTests(unittest.TestCase):
    def admit(self, **kw):
        return ce.admit(forecast(**kw), D1, '2026-09-18', DATES)

    def test_valid_plan_is_admitted(self):
        plan, why = self.admit()
        self.assertEqual((plan['status'], why), ('watching', None))

    def test_research_only_and_legacy_plans_are_none_of_our_business(self):
        self.assertEqual(self.admit(paper=False), (None, None))           # 只做研究留档
        self.assertEqual(self.admit(mode='observed-quote-v1'), (None, None))

    def test_stale_plan_is_recorded_as_skipped_not_silently_dropped(self):
        for kw, text in (({'as_of': '2026-09-17'}, '上一交易日'),
                         ({'eligible': '2026-09-22'}, '首个允许交易日'),
                         ({'created': '2026-09-21T09:25:00+08:00'}, '09:20')):
            plan, why = self.admit(**kw)
            self.assertIn(text, why)


class EntryBreakoutTests(unittest.TestCase):
    def test_not_yet_confirmed_upward_waits(self):
        h = Harness()
        signals = h.tick(quote(last=10.02))                                # < ref×1.005 = 10.05
        self.assertEqual(h.ledger['trades'], [])
        plan = next(iter(h.ledger['plans'].values()))
        self.assertEqual(plan['status'], 'watching')
        watch = next(s for s in signals if s['kind'] == 'entry-watch')
        self.assertAlmostEqual(watch['level']['price'], 10.05)             # 差一点的记录用它
        self.assertEqual(watch['level']['direction'], 'up')

    def test_confirmed_inside_the_zone_fills_at_observed_price_plus_slippage(self):
        h = Harness()
        h.tick(quote(last=10.20))
        pos = h.ledger['positions'][0]
        self.assertEqual(pos['entry_price'], round(10.20 * 1.001, 2))      # 10.21，不是 10.20
        self.assertEqual(pos['shares'] % 100, 0)
        self.assertLessEqual(pos['shares'] * pos['entry_price'], 100000 * SHORT_POLICY['max_weight'])
        self.assertEqual(h.ledger['trades'][0]['batch_sha256'], 'abc123')  # 带证据
        self.assertLess(h.ledger['cash'], 100000)

    def test_chasing_above_the_cap_does_not_fill_but_the_plan_stays_alive(self):
        """要的是"状态约束"而不是"时间约束"：现在太高不买，但价格回到区间内仍然可以买。"""
        h = Harness()
        h.tick(quote(last=10.40, m=0))
        self.assertEqual(h.ledger['trades'], [])
        self.assertEqual(next(iter(h.ledger['plans'].values()))['status'], 'watching')
        h.tick(quote(last=10.20, m=5))
        self.assertEqual(len(h.ledger['positions']), 1)

    def test_breakdown_voids_the_plan_for_the_day(self):
        h = Harness()
        h.tick(quote(last=9.70, m=0))                                      # ≤ ref×0.97
        plan = next(iter(h.ledger['plans'].values()))
        self.assertEqual(plan['status'], 'voided')
        h.tick(quote(last=10.20, m=5))                                     # 之后再回来也不买
        self.assertEqual(h.ledger['positions'], [])

    def test_no_entry_before_the_window_and_expiry_after_it(self):
        h = Harness()
        h.tick(quote(last=10.20, h=9, m=29))
        self.assertEqual(h.ledger['positions'], [])
        h.tick(quote(last=10.02, h=13, m=59))
        self.assertEqual(next(iter(h.ledger['plans'].values()))['status'], 'watching')
        h.tick(quote(last=10.20, h=14, m=0))                               # 窗口结束：即使价格合适也过期
        self.assertEqual(next(iter(h.ledger['plans'].values()))['status'], 'expired')
        self.assertEqual(h.ledger['positions'], [])

    def test_fills_only_once(self):
        h = Harness()
        h.tick(quote(last=10.20, m=0))
        h.tick(quote(last=10.20, m=1))
        h.tick(quote(last=10.21, m=2))
        self.assertEqual(len(h.ledger['positions']), 1)
        self.assertEqual(sum(t['side'] == 'buy' for t in h.ledger['trades']), 1)

    def test_no_quote_means_no_action(self):
        h = Harness()
        h.tick([], now=at(D1, 10, 0))
        self.assertEqual(h.ledger['trades'], [])


class EntryPullbackTests(unittest.TestCase):
    def pullback(self, **kw):
        return Harness([forecast(track='pullback', **kw)])

    def test_buys_at_or_below_reference_but_never_chases(self):
        h = self.pullback()
        h.tick(quote(last=10.20, m=0))                                     # 高于 ref×1.01
        self.assertEqual(h.ledger['positions'], [])
        h.tick(quote(last=10.05, m=1))
        self.assertEqual(len(h.ledger['positions']), 1)

    def test_breaking_ma20_voids_the_pullback(self):
        h = Harness([forecast(track='pullback')], series={'sz000001': bars(close=10.0)})
        h.tick(quote(last=9.95, m=0))            # 未破 ref×0.97，但低于 MA20≈10.0 → 回调变成了破位
        self.assertEqual(next(iter(h.ledger['plans'].values()))['status'], 'voided')
        self.assertIn('MA20', next(iter(h.ledger['plans'].values()))['reason'])

    def test_unverifiable_ma20_means_no_fill(self):
        h = Harness([forecast(track='pullback')], series={'sz000001': bars(n=5)})   # 不足20根
        h.tick(quote(last=10.0, m=0))
        self.assertEqual(h.ledger['positions'], [])

    def test_stop_is_the_higher_of_minus_3pct_and_ma20_at_fill(self):
        h = Harness([forecast(track='pullback')], series={'sz000001': bars(close=10.0)})
        h.tick(quote(last=10.05, m=0))           # MA20≈10.0，入场 10.06，−3% = 9.76，MA20 更高
        pos = h.ledger['positions'][0]
        self.assertGreater(pos['stop_base'], pos['entry_price'] * 0.97)
        self.assertAlmostEqual(pos['stop_base'], pos['ma20_at_fill'], places=3)


class FrozenSpecTests(unittest.TestCase):
    """执行器只读**记录里冻结的那份规格**，不读 exec_spec 里的实时常量。否则以后调一下常量，
    所有已经冻结、还没执行的计划就会悄悄换了规则——和"预测记录不可改写"是同一条原则。"""

    def custom(self, **overrides):
        f = forecast()
        f['exec_spec']['exit'].update(stop_pct=0.05, target_pct=0.20)
        f['exec_spec']['entry'].update(min_pct=0.02, max_pct=0.06)
        for section, patch in overrides.items():
            f['exec_spec'][section].update(patch)
        return f

    def test_exit_levels_come_from_the_frozen_spec(self):
        h = Harness([self.custom()])
        h.tick(quote(last=10.30))                     # 落在冻结的入场区间 [10.20, 10.60] 内
        pos = h.ledger['positions'][0]
        self.assertAlmostEqual(pos['stop_base'], pos['entry_price'] * 0.95, places=3)     # 不是 0.975
        self.assertAlmostEqual(pos['target_price'], pos['entry_price'] * 1.20, places=3)   # 不是 1.07

    def test_entry_zone_comes_from_the_frozen_spec(self):
        h = Harness([self.custom()])
        h.tick(quote(last=10.10))                     # 满足常量里的 ≥10.05，但未达冻结的 ≥10.20
        self.assertEqual(h.ledger['positions'], [])

    def test_changing_the_live_constants_does_not_touch_a_frozen_plan(self):
        h = Harness()
        original = copy.deepcopy(exec_spec.SPECS)
        try:
            exec_spec.SPECS['breakout']['exit']['stop_pct'] = 0.10
            h.tick(quote(last=10.20))
        finally:
            exec_spec.SPECS.clear(); exec_spec.SPECS.update(original)
        pos = h.ledger['positions'][0]
        self.assertAlmostEqual(pos['stop_base'], pos['entry_price'] * 0.975, places=3)


class EntryRefusalTests(unittest.TestCase):
    def test_corporate_action_is_refused(self):
        """实时昨收与缓存上一交易日收盘对不上，多半是除权或缓存过期，不成交。"""
        h = Harness()
        h.tick(quote(last=10.20, prev_close=12.0))
        plan = next(iter(h.ledger['plans'].values()))
        self.assertEqual(plan['status'], 'skipped')
        self.assertIn('复权', plan['reason'])

    def test_missing_daily_cache_is_refused_not_assumed_fine(self):
        h = Harness()
        h.ex.series_fn = lambda s: []
        h.tick(quote(last=10.20))
        self.assertEqual(next(iter(h.ledger['plans'].values()))['status'], 'skipped')

    def test_near_limit_up_waits_instead_of_buying(self):
        h = Harness()
        h.tick(quote(last=10.20, limit_up=10.22))
        self.assertEqual(h.ledger['positions'], [])
        self.assertEqual(next(iter(h.ledger['plans'].values()))['status'], 'watching')   # 重试而非放弃

    def test_full_book_paused_account_and_duplicate_holding_are_skipped_with_reasons(self):
        for setup, text in (('full', '持仓已满'), ('paused', '回撤'), ('held', '已经持有')):
            h = Harness()
            h.ex.prepare(at(D1, 10, 0))
            if setup == 'full':
                h.ledger['positions'] = [stub_position('sz00010%d' % i) for i in range(3)]
            elif setup == 'paused':
                h.ledger['paused'] = True
            else:
                h.ledger['positions'] = [stub_position('sz000001', entry_day=D1)]   # 今天入场，T+1 不会卖
            h.ex.step({'now': at(D1, 10, 0), 'quotes': {'sz000001': quote(last=10.20)}})
            plan = next(iter(h.ledger['plans'].values()))
            self.assertEqual(plan['status'], 'skipped', setup)
            self.assertIn(text, plan['reason'])

    def test_star_board_needs_at_least_200_shares(self):
        h = Harness([forecast('sh688061', ref=45.0)], series={'sh688061': bars(close=45.0)})
        h.ex.prepare(at(D1, 10, 0))
        h.ledger['cash'] = h.ledger['equity'] = h.ledger['peak'] = 5000.0   # 只够 100 股（peak 也要改，否则算 95% 回撤）
        h.ex.step({'now': at(D1, 10, 0), 'quotes': {'sh688061': quote(
            'sh688061', last=45.5, prev_close=45.0, limit_up=54, limit_down=36)}})
        plan = next(iter(h.ledger['plans'].values()))
        self.assertEqual(plan['status'], 'skipped')
        self.assertIn('资金', plan['reason'])


class ExitTests(unittest.TestCase):
    def held(self, track='breakout', entry_quote=10.20):
        h = Harness([forecast(track=track)])
        h.tick(quote(last=entry_quote, day=D1, h=10, m=0))
        assert len(h.ledger['positions']) == 1
        return h

    def day2(self, h, last, prev_close=10.5, m=0, **kw):
        h.new_day()
        return h.tick(quote(last=last, day=D2, h=9, m=31 + m, prev_close=prev_close, **kw))

    def test_t_plus_one_no_selling_on_the_entry_day_even_through_the_stop(self):
        h = self.held()
        h.tick(quote(last=9.00, day=D1, h=10, m=30, limit_down=8.0))       # 远低于止损位
        self.assertEqual(len(h.ledger['positions']), 1)
        self.assertEqual(sum(t['side'] == 'sell' for t in h.ledger['trades']), 0)

    def test_stop_fills_at_the_observed_price_not_at_the_stop_level(self):
        """跳空低开穿过止损位：真实成交只会更差。假装在止损位成交是在给策略作弊。"""
        h = self.held()
        stop = h.ledger['positions'][0]['stop_base']
        self.day2(h, last=9.30)
        sell = [t for t in h.ledger['trades'] if t['side'] == 'sell'][0]
        self.assertEqual(sell['reason'], 'stop')
        self.assertEqual(sell['price'], round(9.30 * 0.999, 2))
        self.assertLess(sell['price'], stop)
        self.assertLess(sell['pnl'], 0)
        self.assertEqual(h.ledger['positions'], [])

    def test_an_unobserved_dip_never_triggers_the_stop(self):
        """两次轮询之间曾经跌到 9.50，没观测到就是没发生。"""
        h = self.held()
        self.day2(h, last=10.30, m=0)
        self.day2_again = h.tick(quote(last=10.30, day=D2, h=9, m=40, prev_close=10.5))
        self.assertEqual(len(h.ledger['positions']), 1)

    def test_take_profit(self):
        h = self.held()
        self.day2(h, last=10.95)                          # ≥ 入场价×1.07 = 10.92
        self.assertEqual([t['reason'] for t in h.ledger['trades'] if t['side'] == 'sell'], ['target'])

    def test_breakeven_price_really_breaks_even(self):
        h = self.held()
        pos = h.ledger['positions'][0]
        bp = pos['breakeven_price']
        self.assertGreater(bp, pos['entry_price'])
        self.assertGreaterEqual(ce.sell_net(bp, pos['shares'], pos['cost'], SHORT_POLICY), 0)
        self.assertLess(ce.sell_net(bp - 0.01, pos['shares'], pos['cost'], SHORT_POLICY), 0)
        # 比"往返成本 0.31%"的近似更准：入场价已含 0.1% 滑点，那个近似会高估
        self.assertLess(bp, pos['entry_price'] * 1.0031)

    def test_breakeven_arms_on_the_entry_day_but_does_not_sell(self):
        h = self.held()
        entry = h.ledger['positions'][0]['entry_price']
        h.tick(quote(last=round(entry * 1.031, 2), day=D1, h=11, m=0))
        pos = h.ledger['positions'][0]
        self.assertTrue(pos['breakeven_armed'])
        self.assertEqual(len(h.ledger['positions']), 1)

    def test_armed_breakeven_stop_exits_above_the_original_stop(self):
        h = self.held()
        pos = h.ledger['positions'][0]
        h.tick(quote(last=round(pos['entry_price'] * 1.031, 2), day=D1, h=11, m=0))   # 武装
        level = ce.Executor._stop_level(h.ex, pos)
        self.assertGreater(level, pos['stop_base'])
        self.day2(h, last=round(level - 0.02, 2), prev_close=10.6)          # 原止损位之上、保本位之下
        sell = [t for t in h.ledger['trades'] if t['side'] == 'sell'][0]
        self.assertEqual(sell['reason'], 'breakeven_stop')

    def test_breakout_day1_time_stop(self):
        h = self.held('breakout', entry_quote=10.20)
        entry = h.ledger['positions'][0]['entry_price']
        self.day2(h, last=10.30, prev_close=entry - 0.01)     # 入场日收盘 ≤ 入场价：假突破快跑
        self.assertEqual([t['reason'] for t in h.ledger['trades'] if t['side'] == 'sell'], ['time_stop_day1'])

    def test_breakout_day1_rule_does_not_fire_when_the_first_close_is_above_entry(self):
        h = self.held('breakout')
        self.day2(h, last=10.30, prev_close=10.60)
        self.assertEqual(len(h.ledger['positions']), 1)

    def test_pullback_has_no_day1_rule(self):
        h = self.held('pullback', entry_quote=10.05)
        entry = h.ledger['positions'][0]['entry_price']
        self.day2(h, last=10.10, prev_close=entry - 0.02)
        self.assertEqual(len(h.ledger['positions']), 1)

    def test_hold_expiry_counts_completed_sessions_including_the_entry_day(self):
        h = self.held('pullback', entry_quote=10.05)
        for day, expect_open in ((D2, True), (D3, True), (D4, False)):
            h.new_day()
            h.tick(quote(last=10.15, day=day, h=9, m=31, prev_close=10.12))
            self.assertEqual(len(h.ledger['positions']) == 1, expect_open, day)
        self.assertEqual([t['reason'] for t in h.ledger['trades'] if t['side'] == 'sell'], ['hold_expiry'])

    def test_limit_down_blocks_the_sale_and_keeps_the_signal(self):
        h = self.held()
        self.day2(h, last=9.00, limit_down=9.00)
        self.assertEqual(len(h.ledger['positions']), 1)
        self.assertIn('跌停', h.ledger['positions'][0]['exit_blocked'])
        h.tick(quote(last=9.20, day=D2, h=9, m=45, limit_down=9.00, prev_close=10.5))   # 打开后卖出
        self.assertEqual(h.ledger['positions'], [])

    def test_drawdown_pause_blocks_new_entries_and_queues_exits(self):
        h = self.held()
        h.ledger['peak'] = h.ledger['equity'] * 1.5            # 造一个 >20% 回撤
        ce.save_ledger(h.dir, h.ledger)                         # 每天是一个新进程，账本必须从磁盘读回
        self.day2(h, last=10.30, prev_close=10.6)
        self.assertTrue(h.ledger['paused'])
        self.assertEqual([t['reason'] for t in h.ledger['trades'] if t['side'] == 'sell'], ['drawdown_pause'])

    def test_pnl_and_cash_are_consistent(self):
        h = self.held()
        self.day2(h, last=10.95)
        buy = next(t for t in h.ledger['trades'] if t['side'] == 'buy')
        sell = next(t for t in h.ledger['trades'] if t['side'] == 'sell')
        expected_cash = 100000 - buy['price'] * buy['shares'] - buy['fee'] + sell['price'] * sell['shares'] - sell['fee']
        self.assertAlmostEqual(h.ledger['cash'], expected_cash, places=1)
        self.assertAlmostEqual(h.ledger['equity'], h.ledger['cash'], places=1)


class PersistenceTests(unittest.TestCase):
    def test_dry_run_never_writes_the_ledger(self):
        h = Harness()
        h.tick(quote(last=10.20), dry_run=True)
        self.assertFalse(ce.ledger_path(h.dir).exists())

    def test_ledger_survives_a_restart_and_is_not_refilled(self):
        h = Harness()
        h.tick(quote(last=10.20, m=0))
        h2 = Harness(); h2.dir = h.dir
        h2.ex.directory = h.dir
        h2.tick(quote(last=10.20, m=1))
        led = json.loads(ce.ledger_path(h.dir).read_text())
        self.assertEqual(len(led['positions']), 1)
        self.assertEqual(sum(t['side'] == 'buy' for t in led['trades']), 1)

    def test_yesterdays_unresolved_plan_expires_instead_of_living_on(self):
        h = Harness()
        h.tick(quote(last=10.02, day=D1, h=10, m=0))            # 观察中，当天引擎没跑到 14:00
        h.new_day()
        h.forecasts = []                                         # 新的一天没有新计划
        h.ex.prepare(at(D2, 9, 30))
        plan = next(iter(h.ledger['plans'].values()))
        self.assertEqual(plan['status'], 'expired')

    def test_corrupt_ledger_is_quarantined_not_silently_reset(self):
        h = Harness()
        h.dir.mkdir(parents=True, exist_ok=True)
        ce.ledger_path(h.dir).write_text('{broken')
        h.ex.prepare(at(D1, 10, 0))
        self.assertTrue(ce.ledger_path(h.dir).with_suffix('.broken').exists())
        self.assertTrue(any('损坏' in i for i in h.ledger['issues']))

    def test_plans_load_once_per_day(self):
        calls = []
        h = Harness()
        h.ex.plans_fn = lambda prev: calls.append(prev) or h.forecasts
        h.ex.prepare(at(D1, 9, 0)); h.ex.prepare(at(D1, 9, 31)); h.ex.prepare(at(D1, 9, 32))
        self.assertEqual(len(calls), 1)

    def test_no_plans_loaded_on_a_non_trading_day(self):
        h = Harness()
        h.ex.prepare(at('2026-09-19', 10, 0))                    # 周六，不在开市日期里
        self.assertEqual(h.ledger['plans'], {})

    def test_unavailable_calendar_is_recorded_and_nothing_is_guessed(self):
        h = Harness()
        h.ex.dates_fn = lambda: (_ for _ in ()).throw(ValueError('checksum'))
        h.ex.prepare(at(D1, 10, 0))
        self.assertEqual(h.ledger['plans'], {})
        self.assertTrue(any('日历' in i for i in h.ledger['issues']))


class EngineIntegrationTests(unittest.TestCase):
    def test_full_path_through_the_engine_notifies_once_and_watches_the_right_symbols(self):
        h = Harness()
        snapshots = iter([quote(last=10.20, m=0), quote(last=10.21, m=1)])
        events, symbols_seen = [], []
        for m in (0, 1):
            now = at(D1, 10, m)
            r = ie.run_tick(Path('.'), lambda: symbols_seen.append(1) or h.ex.watch_symbols(now),
                            now=now, directory=h.dir, calendar_fn=lambda d: 'open',
                            snapshot_fn=lambda syms, q=next(snapshots): {'quotes': [q], 'failures': []},
                            evaluators=[h.ex.step])
            events += r['events']
        entries = [e for e in events if e['kind'] == 'paper-entry']
        self.assertEqual(len(entries), 1)                        # 第二轮不重复推
        self.assertEqual(entries[0]['evidence']['batch_sha256'], 'abc123')
        self.assertEqual(len(h.ledger['positions']), 1)

    def test_near_miss_levels_are_recorded_for_watched_plans(self):
        h = Harness()
        now = at(D1, 10, 0)
        ie.run_tick(Path('.'), ['sz000001'], now=now, directory=h.dir, calendar_fn=lambda d: 'open',
                    snapshot_fn=lambda s: {'quotes': [quote(last=10.03)], 'failures': []}, evaluators=[h.ex.step])
        state = json.loads((h.dir / 'state-2026-09-21.json').read_text())
        rec = state['levels']['entry-watch:select-0.4-2026-09-18-sz000001']
        self.assertAlmostEqual(rec['closest_pct'], (10.05 / 10.03 - 1) * 100, places=3)   # "差 0.2%"
        self.assertFalse(rec['reached'])


class LegacyPathsMustNotConsumeConditionalPlansTests(unittest.TestCase):
    def test_expire_plans_leaves_conditional_plans_alone(self):
        """15:35 的日线流程不能按"09:35 窗口已过"把有效期到 14:00 的条件计划记成跳过。"""
        import expire_plans
        legacy = {**forecast(fid='legacy', mode='observed-quote-v1'), 'eligible_from': '2026-09-21'}
        cond = {**forecast(fid='cond'), 'eligible_from': '2026-09-21'}
        state = {'attempted': [], 'positions': [], 'trades': []}
        expire_plans.expire(state, [legacy, cond], DATES, at(D1, 15, 35))
        self.assertEqual(state['attempted'], ['legacy'])          # 旧的照旧被记为错过
        self.assertNotIn('cond', state['attempted'])


if __name__ == '__main__':
    unittest.main()
