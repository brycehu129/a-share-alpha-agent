import fcntl
import json
import tempfile
import unittest
from datetime import datetime, timedelta, date
from pathlib import Path

import minute_data
import money_flow
import scenario_ledger
import sentinel as sn
from collect_quotes import CST

DAY = '2026-09-21'
NOW = datetime(2026, 9, 21, 10, 0, 0, tzinfo=CST)


def bars(n=40, close=10.0):
    out, d = [], date(2026, 9, 18)
    while len(out) < n:
        if d.weekday() < 5:
            out.append({'date': d.isoformat(), 'close': str(close), 'open': str(close), 'high': str(close),
                        'low': str(close), 'volume_raw': '1000'})
        d -= timedelta(days=1)
    return list(reversed(out))


def quote(symbol='sz000001', last=9.4, prev=10.0, ratio=3.0, change=None, **kw):
    q = {'symbol': symbol, 'market': 'cn', 'name': '测试股', 'last': str(last), 'previous_close': str(prev),
         'open': '10.0', 'high': '10.1', 'low': str(min(last, 9.9)), 'change_pct': str(change if change is not None else round((last / prev - 1) * 100, 2)),
         'volume_ratio': str(ratio), 'turnover_pct': '1.2', 'quote_date': DAY, 'quote_at': NOW.isoformat(),
         'age_seconds': 4, 'is_index': False, 'limit_up': '11.0', 'limit_down': '9.0', 'batch_sha256': 'abc',
         'batch_fetched_at': NOW.isoformat()}
    q.update(kw)
    return q


def minute(prices=(10.0, 10.1, 9.9, 9.6, 9.4)):
    b = []
    for i, p in enumerate(prices):
        b.append({'t': '%02d%02d' % (9, 30 + i), 'price': p, 'vwap': 9.95, 'minute_volume_shares': 100,
                  'cum_volume_shares': 100 * (i + 1), 'cum_amount': 1.0})
    return {'trade_date': DAY, 'bars': b, 'vwap': 9.95, 'high_close': max(prices), 'low_close': min(prices),
            'complete': False, 'symbol': 'sz000001'}


def tick(quotes, minutes=None, due=None):
    return {'now': NOW, 'quotes': {q['symbol']: q for q in quotes},
            'minutes': minutes or (lambda s: minute()), 'node_due': due or (lambda h: None)}


HOLD = {'symbol': 'sz000001', 'name': '测试股', 'shares': 1000, 'cost_price': 10.0, 'stop_price': 9.5,
        'target_price': 12.0, 'hold_type': 'swing', 't_base_shares': 0}


class Harness:
    def __init__(self, holdings=None, watch=None, series=None, flow=None):
        self.dir = Path(tempfile.mkdtemp())
        self.sent = []
        self.holdings, self.watch = holdings if holdings is not None else [HOLD], watch or []
        self.series = series if series is not None else bars()
        self.flow, self.flow_calls = flow, []               # flow 为 None 或异常 = 资金流不可用

        def flow_fn(sym, now):
            self.flow_calls.append(sym)
            if self.flow is None or isinstance(self.flow, Exception):
                raise self.flow or money_flow.FlowError('资金流暂不可用')
            return self.flow
        self.s = sn.Sentinel(Path('.'), book_fn=lambda: (self.holdings, self.watch), series_fn=lambda sym: self.series,
                             send_fn=lambda text: self.sent.append(text) or {'sent': True, 'chunks': 1},
                             minute_fn=lambda sym, now: minute(), directory=self.dir,
                             flow_fn=flow_fn, flow_dir=self.dir / 'intraday')


def keys(signals, active=None):
    return {x['key'].split(':')[0] for x in signals if active is None or x['active'] == active}


class EvaluateTests(unittest.TestCase):
    def test_holding_produces_the_expected_active_signals(self):
        h = Harness()
        sigs = h.s.evaluate(tick([quote(last=9.4)]))
        self.assertLessEqual({'below-cost', 'stop', 'vol-surge-down'}, keys(sigs, active=True))
        self.assertEqual(len({x['key'] for x in sigs}), len(sigs))              # key 不重复

    def test_stocks_without_a_fresh_quote_produce_nothing(self):
        self.assertEqual(Harness().s.evaluate(tick([])), [])

    def test_index_quotes_feed_market_context_but_are_not_evaluated_as_stocks(self):
        h = Harness()
        idx = quote('sh000001', last=3900, prev=3800, is_index=True, name='上证指数')
        sigs = h.s.evaluate(tick([quote(), idx]))
        self.assertEqual(h.s.market, {'上证': round((3900 / 3800 - 1) * 100, 2)})
        self.assertFalse([x for x in sigs if 'sh000001' in x['key']])

    def test_held_and_watched_symbol_does_not_double_fire(self):
        """既持有又在自选：早先两边都会汇报 high20-break 等同名信号，同一件事推两次。"""
        w = {'symbol': 'sz000001', 'name': '测试股', 'intent': 'buy', 'buy_low': 9.0, 'buy_high': 9.5}
        h = Harness(watch=[w])
        sigs = h.s.evaluate(tick([quote(last=9.4)]))
        self.assertEqual(len({x['key'] for x in sigs}), len(sigs))
        self.assertIn('buy-zone', keys(sigs))                                    # 自选独有的买入区间保留

    def test_corporate_action_drops_ma_signals_but_keeps_price_level_ones(self):
        """昨收对不上缓存 = 多半除权：均线不可信，宁可不触发均线类；但成本价/止损位是你自己的价位，照常。"""
        h = Harness()
        sigs = h.s.evaluate(tick([quote(last=9.4, prev=15.0)]))
        self.assertNotIn('ma20-break', keys(sigs))
        self.assertIn('stop', keys(sigs, active=True))

    def test_missing_daily_cache_is_recorded_and_ma_signals_are_absent(self):
        h = Harness(series=[])
        sigs = h.s.evaluate(tick([quote()]))
        self.assertNotIn('ma20-break', keys(sigs))
        self.assertTrue(any('日线缓存' in i for i in h.s.cache['sz000001']['issues']))

    def test_t_prompts_need_minute_data_and_failure_degrades_gracefully(self):
        h = Harness(holdings=[{**HOLD, 't_base_shares': 500}])

        def boom(sym):
            raise minute_data.MinuteError('接口挂了')
        sigs = h.s.evaluate(tick([quote(last=9.4)], minutes=boom))
        self.assertNotIn('t-sell-high', keys(sigs))                              # 没有分时就不做T提示，但其余照常
        self.assertIn('stop', keys(sigs))
        self.assertTrue(any('做T提示暂停' in i for i in h.s.cache['sz000001']['issues']))

    def test_symbols_include_the_market_indices_for_free(self):
        self.assertEqual(Harness().s.symbols()[-3:], sn.INDEX_SYMBOLS)

    def test_node_signals_are_emitted_when_due(self):
        h = Harness()
        sigs = h.s.evaluate(tick([quote()], due=lambda hh: 30 if hh == '0945' else None))
        self.assertIn('node', ' '.join(x['key'] for x in sigs))


def event(symbol='sz000001', kind='sentinel.stop_hit', severity='urgent', detail='触及止损'):
    return {'key': '%s:%s' % (kind, symbol), 'symbol': symbol, 'kind': kind, 'severity': severity, 'detail': detail,
            'observed_at': NOW.isoformat(), 'price': '9.4', 'quote_at': NOW.isoformat(),
            'evidence': {'batch_sha256': 'abc', 'batch_fetched_at': NOW.isoformat()}}


class AfterTickTests(unittest.TestCase):
    def prime(self, h, **kw):
        h.s.evaluate(tick([quote(**kw)]))

    def test_alert_is_sent_immediately_and_stands_on_its_own(self):
        h = Harness(); self.prime(h)
        r = h.s.after_tick({'events': [event()], 'dry_run': False}, now=NOW)
        self.assertEqual(r['alerts'], 1)
        text = h.sent[0]
        for needle in ('【紧急】', '测试股', 'sz000001', '9.40', '触及止损', '成本 10.00', '止损 9.50', '不下单'):
            self.assertIn(needle, text)

    def test_only_sentinel_events_are_pushed(self):
        """候选池执行器的成交事件（paper-entry 等）不是自选股提醒，不能推到你的手机上。"""
        h = Harness(); self.prime(h)
        r = h.s.after_tick({'events': [event(kind='paper-entry')], 'dry_run': False}, now=NOW)
        self.assertEqual(r['alerts'], 0)
        self.assertEqual(h.sent, [])

    def test_dry_run_pushes_nothing(self):
        h = Harness(); self.prime(h)
        h.s.after_tick({'events': [event()], 'dry_run': True}, now=NOW)
        self.assertEqual(h.sent, [])

    def test_multiple_symbols_in_one_tick_share_one_message_urgent_first(self):
        h = Harness(holdings=[HOLD, {**HOLD, 'symbol': 'sz000002', 'name': '另一只'}])
        h.s.evaluate(tick([quote(), quote('sz000002', last=10.5, prev=10.0, name='另一只')]))
        h.s.after_tick({'events': [event('sz000002', 'sentinel.target_hit', 'normal', '触及目标'),
                                   event('sz000001')], 'dry_run': False}, now=NOW)
        self.assertEqual(len(h.sent), 1)
        self.assertLess(h.sent[0].index('测试股'), h.sent[0].index('另一只'))

    def test_push_failure_is_logged_never_raised(self):
        h = Harness(); self.prime(h)
        h.s.send_fn = lambda text: {'sent': False, 'reason': 'webhook 挂了'}
        r = h.s.after_tick({'events': [event()], 'dry_run': False}, now=NOW)
        self.assertFalse(r['push']['sent'])
        log = (h.dir.parent / 'sentinel').exists()
        alerts = list(sn.sdir(h.dir).glob('alerts-*.jsonl'))
        self.assertTrue(alerts)
        self.assertIn('webhook 挂了', alerts[0].read_text())

    def test_alert_is_logged_with_its_evidence(self):
        h = Harness(); self.prime(h)
        h.s.after_tick({'events': [event()], 'dry_run': False}, now=NOW)
        rec = json.loads(next(sn.sdir(h.dir).glob('alerts-*.jsonl')).read_text().splitlines()[0])
        self.assertEqual(rec['events'][0]['evidence']['batch_sha256'], 'abc')

    def test_alert_text_and_stored_block_carry_flow_and_order_book_facts(self):
        flow = {'as_of': '1000', 'main': -224507630.0, 'xlarge': -75691389.0, 'large': -148816241.0, 'mid': 1.0,
                'small': 2.0, 'main_5m': -1e6, 'main_30m': -4e7, 'peak_main': 1e6, 'peak_time': '0940',
                'from_peak': -2e8, 'flip': None}
        h = Harness(flow=flow)
        h.s.evaluate(tick([quote(outer_vol='213103', inner_vol='189631', bid_ask_ratio='5.18')]))
        h.s.after_tick({'events': [event()], 'dry_run': False}, now=NOW)
        self.assertIn('资金｜主力 -2.25亿（近30分钟 -4000万）｜大单 -1.49亿｜外盘占比 52.9%｜委比 +5.2%', h.sent[0])
        rec = json.loads(next(sn.sdir(h.dir).glob('alerts-*.jsonl')).read_text().splitlines()[0])
        block = rec['blocks'][0]
        self.assertEqual((block['symbol'], block['urgent'], block['price']), ('sz000001', True, '9.4'))
        self.assertEqual(block['events'], [{'kind': 'sentinel.stop_hit', 'severity': 'urgent', 'detail': '触及止损'}])
        self.assertEqual(block['flow']['main'], -224507630.0)
        self.assertNotIn('rows', block['flow'])
        self.assertEqual(block['book'], {'outer_pct': 52.9, 'bid_ask_ratio': 5.18})
        self.assertIn('触及止损', block['text'])                          # 单股原文，界面直接展示
        self.assertEqual(rec['text'].count('【紧急】'), 1)               # 完整推送原文仍在

    def test_alert_goes_out_without_the_flow_line_when_flow_is_unavailable(self):
        h = Harness(flow=None); self.prime(h)
        h.s.after_tick({'events': [event()], 'dry_run': False}, now=NOW)
        self.assertEqual(len(h.sent), 1)
        self.assertNotIn('资金｜', h.sent[0])
        rec = json.loads(next(sn.sdir(h.dir).glob('alerts-*.jsonl')).read_text().splitlines()[0])
        self.assertIsNone(rec['blocks'][0]['flow'])

    def test_recent_recorded_flow_is_used_without_a_network_call_and_a_stale_one_is_not(self):
        h = Harness(flow=None); self.prime(h)
        (h.dir / 'intraday').mkdir()
        row = {'symbol': 'sz000001', 'as_of': '0958', 'main': 5e7, 'xlarge': 3e7, 'large': 2e7, 'mid': 0.0,
               'small': 0.0, 'main_5m': 1e6, 'main_30m': 2e6, 'peak_main': 5e7, 'peak_time': '0958', 'from_peak': 0.0}
        recent = {**row, 'at': (NOW - timedelta(minutes=4)).isoformat()}
        (h.dir / 'intraday' / 'flow-2026-09-21.jsonl').write_text(json.dumps(recent) + '\n')
        h.s.after_tick({'events': [event()], 'dry_run': False}, now=NOW)
        self.assertEqual(h.flow_calls, [])                                # 直接用留存，没联网
        self.assertIn('主力 +5000万', h.sent[0])
        old = {**row, 'at': (NOW - timedelta(minutes=30)).isoformat()}
        (h.dir / 'intraday' / 'flow-2026-09-21.jsonl').write_text(json.dumps(old) + '\n')
        h.s.cache.clear(); self.prime(h)
        h.s.after_tick({'events': [event(kind='sentinel.target_hit', severity='normal')], 'dry_run': False}, now=NOW)
        self.assertEqual(h.flow_calls, ['sz000001'])                       # 留存太旧：联网取（这里取不到）
        self.assertNotIn('资金｜主力 +5000万', h.sent[1])

    def test_pending_item_is_saved_and_no_chart_is_generated(self):
        h = Harness(); self.prime(h)
        r = h.s.after_tick({'events': [event()], 'dry_run': False}, now=NOW)
        self.assertFalse((sn.sdir(h.dir) / 'charts').exists())          # 分时图已取消：不再生成、不再存盘
        pend = json.loads(next((sn.sdir(h.dir) / 'pending').glob('*.json')).read_text())
        self.assertNotIn('chart', pend)
        self.assertEqual(pend['symbol'], 'sz000001')
        self.assertTrue(pend['is_holding'])
        self.assertEqual(pend['limits'], {'limit_up': 11.0, 'limit_down': 9.0})
        self.assertIn('你的止损价', pend['entry']['key_levels'])
        self.assertEqual(pend['entry']['holding']['stop_price'], 9.5)
        self.assertEqual(r['alerts'], 1)

    def test_alert_still_goes_out_when_minute_data_is_unavailable(self):
        """最需要快的东西不能被慢的/不稳的东西拖住：分时取不到，就没有均价线，告警照发。"""
        h = Harness(); self.prime(h)

        def boom(sym, now):
            raise OSError('down')
        h.s.minute_fn = boom
        h.s.after_tick({'events': [event()], 'dry_run': False}, now=NOW)
        self.assertEqual(len(h.sent), 1)
        self.assertNotIn('均价线', h.sent[0])

    def test_minute_fetching_has_a_total_time_budget_so_the_alert_still_goes_out(self):
        """tick 服务的 systemd 时限只有 50 秒，而每次取分时最长 20 秒。预算用完就不再取，告警照发。"""
        h = Harness(holdings=[HOLD, {**HOLD, 'symbol': 'sz000002', 'name': '甲'}, {**HOLD, 'symbol': 'sz000003', 'name': '乙'}])
        h.s.evaluate(tick([quote(), quote('sz000002', name='甲'), quote('sz000003', name='乙')]))
        fetched = []

        def slow(sym, now):
            fetched.append(sym)
            import time as _t
            sn.time.monotonic  # noqa
            return minute()
        h.s.minute_fn = slow
        orig, calls = sn.time.monotonic, {'n': 0}

        def fake_clock():                              # 第一次取分时之后，时钟一下子跳过预算
            calls['n'] += 1
            return 0 if calls['n'] <= 2 else 1000
        sn.time.monotonic = fake_clock
        try:
            h.s.after_tick({'events': [event('sz000001'), event('sz000002'), event('sz000003')], 'dry_run': False}, now=NOW)
        finally:
            sn.time.monotonic = orig
        self.assertLess(len(fetched), 3)               # 没有把 3 只都取一遍
        self.assertEqual(len(h.sent), 1)               # 但告警发出去了
        for name in ('测试股', '甲', '乙'):
            self.assertIn(name, h.sent[0])

    def test_time_nodes_alone_are_not_pushed_but_still_queue_a_scenario_analysis(self):
        """"09:45 开盘方向确立"本身没有任何可操作信息，5只×3个节点每天就是15条噪音。
        它的价值是触发情景研判——只入队，等带价位的情景出来再推那一条有内容的。"""
        h = Harness(); self.prime(h)
        r = h.s.after_tick({'events': [event(kind='sentinel.node_0945', severity='normal', detail='09:45 开盘方向确立')],
                            'dry_run': False}, now=NOW)
        self.assertEqual(r['alerts'], 0)
        self.assertEqual(h.sent, [])
        self.assertEqual(len(r['pending']), 1)                     # 但研判队列里有它
        self.assertEqual(len(list((sn.sdir(h.dir) / 'pending').glob('*.json'))), 1)

    def test_a_node_that_coincides_with_a_real_event_rides_along_as_context(self):
        h = Harness(); self.prime(h)
        h.s.after_tick({'events': [event(), event(kind='sentinel.node_0945', severity='normal', detail='09:45 开盘方向确立')],
                        'dry_run': False}, now=NOW)
        self.assertEqual(len(h.sent), 1)
        self.assertIn('触及止损', h.sent[0])
        self.assertIn('开盘方向确立', h.sent[0])

    def test_no_events_means_nothing(self):
        h = Harness()
        self.assertEqual(h.s.after_tick({'events': [], 'dry_run': False}, now=NOW), {'alerts': 0})


class FormatTests(unittest.TestCase):
    ENTRY = {'symbol': 'sz000001', 'current_read': '现价9.40跌破成本，位于均价线下方0.6%', 'watch_metrics': ['量能'],
             'action_hint': 'wait', 'confidence': 3, 'caveats': [],
             'scenarios': [{'label': '反抽', 'direction': 'up', 'trigger_price': 9.7, 'trigger_condition': '站上均价线',
                            'target_low': 9.9, 'target_high': 10.0, 'invalidate_price': 9.3},
                           {'label': '破位', 'direction': 'down', 'trigger_price': 9.2, 'trigger_condition': '放量跌破',
                            'target_low': 8.9, 'target_high': 9.0, 'invalidate_price': 9.5}]}

    def test_scenarios_carry_all_numbers_and_the_honest_caveats(self):
        t = sn.format_scenarios(self.ENTRY, '测试股', 9.4)
        for needle in ('9.70', '9.90–10.00', '9.30', '↑', '↓', '不是胜率', '收盘后自动对账', '不构成投资建议'):
            self.assertIn(needle, t)

    def test_no_percentage_win_rate_is_ever_printed(self):
        self.assertNotRegex(sn.format_scenarios(self.ENTRY, '测试股', 9.4), r'胜率\s*\d|概率\s*\d|\d+\s*%的')


# --- 研判进程 ----------------------------------------------------------------

def pending_item(sym='sz000001', created=NOW, holding=True, t_base=0, last='9.4'):
    return {'id': 'p-' + sym, 'created_at': created.isoformat(), 'symbol': sym, 'chart': None, 'market': {'上证': -0.5},
            'node': 'sentinel.stop_hit', 'is_holding': holding, 't_base': t_base,
            'limits': {'limit_up': 11.0, 'limit_down': 9.0},
            'entry': {'symbol': sym, 'name': '测试股', 'roles': ['holding'], 'triggers': [{'kind': 'sentinel.stop_hit', 'detail': 'd'}],
                      'quote': {'last': last}, 'price_facts': {}, 'day': None, 'limit_facts': {}, 'holding': None,
                      'watch': None, 'key_levels': {}, 'recent_minutes': [], 'issues': []}}


def good_entry(sym='sz000001'):
    return {'symbol': sym, 'current_read': '现状', 'watch_metrics': [], 'action_hint': 'wait', 'confidence': 3, 'caveats': [],
            'scenarios': [{'label': '反抽', 'direction': 'up', 'trigger_price': 9.7, 'trigger_condition': 'c',
                           'target_low': 9.9, 'target_high': 10.0, 'invalidate_price': 9.3}]}


class AnalyzeHarness:
    def __init__(self, items=None):
        self.dir = Path(tempfile.mkdtemp())
        self.sent, self.calls = [], []
        (sn.sdir(self.dir) / 'pending').mkdir(parents=True)
        for it in (items if items is not None else [pending_item()]):
            (sn.sdir(self.dir) / 'pending' / (it['id'] + '.json')).write_text(json.dumps(it))

    def run(self, data=None, fresh_last='9.4', ok=(True, ''), max_calls=15, snap=None, **kw):
        def analyze(payload):
            self.calls.append(payload)
            return ({'stocks': [good_entry()], 'data_caveats': []} if data is None else data), {'status': 'ok', 'model': 'm', 'input_tokens': 1, 'output_tokens': 1, 'prompt_version': 'v'}
        return sn.analyze_pending(self.dir, now=kw.pop('now', NOW + timedelta(seconds=60)),
                                  snapshot_fn=snap or (lambda syms: {'quotes': [quote(last=float(fresh_last))], 'failures': []}),
                                  analyze_fn=kw.pop('analyze_fn', analyze), send_fn=lambda t: self.sent.append(t) or {'sent': True},
                                  ai_ok_fn=lambda: ok, max_calls=max_calls)

    def status(self):
        return {json.loads(p.read_text())['id']: json.loads(p.read_text())['status'] for p in (sn.sdir(self.dir) / 'done').glob('*.json')}


class AnalyzePendingTests(unittest.TestCase):
    def test_valid_scenario_is_pushed_recorded_and_the_item_is_done(self):
        h = AnalyzeHarness()
        r = h.run()
        self.assertEqual((r['processed'], r['pushed']), (1, 1))
        self.assertIn('情景研判', h.sent[0])
        rows = scenario_ledger.load_scenarios(h.dir, DAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['price_at_issue'], 9.4)
        self.assertEqual(rows[0]['node'], 'sentinel.stop_hit')
        self.assertEqual(h.status(), {'p-sz000001': 'done'})
        self.assertFalse(list((sn.sdir(h.dir) / 'pending').glob('*.json')))

    def test_no_pending_items_costs_nothing(self):
        h = AnalyzeHarness(items=[])
        called = []
        r = sn.analyze_pending(h.dir, now=NOW, snapshot_fn=lambda s: called.append(1), analyze_fn=lambda p: called.append(2))
        self.assertEqual(r, {'processed': 0})
        self.assertEqual(called, [])                                           # 不取报价、不调 AI

    def test_no_ai_configured_degrades_and_does_not_retry_forever(self):
        h = AnalyzeHarness()
        r = h.run(ok=(False, '未配置 ANTHROPIC_API_KEY'))
        self.assertIn('no_ai', r)
        self.assertEqual(h.status(), {'p-sz000001': 'no_ai'})                   # 标记完成，不会每分钟重试
        self.assertEqual(h.calls, [])

    def test_daily_budget_stops_ai_calls_but_alerts_already_went_out(self):
        h = AnalyzeHarness()
        sn.bump_ai(h.dir, DAY, {}); sn.bump_ai(h.dir, DAY, {})
        r = h.run(max_calls=2)
        self.assertTrue(r['budget'])
        self.assertEqual(h.calls, [])
        self.assertEqual(h.status(), {'p-sz000001': 'budget'})

    def test_one_analysis_run_sends_one_merged_message_not_one_per_stock(self):
        """回放里 13 次研判推了 29 条消息——一天几十条，违背"只推可操作事件"。"""
        h = AnalyzeHarness([pending_item('sz000001'), pending_item('sz000002')])
        h.run(data={'stocks': [good_entry('sz000001'), good_entry('sz000002')], 'data_caveats': []},
              snap=lambda syms: {'quotes': [quote('sz000001', last=9.4), quote('sz000002', last=9.4)], 'failures': []})
        self.assertEqual(len(h.sent), 1)
        self.assertEqual(h.sent[0].count('【情景研判】'), 2)

    def test_scenario_issue_time_follows_the_passed_clock_not_the_wall_clock(self):
        """早先用 datetime.now()：回放/测试里情景的发布时间和它所属的交易日时间轴脱钩，收盘对账时
        "发布之后没有分钟数据"，53 条全部对不了账。"""
        h = AnalyzeHarness()
        h.run(now=NOW + timedelta(seconds=25))
        issued = datetime.fromisoformat(scenario_ledger.load_scenarios(h.dir, DAY)[0]['issued_at'])
        self.assertEqual(issued.date().isoformat(), DAY)
        self.assertGreaterEqual(issued, NOW + timedelta(seconds=25))
        self.assertLess(issued, NOW + timedelta(seconds=40))

    def test_scenarios_that_were_never_delivered_are_marked_and_kept_out_of_the_stats(self):
        """推送失败时你根本没看到这条情景，记进命中率等于统计了一批你无法据此行动的东西。"""
        h = AnalyzeHarness()
        sn.analyze_pending(h.dir, now=NOW + timedelta(seconds=60),
                           snapshot_fn=lambda s: {'quotes': [quote(last=9.4)], 'failures': []},
                           analyze_fn=lambda p: ({'stocks': [good_entry()], 'data_caveats': []}, {'status': 'ok'}),
                           send_fn=lambda t: {'sent': False, 'reason': 'webhook 挂了'}, ai_ok_fn=lambda: (True, ''))
        rows = scenario_ledger.load_scenarios(h.dir, DAY)
        self.assertEqual([r['delivered'] for r in rows], [False])
        joined = scenario_ledger.load_joined(h.dir, [DAY])
        for r in joined:
            r['outcome'] = 'triggered_and_hit'
        s = scenario_ledger.summarize(joined)
        self.assertEqual(s['overall']['n'], 0)
        self.assertEqual(s['undelivered'], 1)

    def test_every_call_is_counted(self):
        h = AnalyzeHarness()
        h.run()
        self.assertEqual(sn.ai_count(h.dir, DAY), 1)

    def test_stale_items_are_expired_not_analysed(self):
        h = AnalyzeHarness()
        r = h.run(now=NOW + timedelta(seconds=sn.PENDING_MAX_AGE_S + 1))
        self.assertEqual(h.calls, [])
        self.assertEqual(h.status(), {'p-sz000001': 'expired'})

    def test_same_symbol_events_are_merged_into_one_analysis(self):
        a, b = pending_item(), pending_item()
        b['id'] = 'p2-sz000001'
        b['entry']['triggers'] = [{'kind': 'sentinel.below_cost', 'detail': 'x'}]
        h = AnalyzeHarness([a, b])
        h.run()
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(len(h.calls[0]['stocks']), 1)
        self.assertEqual(len(h.calls[0]['stocks'][0]['triggers']), 2)
        self.assertEqual(set(h.status().values()), {'done'})

    def test_price_is_refreshed_before_the_call_because_ai_takes_a_minute(self):
        h = AnalyzeHarness()
        h.run(fresh_last='9.55')
        self.assertEqual(h.calls[0]['stocks'][0]['quote']['last'], '9.55')

    def test_a_trigger_the_price_has_already_crossed_while_waiting_is_dropped(self):
        """AI 要 30–90 秒，价格可能已经越过它给的触发价。用新鲜报价重新校验，越过的情景丢弃。"""
        h = AnalyzeHarness()
        r = h.run(fresh_last='9.8')                                            # 触发价 9.7 已被越过
        self.assertEqual(r['pushed'], 0)
        self.assertEqual(h.sent, [])
        self.assertEqual(scenario_ledger.load_scenarios(h.dir, DAY), [])       # 没推的不入账，收盘不会拿它对账
        self.assertTrue(any('没有高于现价' in x for x in r['dropped']))

    def test_no_fresh_quote_means_no_analysis(self):
        h = AnalyzeHarness()
        r = h.run(snap=lambda s: {'quotes': [quote(age_seconds=999)], 'failures': []})
        self.assertEqual(h.calls, [])
        self.assertEqual(h.status(), {'p-sz000001': 'stale_quote'})

    def test_ai_failure_marks_the_item_and_pushes_nothing(self):
        h = AnalyzeHarness()
        r = h.run(analyze_fn=lambda p: (None, {'status': 'rate_limited', 'error': '慢点'}))
        self.assertEqual(r['ai_failed'], 'rate_limited')
        self.assertEqual(h.sent, [])
        self.assertEqual(h.status(), {'p-sz000001': 'ai_failed'})
        self.assertEqual(sn.ai_count(h.dir, DAY), 1)                           # 失败的调用也算次数，防止重试风暴

    def test_symbols_the_model_forgot_are_reported(self):
        h = AnalyzeHarness()
        r = h.run(data={'stocks': [], 'data_caveats': []})
        self.assertTrue(any('未返回' in x for x in r['dropped']))

    def test_t_hint_without_a_declared_base_is_downgraded_before_pushing(self):
        h = AnalyzeHarness()
        e = good_entry(); e['action_hint'] = 't_sell_high'
        h.run(data={'stocks': [e], 'data_caveats': []})
        self.assertNotIn('做T', h.sent[0])

    def test_overlapping_analyst_run_is_skipped(self):
        h = AnalyzeHarness()
        lock = open(sn.sdir(h.dir) / '.analyst.lock', 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertIn('skipped', h.run())
            self.assertEqual(h.calls, [])
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN); lock.close()

    def test_unexpected_exception_is_contained(self):
        h = AnalyzeHarness()
        r = h.run(snap=lambda s: (_ for _ in ()).throw(RuntimeError('boom')))
        self.assertIn('boom', r['error'])


if __name__ == '__main__':
    unittest.main()
