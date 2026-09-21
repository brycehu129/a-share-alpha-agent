import unittest

import stock_search as ss

RAW = 'v_hint="sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A^sh~600903~\\u8d35\\u5dde\\u71c3\\u6c14~gzrq~GP-A^hk~06199~\\u8d35\\u5dde\\u94f6\\u884c~gzyh~GP^sh~510300~\\u6caa\\u6df1300ETF~hs300etf~ETF-A^sh~000001~\\u4e0a\\u8bc1\\u6307\\u6570~szzs~ZS^sz~300458~\\u5168\\u5fd7\\u79d1\\u6280~qzkj~GP-A^sh~688981~\\u4e2d\\u82af\\u56fd\\u9645~zxgj~GP-A-KCB"'


class ParseTests(unittest.TestCase):
    def test_keeps_only_shenzhen_and_shanghai_a_shares_and_decodes_the_names(self):
        out = ss.parse(RAW)
        self.assertEqual([h['symbol'] for h in out], ['sh600519', 'sh600903', 'sz300458', 'sh688981'])   # 港股/ETF/指数都不要
        self.assertEqual(out[0], {'symbol': 'sh600519', 'code': '600519', 'name': '贵州茅台', 'pinyin': 'GZMT', 'board': ''})
        self.assertEqual(out[3]['board'], '科创板')

    def test_no_result_is_an_empty_list_and_garbage_is_an_error(self):
        self.assertEqual(ss.parse('v_hint="N";'), [])
        with self.assertRaises(ss.SearchError):
            ss.parse('<html>blocked</html>')

    def test_malformed_items_are_skipped_not_fatal(self):
        self.assertEqual(ss.parse('v_hint="sh~600519~x^broken^sz~00000~short~s~GP-A"'), [])


class SearchTests(unittest.TestCase):
    def fetch(self, seen):
        def fn(url):
            seen.append(url)
            return RAW.encode()
        return fn

    def test_query_is_url_encoded_and_results_are_limited(self):
        seen = []
        out = ss.search('贵州', limit=2, fetch=self.fetch(seen))
        self.assertEqual(len(out), 2)
        self.assertTrue(seen[0].endswith('q=%E8%B4%B5%E5%B7%9E'))

    def test_blank_and_overlong_queries_never_hit_the_network(self):
        seen = []
        self.assertEqual(ss.search('  ', fetch=self.fetch(seen)), [])
        with self.assertRaises(ss.SearchError):
            ss.search('x' * 21, fetch=self.fetch(seen))
        self.assertEqual(seen, [])

    def test_network_failures_are_a_search_error_not_an_empty_result(self):
        def boom(url):
            raise OSError('down')
        with self.assertRaises(ss.SearchError):
            ss.search('600519', fetch=boom)

    def test_resolve_needs_an_exact_symbol_match(self):
        fetch = self.fetch([])
        self.assertEqual(ss.resolve('sh600519', fetch=fetch)['name'], '贵州茅台')
        self.assertIsNone(ss.resolve('sz000009', fetch=fetch))


if __name__ == '__main__':
    unittest.main()
