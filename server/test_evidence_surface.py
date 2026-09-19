import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import alpha_engine
import conditional_exec as ce
import contract_labels as cl
import dashboard_export
import dashboard_page
from decision_view import decisions
from spec_fixtures import build_spec
from test_contract_labels import outcome, record

NOW = datetime.fromisoformat('2026-09-22T15:35:00+08:00')
PLAN_ID = 'select-0.4-2026-09-21-sh600520'


def ledger():
    led = ce.new_ledger(NOW)
    led.update(cash=71000.0, equity=101250.0, peak=102000.0, drawdown_pct=0.7353)
    led['positions'] = [{'id': PLAN_ID, 'symbol': 'sh600520', 'name': '中发', 'track': 'breakout', 'shares': 2500,
                         'entry_day': '2026-09-22', 'entry_price': 11.6, 'cost': 29050, 'stop_base': 11.31,
                         'target_price': 12.41, 'breakeven_price': 11.64, 'breakeven_armed': True, 'mark': 11.9}]
    led['trades'] = [{'id': 'x-exit', 'prediction_id': 'other', 'symbol': 'sz000797', 'side': 'sell', 'date': '2026-09-22',
                      'price': 9.72, 'shares': 3000, 'fee': 20, 'pnl': -812.5, 'return_pct': -2.71, 'reason': 'stop'}]
    led['plans'] = {PLAN_ID: {'id': PLAN_ID, 'symbol': 'sh600520', 'name': '中发', 'track': 'breakout', 'rank': 1,
                              'day': '2026-09-22', 'status': 'filled', 'reason': '成交'}}
    return led


class SnapshotTests(unittest.TestCase):
    def test_snapshot_summarises_the_ledger_without_mutating_it(self):
        led = ledger()
        before = json.dumps(led, sort_keys=True)
        acc = ce.account_snapshot(led, NOW)
        self.assertEqual(json.dumps(led, sort_keys=True), before)
        self.assertEqual((acc['equity'], acc['nav'], acc['paused']), (101250.0, 1.0125, False))
        p = acc['positions'][0]
        self.assertEqual((p['stop'], p['breakeven_armed'], p['unrealized_pct']), (11.64, True, round((11.9 / 11.6 - 1) * 100, 3)))
        self.assertEqual(acc['plan_counts'], {'filled': 1})

    def test_win_rate_is_withheld_until_enough_closed_trades(self):
        acc = ce.account_snapshot(ledger(), NOW)
        self.assertEqual((acc['trade_stats']['closed'], acc['trade_stats']['wins'], acc['trade_stats']['win_rate_pct']), (1, 0, None))
        led = ledger()
        led['trades'] = [{**led['trades'][0], 'id': 't%d' % i, 'pnl': 1.0 if i % 2 else -1.0} for i in range(30)]
        self.assertEqual(ce.account_snapshot(led, NOW)['trade_stats']['win_rate_pct'], 50.0)

    def test_curve_appends_today_and_overwrites_a_same_day_rerun(self):
        prior = [{'date': '2026-09-21', 'equity': 100000.0, 'nav': 1.0}, {'date': '2026-09-22', 'equity': 1.0, 'nav': 0.0}]
        curve = ce.account_snapshot(ledger(), NOW, prior)['curve']
        self.assertEqual([c['date'] for c in curve], ['2026-09-21', '2026-09-22'])
        self.assertEqual(curve[-1]['equity'], 101250.0)

    def test_render_states_the_two_accounts_are_separate(self):
        text = '\n'.join(ce.render_account(ce.account_snapshot(ledger(), NOW)))
        self.assertIn('不得混算', text)
        self.assertIn('不是你的真实持仓', text)
        self.assertIn('胜率暂不显示', text)
        self.assertIn('止损', text)


class DecisionViewTests(unittest.TestCase):
    def agent(self, plan_status='filled', with_position=True, trades=None):
        led = ledger()
        led['plans'][PLAN_ID]['status'] = plan_status
        led['plans'][PLAN_ID]['reason'] = '有效时段结束仍未触发'
        led['plans'][PLAN_ID]['last_note'] = '现价 10.1 尚未向上确认'
        if not with_position:
            led['positions'] = []
        if trades:
            led['trades'] = trades
        f = {'id': PLAN_ID, 'symbol': 'sh600520', 'name': '中发', 'created_at': '2026-09-21T15:40:00+08:00',
             'paper_eligible': True, 'reference_price': 10.0, 'strategy_type': 'breakout',
             'execution_mode': 'conditional-intraday-v1', 'exec_spec': build_spec('breakout'), 'eligible_from': '2026-09-22'}
        return {'candidates': [], 'forecasts': [f], 'portfolio': {'positions': [], 'trades': [], 'valuation_status': 'current',
                                                                 'paused': False},
                'exec02': ce.account_snapshot(led, NOW)}

    def test_filled_plan_shows_the_exec02_position_not_exec01_guesses(self):
        d = decisions(self.agent())[0]
        self.assertEqual((d['state'], d['shares'], d['entry_price'], d['stop_price'], d['target_price']),
                         ('holding', 2500, 11.6, 11.64, 12.41))
        self.assertIn('保本止损已武装', d['reasons'][0])

    def test_closed_plan_shows_its_exit(self):
        sell = {'prediction_id': PLAN_ID, 'symbol': 'sh600520', 'side': 'sell', 'date': '2026-09-23', 'price': 12.4,
                'pnl': 900.0, 'return_pct': 3.1, 'reason': 'target'}
        d = decisions(self.agent(with_position=False, trades=[sell]))[0]
        self.assertEqual(d['state'], 'exited')
        self.assertIn('止盈', d['reasons'][0])

    def test_expired_plan_is_not_shown_as_waiting_forever(self):
        d = decisions(self.agent('expired', with_position=False))[0]
        self.assertEqual(d['state'], 'skipped')
        self.assertIn('过期', d['reasons'][0])
        self.assertIn('不会补买', d['reasons'][1])

    def test_watching_plan_is_waiting(self):
        d = decisions(self.agent('watching', with_position=False))[0]
        self.assertEqual(d['state'], 'waiting_buy')
        self.assertIn('尚未向上确认', d['reasons'][0])

    def test_entry_zone_is_the_tracks_own_zone_not_the_legacy_symmetric_band(self):
        d = decisions(self.agent('watching', with_position=False))[0]
        self.assertEqual((d['entry_low'], d['entry_high']), (10.05, 10.3))       # 突破：+0.5% ~ +3%，不是 ±3%
        pb = self.agent('watching', with_position=False)
        pb['forecasts'][0].update(strategy_type='pullback', exec_spec=build_spec('pullback'))
        d = decisions(pb)[0]
        self.assertEqual((d['entry_low'], d['entry_high']), (None, 10.1))         # 回调：只有上限

    def test_reports_without_the_new_sections_still_work(self):
        a = self.agent()
        del a['exec02']
        self.assertEqual(decisions(a)[0]['state'], 'waiting_buy')                 # 旧行为不变
        a['forecasts'][0]['execution_mode'] = None
        self.assertEqual(decisions(a)[0]['state'], 'waiting_buy')


class EngineTests(unittest.TestCase):
    def test_early_report_has_the_new_keys_so_consumers_never_keyerror(self):
        r = alpha_engine.run(Path(tempfile.mkdtemp()), '20260922000000-1')
        self.assertIsNone(r['evidence'])
        self.assertIsNone(r['exec02'])

    def test_render_includes_both_sections_when_present(self):
        r = alpha_engine.run(Path(tempfile.mkdtemp()), '20260922000000-2')
        r['exec02'] = ce.account_snapshot(ledger(), NOW)
        recs = [record(i) for i in range(3)]
        r['evidence'] = cl.summarize(recs, [outcome(i) for i in range(3)], 'select-0.4', 'exec-0.2')
        text = alpha_engine.render(r)
        self.assertIn('## 盘中条件执行虚拟账户', text)
        self.assertIn('## 证据三层', text)
        self.assertIn('研究标签', text)
        self.assertNotIn('暂未并入本报告', text)


class ExportTests(unittest.TestCase):
    def test_dashboard_data_carries_exec02_and_evidence_and_tolerates_their_absence(self):
        from tushare_sync import save
        h = Path(tempfile.mkdtemp())
        report = alpha_engine.run(h, '20260922000000-3')
        report.update(portfolio={'positions': [], 'trades': [], 'curve': [], 'valuation_status': 'current', 'paused': False,
                                 'equity': 1, 'cash': 1, 'last_date': '2026-09-22', 'excess_pp': 0, 'max_drawdown_pct': 0,
                                 'trade_win_rate': None},
                      status='ready', screen=None, candidates=[], calibration=None, calibration_short=None)
        save(h / 'agent' / '20260922000000-3.json', report)
        out = dashboard_export.build(h)['agent']
        self.assertIsNone(out['exec02'])
        self.assertIsNone(out['evidence'])
        report.update(exec02=ce.account_snapshot(ledger(), NOW),
                      evidence=cl.summarize([record(1)], [], 'select-0.4', 'exec-0.2'))
        save(h / 'agent' / '20260922000001-3.json', {**report, 'id': '20260922000001-3', 'generated_at': '2026-09-22T16:00:00+08:00'})
        out = dashboard_export.build(h)['agent']
        self.assertEqual(out['exec02']['equity'], 101250.0)
        self.assertIn('breakout/top3', out['evidence']['groups'])

    def test_template_has_no_duplicate_top_level_declarations(self):
        """页面脚本里一个重复的 const 会让整个看板不渲染——上次就差点这样。"""
        import re
        html = Path(dashboard_page.TEMPLATE_PATH).read_text(encoding='utf-8')
        script = html[html.index('<script>'):]
        names = re.findall(r'^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)', script, flags=re.M)
        dupes = {n for n in names if names.count(n) > 1}
        self.assertEqual(dupes, set())


if __name__ == '__main__':
    unittest.main()
