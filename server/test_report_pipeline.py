import copy
import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline import SYMBOLS, build_report, markdown, persist_report, read_history
from collect_quotes import open_database


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = open_database(self.root / 'quotes.sqlite3')
        self.db.execute('INSERT INTO collection_runs VALUES(?,?,?,?,?,?)',
                        ('synthetic', '2026-09-15T20:00:00+08:00', 'https://qt.gtimg.cn/q=test', 'success', None, 'fixture'))
        self.db.executemany('INSERT INTO quote_snapshots VALUES(?,?,?,?,?,?,?,?)', [
            ('synthetic', s, '测试指数', '101', '100', '100', '2026-09-15T15:00:00+08:00', '[]') for s in SYMBOLS
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_restore_append_and_no_overwrite(self):
        first = build_report(self.db, '100-1', [])
        persist_report(self.root, first)
        saved = (self.root / 'records/100-1.json').read_bytes()
        restored = read_history(self.root)
        second = build_report(self.db, '101-1', restored)
        persist_report(self.root, second)
        self.assertEqual(second['previous_count'], 1)
        self.assertEqual(len(read_history(self.root)), 2)
        self.assertEqual(saved, (self.root / 'records/100-1.json').read_bytes())
        with self.assertRaises(ValueError):
            persist_report(self.root, first)

    def test_corrupt_history_fails_closed(self):
        persist_report(self.root, build_report(self.db, '100-1', []))
        p = self.root / 'records/100-1.json'
        data = json.loads(p.read_text())
        data['payload']['status'] = 'changed'
        p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            read_history(self.root)

    def test_stale_and_mixed_dates_are_explicit(self):
        self.db.execute("UPDATE quote_snapshots SET quote_at='2026-09-14T15:00:00+08:00' WHERE symbol=?", (SYMBOLS[0],))
        r = build_report(self.db, '100-1', [])
        self.assertIn('暂停横向比较', r['summary'])
        self.assertTrue(any('15分钟' in w for w in r['warnings']))
        self.assertTrue(any('并非采集当天' in w for w in r['warnings']))

    def test_failed_capture_is_not_replaced_by_old_quotes(self):
        self.db.execute('INSERT INTO collection_runs VALUES(?,?,?,?,?,?)',
                        ('failed', '2026-09-15T20:01:00+08:00', 'https://qt.gtimg.cn/q=test', 'failed', 'timeout', None))
        r = build_report(self.db, '100-1', [])
        self.assertEqual(r['quotes'], [])
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(r['predictions'], [])
        self.assertFalse(r['model_called'])

    def test_report_escapes_source_names(self):
        r = build_report(self.db, '100-1', [])
        r['quotes'][0]['name'] = '<script>|bad\nname'
        result = markdown(r)
        self.assertNotIn('<script>', result)
        self.assertIn('&#124;', result)


if __name__ == '__main__':
    unittest.main()
