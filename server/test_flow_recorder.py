import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import flow_recorder as fr
import money_flow
from collect_quotes import CST

NOW = datetime(2026, 9, 21, 10, 5, 15, tzinfo=CST)      # 周一 上午盘中


def flow(main=-100.0):
    return {'as_of': '1005', 'main': main, 'xlarge': -40.0, 'large': -60.0, 'mid': 10.0, 'small': 90.0,
            'main_5m': -5.0, 'main_30m': -20.0, 'peak_main': 30.0, 'peak_time': '0940', 'from_peak': -130.0,
            'flip': {'at': '0950', 'from': '净流入', 'to': '净流出'}, 'rows': [1, 2, 3]}


class RunTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def run_once(self, symbols, fetch, now=NOW, calendar='open', **kw):
        return fr.run(Path('.'), now=now, symbols=symbols, directory=self.dir, fetch=fetch,
                      calendar_fn=lambda d: calendar, **kw)

    def rows(self):
        return [json.loads(l) for l in (self.dir / 'flow-2026-09-21.jsonl').read_text().splitlines()]

    def test_writes_one_compact_row_per_stock_and_records_failures_as_error_rows(self):
        def fetch(symbol, now):
            if symbol == 'sz000002':
                raise money_flow.FlowError('资金流请求失败（URLError）')
            return flow()
        r = self.run_once(['sz000001', 'sz000002'], fetch)
        self.assertEqual((r['ran'], r['ok'], r['failed']), (True, 1, ['sz000002']))
        rows = self.rows()
        self.assertEqual([x['symbol'] for x in rows], ['sz000001', 'sz000002'])
        self.assertEqual(rows[0]['main'], -100.0)
        self.assertNotIn('rows', rows[0])                    # 不存整天序列
        self.assertIn('URLError', rows[1]['error'])
        self.assertNotIn('main', rows[1])

    def test_indexes_and_foreign_symbols_are_never_fetched(self):
        seen = []
        self.run_once(['sh000001', 'sz399006', 'hkHSI', 'sz000001', 'sz000001'], lambda s, n: seen.append(s) or flow())
        self.assertEqual(seen, ['sz000001'])

    def test_outside_the_session_or_without_open_calendar_nothing_runs(self):
        called = []
        for when, cal in ((datetime(2026, 9, 21, 12, 0, tzinfo=CST), 'open'), (NOW, 'closed'), (NOW, 'unknown')):
            r = self.run_once(['sz000001'], lambda s, n: called.append(s), now=when, calendar=cal)
            self.assertFalse(r['ran'])
        self.assertEqual(called, [])
        self.assertFalse(list(self.dir.glob('*')))

    def test_empty_book_is_a_skip_not_an_error(self):
        self.assertIn('没有需要采集', self.run_once([], lambda s, n: flow())['skipped'])

    def test_a_crash_or_a_hang_in_one_stock_does_not_lose_the_others(self):
        def fetch(symbol, now):
            if symbol == 'sz000002':
                raise RuntimeError('boom')
            if symbol == 'sz000003':
                time.sleep(1.5)
            return flow()
        r = self.run_once(['sz000001', 'sz000002', 'sz000003'], fetch, budget_s=0.3)
        self.assertEqual(r['ok'], 1)
        errors = {x['symbol']: x['error'] for x in self.rows() if 'error' in x}
        self.assertIn('RuntimeError', errors['sz000002'])
        self.assertIn('时间预算', errors['sz000003'])

    def test_unreadable_book_is_reported_not_raised(self):
        def bad():
            raise ValueError('corrupt')
        self.assertIn('读取持仓/自选失败', self.run_once(bad, lambda s, n: flow())['error'])


class ReadBackTests(unittest.TestCase):
    def test_series_and_summary_separate_good_rows_from_errors(self):
        d = Path(tempfile.mkdtemp())
        lines = [{'at': 'a1', 'symbol': 'sz000001', 'main': 1.0}, {'at': 'a1', 'symbol': 'sz000002', 'error': 'x'},
                 {'at': 'a2', 'symbol': 'sz000001', 'main': 2.0}, {'at': 'a2', 'symbol': 'sz000002', 'error': 'y'}]
        (d / 'flow-2026-09-21.jsonl').write_text('\n'.join(json.dumps(l) for l in lines) + '\n{half')
        self.assertEqual([r['main'] for r in fr.series(d, '2026-09-21', 'sz000001')], [1.0, 2.0])
        self.assertEqual(fr.series(d, '2026-09-21', 'sz000002'), [])
        s = fr.collection_summary(d, '2026-09-21')
        self.assertEqual(s['runs'], 2)
        self.assertEqual(s['symbols']['sz000001'], {'ok': 2, 'error': 0, 'last_error': None})
        self.assertEqual(s['symbols']['sz000002'], {'ok': 0, 'error': 2, 'last_error': 'y'})
        self.assertEqual(fr.collection_summary(d, '2026-09-18'), {'runs': 0, 'symbols': {}})


if __name__ == '__main__':
    unittest.main()
