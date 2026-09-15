import unittest
from datetime import date, timedelta
from research_pipeline import factors, industries


class ResearchTests(unittest.TestCase):
    def test_returns_and_alignment(self):
        rows = [{'date': (date(2026, 1, 1) + timedelta(days=i)).isoformat(), 'close': str(100+i)} for i in range(21)]
        benchmark = [{**r, 'close': '100'} for r in rows]
        result = factors(rows, benchmark, rows[-1]['date'])
        self.assertEqual(result['excess20_pp'], '20.00')
        later = rows + [{'date': '2026-01-22', 'close': '999'}]
        self.assertEqual(factors(later, benchmark, rows[-1]['date']), result)
        with self.assertRaises(ValueError):
            factors(rows[:-1], benchmark, rows[-1]['date'])
        broken = [dict(r) for r in rows]
        broken[5]['date'] = '2025-12-31'
        with self.assertRaises(ValueError):
            factors(broken, benchmark, rows[-1]['date'])

    def test_classification_no_js_execution(self):
        raw = 'var example={"new_bank":"new_bank,银行,3,0"};'.encode('gb18030')
        self.assertEqual(industries(raw)[0]['expected'], 3)
        with self.assertRaises(ValueError):
            industries(b'var x={"evil":"x,name,1"};')
