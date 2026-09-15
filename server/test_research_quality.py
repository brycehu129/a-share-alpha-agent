import unittest
from research_quality import exclude_reason, parse_industry_rows


class QualityTests(unittest.TestCase):
    def test_name_and_quote_exclusions(self):
        q = dict(name='普通股', open='10', last='10', previous_close='10', quote_at='2026-09-15T15:00:00+08:00')
        self.assertIsNone(exclude_reason({'name': '普通股'}, q, '2026-09-15'))
        self.assertIsNotNone(exclude_reason({'name': '*ＳＴ普通'}, {**q, 'name': '*ST普通'}, '2026-09-15'))
        self.assertIsNotNone(exclude_reason({'name': '普通退'}, q, '2026-09-15'))
        self.assertIsNotNone(exclude_reason({'name': '普通股'}, {**q, 'open': '0'}, '2026-09-15'))
        self.assertIsNotNone(exclude_reason({'name': '普通股'}, q, '2026-09-14'))
        self.assertIsNotNone(exclude_reason({'name': '普通股'}, None, '2026-09-15'))

    def test_industry_completeness(self):
        rows = [{'f12': '600000', 'f13': 1, 'f100': '银行'}]
        self.assertEqual(parse_industry_rows(rows, 1)[0]['symbols'], ['sh600000'])
        with self.assertRaises(ValueError):
            parse_industry_rows(rows + rows, 2)
        with self.assertRaises(ValueError):
            parse_industry_rows(rows, 2)
        with self.assertRaises(ValueError):
            parse_industry_rows([{**rows[0], 'f100': '-'}], 1)
