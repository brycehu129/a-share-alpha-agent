import tempfile
import unittest
from pathlib import Path
from session_brief import calendar_state
from tushare_sync import save


class CalendarTests(unittest.TestCase):
    def test_closed_missing_and_disagreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp)
            self.assertEqual(calendar_state(history, '20261001'), 'unknown')
            for e in ('SSE', 'SZSE'):
                save(history / 'tushare_data/calendars' / (e+'.json'), {'rows':[{'cal_date':'20261001','is_open':0}]})
            self.assertEqual(calendar_state(history, '20261001'), 'closed')
            save(history / 'tushare_data/calendars/SZSE.json', {'rows':[{'cal_date':'20261001','is_open':1}]})
            self.assertEqual(calendar_state(history, '20261001'), 'unknown')
