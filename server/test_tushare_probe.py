import unittest
from tushare_probe import classify


class ProbeTests(unittest.TestCase):
    def test_permission_and_redaction(self):
        r = classify({'code': 2002, 'msg': '没有权限 example-secret'}, 'example-secret')
        self.assertEqual(r['status'], 'permission_denied')
        self.assertNotIn('example-secret', r['message'])

    def test_empty_is_not_permission_denied(self):
        r = classify({'code': 0, 'data': {'fields': ['code'], 'items': []}}, 'example-secret')
        self.assertEqual(r['status'], 'empty')
        with self.assertRaises(ValueError):
            classify({'code': 0, 'data': {'fields': ['code'], 'items': [[]]}}, 'example-secret')
