import json
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import announcements as an

CST = an.CST
SINCE = datetime(2026, 9, 18, 15, 0, tzinfo=CST)
NOW = datetime(2026, 9, 21, 8, 40, tzinfo=CST)


def row(title, when, columns=('其他',)):
    return {'time': when, 'title': title, 'columns': list(columns)}


def at(text):
    return datetime.fromisoformat('2026-09-%sT%s+08:00' % tuple(text.split(' ')))


class ClassifyTests(unittest.TestCase):
    def test_high_risk_titles_block(self):
        for title, reason in (('某某:关于股东减持股份计划的预披露公告', '股东/高管减持'), ('某某:关于收到立案告知书的公告', '被立案调查'),
                              ('某某:2026年半年度业绩预亏公告', '业绩预亏'), ('某某:关于公司股票停牌的公告', '停牌'),
                              ('某某:关于公司股票被实施退市风险警示的公告', '退市风险'), ('某某:收到行政处罚决定书', '行政处罚'),
                              ('某某:关于持股5%以上股东股份被司法冻结的公告', '股份被冻结')):
            self.assertEqual(an.classify(title), ('block', reason), title)

    def test_titles_that_negate_the_keyword_are_only_flagged(self):
        for title in ('某某:关于不存在减持计划的说明', '某某:关于股东减持计划实施完毕的公告', '某某:关于停牌核查结果及复牌的公告',
                      '某某:关于股东承诺不减持的公告'):
            level, reason = an.classify(title)
            self.assertEqual(level, 'flag', title)
            self.assertIn('人工看', reason)

    def test_worrying_but_not_disqualifying_titles_are_flagged(self):
        for title in ('某某:收到深圳证券交易所问询函', '某某:股票交易异常波动公告', '某某:关于限售股份上市流通的提示性公告',
                      '某某:关于筹划重大资产重组的提示性公告', '某某:关于股份质押的公告',
                      '某某:关于变更董事长、法定代表人的公告', '某某:关于董事辞职的公告'):
            self.assertEqual(an.classify(title)[0], 'flag', title)

    def test_ordinary_titles_are_info(self):
        for title in ('贵州茅台:关于召开2026年半年度业绩说明会的公告', '某某:2026年第三季度报告', '某某:关于回购股份进展的公告',
                      '某某:关于2026年半年度业绩预增的公告'):
            self.assertEqual(an.classify(title)[0], 'info', title)


class CheckSymbolTests(unittest.TestCase):
    def check(self, rows, symbol='sh600000'):
        return an.check_symbol(symbol, SINCE, NOW, fetch_fn=lambda s: rows)

    def test_only_announcements_after_the_cutoff_count(self):
        r = self.check([row('某某:关于股东减持股份的公告', at('18 14:59:59')), row('某某:普通公告', at('18 20:10:00'))])
        self.assertEqual(r['status'], 'clear')                                # 收盘前的减持公告，K 线已经反映过了
        self.assertEqual(len(r['items']), 1)
        r = self.check([row('某某:关于股东减持股份的公告', at('18 15:00:01'))])
        self.assertEqual(r['status'], 'block')                                # 刚过 15:00 的算

    def test_status_is_the_worst_level_and_items_are_ordered_by_severity(self):
        r = self.check([row('某某:普通公告', at('19 20:00:00')), row('某某:收到问询函', at('19 21:00:00')),
                        row('某某:业绩预亏公告', at('19 22:00:00'))])
        self.assertEqual(r['status'], 'block')
        self.assertEqual([i['level'] for i in r['items']], ['block', 'flag', 'info'])
        self.assertEqual(r['total_items'], 3)

    def test_titles_kept_are_capped(self):
        r = self.check([row('某某:公告%d' % i, at('19 20:%02d:00' % i)) for i in range(20)])
        self.assertEqual((len(r['items']), r['total_items']), (an.MAX_TITLES, 20))

    def test_fetch_failure_is_unverified_never_clear(self):
        def boom(symbol):
            raise an.AnnouncementError('timeout')
        r = an.check_symbol('sh600000', SINCE, NOW, fetch_fn=boom)
        self.assertEqual((r['status'], r['items'], r['error']), ('unverified', [], 'timeout'))

    def test_unsupported_codes_are_unverified_without_a_request(self):
        called = []
        r = an.check_symbol('bj430001', SINCE, NOW, fetch_fn=lambda s: called.append(s))
        self.assertEqual((r['status'], called), ('unverified', []))


class CheckManyTests(unittest.TestCase):
    def test_every_symbol_gets_an_answer_and_requests_are_spaced(self):
        sleeps = []
        out = an.check_many(['sh600000', 'sh600001', 'sz000001'], SINCE, NOW, fetch_fn=lambda s: [], sleep=sleeps.append)
        self.assertEqual(sorted(out), ['sh600000', 'sh600001', 'sz000001'])
        self.assertEqual(len(sleeps), 3)

    def test_running_out_of_time_marks_the_rest_unverified_instead_of_hanging(self):
        clock = iter([0, 1, 2, an.TOTAL_BUDGET_S + 5, an.TOTAL_BUDGET_S + 6])
        out = an.check_many(['sh600001', 'sh600002', 'sh600003', 'sh600004'], SINCE, NOW, fetch_fn=lambda s: [],
                            sleep=lambda d: None, monotonic=lambda: next(clock))
        self.assertEqual([out[s]['status'] for s in ('sh600001', 'sh600002')], ['clear', 'clear'])       # 预算内的正常核查
        self.assertEqual([out[s]['status'] for s in ('sh600003', 'sh600004')], ['unverified', 'unverified'])
        self.assertIn('时间预算', out['sh600003']['error'])


class FakeAnnouncementServer:
    def __init__(self, script):
        self.script, self.paths = script, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                status, body = outer.script.pop(0) if outer.script else (200, b'{}')
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d/ann?' % self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def payload(items):
    return json.dumps({'data': {'list': [{'art_code': 'A%d' % i, 'title': t, 'title_ch': t, 'display_time': when,
                                          'columns': [{'column_name': '其他'}]} for i, (t, when) in enumerate(items)]}}).encode()


class FetchTests(unittest.TestCase):
    def serve(self, script):
        fake = FakeAnnouncementServer(script)
        self.addCleanup(fake.close)
        p = patch.object(an, 'ENDPOINT', fake.url)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_parses_the_real_response_shape_and_asks_for_the_right_stock(self):
        fake = self.serve([(200, payload([('某某:关于减持的公告', '2026-09-18 21:18:08:390'), ('某某:普通公告', '2026-09-17 09:00:00:000')]))])
        rows = an.fetch('sh600519')
        self.assertEqual([r['title'] for r in rows], ['某某:关于减持的公告', '某某:普通公告'])
        self.assertEqual(rows[0]['time'], datetime(2026, 9, 18, 21, 18, 8, tzinfo=CST))
        self.assertIn('stock_list=600519', fake.paths[0])
        self.assertEqual(rows[0]['columns'], ['其他'])

    def test_every_failure_mode_becomes_an_announcement_error(self):
        for script in ([(500, b'boom')], [(200, b'not json')], [(200, b'{"data": null}')], [(200, b'{"data": {"list": [{"x": 1}]}}')]):
            self.serve(script)
            with self.assertRaises(an.AnnouncementError):
                an.fetch('sh600519')

    def test_unreachable_server_is_an_error_not_a_hang(self):
        with patch.object(an, 'ENDPOINT', 'http://127.0.0.1:1/ann?'):
            with self.assertRaises(an.AnnouncementError):
                an.fetch('sh600519', timeout=1)

    def test_end_to_end_check_symbol_over_http(self):
        self.serve([(200, payload([('某某:关于股东减持股份计划的公告', '2026-09-18 21:18:08:390')]))])
        r = an.check_symbol('sh600519', SINCE, NOW)
        self.assertEqual(r['status'], 'block')


if __name__ == '__main__':
    unittest.main()
