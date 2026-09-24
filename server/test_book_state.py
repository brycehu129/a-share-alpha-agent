import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import book_state as bs
from collect_quotes import CST

NOW = datetime(2026, 9, 21, 10, 30, tzinfo=CST)
LATER = datetime(2026, 9, 21, 11, 0, tzinfo=CST)


def bars(rows):
    return [{'date': d, 'high': h} for d, h in rows]


class TouchPeakTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_first_call_seeds_from_local_history_since_opened_on(self):
        b = bars([('2026-09-18', '11.0'), ('2026-09-19', '12.5'), ('2026-09-20', '10.0')])
        peak = bs.touch_peak('sh600000', 11.0, NOW, bars=b, opened_on='2026-09-18', directory=self.dir)
        self.assertEqual(peak, 12.5)          # 历史高点 12.5，不是今天观测到的 11.0

    def test_history_before_opened_on_is_ignored(self):
        b = bars([('2026-09-10', '99.0'), ('2026-09-19', '11.0')])
        peak = bs.touch_peak('sh600000', 11.0, NOW, bars=b, opened_on='2026-09-18', directory=self.dir)
        self.assertEqual(peak, 11.0)          # 9-10 的天价是开仓之前的，不算

    def test_no_history_available_just_uses_the_first_observation(self):
        peak = bs.touch_peak('sh600000', 10.0, NOW, directory=self.dir)
        self.assertEqual(peak, 10.0)

    def test_peak_only_goes_up_never_down(self):
        bs.touch_peak('sh600000', 12.0, NOW, directory=self.dir)
        peak = bs.touch_peak('sh600000', 11.0, LATER, directory=self.dir)
        self.assertEqual(peak, 12.0)
        peak = bs.touch_peak('sh600000', 13.0, LATER, directory=self.dir)
        self.assertEqual(peak, 13.0)

    def test_symbols_are_independent(self):
        bs.touch_peak('sh600000', 12.0, NOW, directory=self.dir)
        bs.touch_peak('sz000001', 5.0, NOW, directory=self.dir)
        self.assertEqual(bs.get('sh600000', self.dir)['peak_price'], 12.0)
        self.assertEqual(bs.get('sz000001', self.dir)['peak_price'], 5.0)

    def test_none_price_reads_without_writing(self):
        bs.touch_peak('sh600000', 12.0, NOW, directory=self.dir)
        peak = bs.touch_peak('sh600000', None, LATER, directory=self.dir)
        self.assertEqual(peak, 12.0)

    def test_records_the_time_of_the_new_peak(self):
        bs.touch_peak('sh600000', 12.0, NOW, directory=self.dir)
        self.assertEqual(bs.get('sh600000', self.dir)['peak_at'], NOW.isoformat())


class RecordAddTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_counts_accumulate(self):
        bs.record_add('sh600000', NOW, self.dir)
        bs.record_add('sh600000', LATER, self.dir)
        row = bs.get('sh600000', self.dir)
        self.assertEqual(row['add_count'], 2)
        self.assertEqual(row['last_add_at'], LATER.isoformat())

    def test_does_not_disturb_the_peak(self):
        bs.touch_peak('sh600000', 12.0, NOW, directory=self.dir)
        bs.record_add('sh600000', LATER, self.dir)
        self.assertEqual(bs.get('sh600000', self.dir)['peak_price'], 12.0)


class ClearTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_clears_peak_and_add_count_for_a_fresh_position(self):
        bs.touch_peak('sh600000', 12.0, NOW, directory=self.dir)
        bs.record_add('sh600000', NOW, self.dir)
        bs.clear('sh600000', self.dir)
        self.assertEqual(bs.get('sh600000', self.dir), {})

    def test_clearing_an_unknown_symbol_is_a_no_op(self):
        bs.clear('sh999999', self.dir)          # 不抛异常


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(bs.load(self.dir), {})

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        import os
        bs.touch_peak('sh600000', 12.0, NOW, directory=self.dir)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith('.tmp')], [])


if __name__ == '__main__':
    unittest.main()
