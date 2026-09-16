import unittest
from tushare_probe import classify
from tushare_probe import selected_source, ENDPOINT, XIAODEFA_ENDPOINT, probe
from unittest.mock import patch


class ProbeTests(unittest.TestCase):
    def test_credentials_stay_with_their_source(self):
        with patch.dict('os.environ', {'TUSHARE_TOKEN':'official', 'XIAODEFA_TOKEN':'third-party'}, clear=True):
            self.assertEqual(selected_source(), (XIAODEFA_ENDPOINT, 'third-party'))
        with patch.dict('os.environ', {'TUSHARE_TOKEN':'official'}, clear=True):
            self.assertEqual(selected_source(), (ENDPOINT, 'official'))
        with self.assertRaises(ValueError):
            probe('daily', {}, '', 'secret', 'https://unconfigured.example/')

    def test_permission_and_redaction(self):
        r = classify({'code': 2002, 'msg': '没有权限 example-secret'}, 'example-secret')
        self.assertEqual(r['status'], 'permission_denied')
        self.assertNotIn('example-secret', r['message'])

    def test_empty_is_not_permission_denied(self):
        r = classify({'code': 0, 'data': {'fields': ['code'], 'items': []}}, 'example-secret')
        self.assertEqual(r['status'], 'empty')
        with self.assertRaises(ValueError):
            classify({'code': 0, 'data': {'fields': ['code'], 'items': [[]]}}, 'example-secret')
