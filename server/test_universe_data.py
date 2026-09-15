import unittest
from datetime import datetime
from collect_quotes import CST
from universe_data import validate_members, parse_batch


class UniverseTests(unittest.TestCase):
    def test_scope_and_duplicates(self):
        rows = [{'symbol': s, 'name': s} for s in ['sh600000', 'sz300750', 'bj920001']]
        members, excluded = validate_members(rows, 3)
        self.assertEqual(len(members), 2)
        self.assertEqual(excluded, ['bj920001'])
        with self.assertRaises(ValueError):
            validate_members(rows + [rows[0]], 4)
        with self.assertRaises(ValueError):
            validate_members(rows, 4)

    def test_one_bad_quote_does_not_drop_batch(self):
        fields = [''] * 31
        fields[1:6] = ['测试', '600000', '10', '10', '10']
        fields[30] = '20260915150000'
        raw = ('v_sh600000="' + '~'.join(fields) + '";').encode('gb18030')
        quotes, failures = parse_batch(raw, ['sh600000', 'sz000001'], datetime(2026, 9, 15, 16, tzinfo=CST))
        self.assertEqual([q['symbol'] for q in quotes], ['sh600000'])
        self.assertEqual([f['symbol'] for f in failures], ['sz000001'])
