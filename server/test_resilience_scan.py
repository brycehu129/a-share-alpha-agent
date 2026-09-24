import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import market_rankings as mr
import resilience_scan as rs
from collect_quotes import CST

NOW = datetime(2026, 9, 24, 10, 30, tzinfo=CST)


def sector(name, change, flow_pct, up=6, down=4, flat=0):
    return {'f12': 'BK' + name, 'f14': name, 'f3': change, 'f62': 1e8, 'f184': flow_pct,
            'f104': up, 'f105': down, 'f106': flat, 'f2': 100}


def stock(code, name, change, flow, flow_pct, industry='半导体', cap=80e8, high=10.8, low=10.2):
    return {'f12': code, 'f14': name, 'f2': 10.5, 'f3': change, 'f62': flow, 'f184': flow_pct,
            'f100': industry, 'f103': '芯片', 'f20': cap * 1.2, 'f21': cap,
            'f15': high, 'f16': low, 'f17': 10.3, 'f18': 10.0}


def quotes(hs300=-1.5, zz500=-1.2, zz1000=-1.0, sse=-1.4):
    return {'session': 'morning', 'failures': [], 'quotes': [
        {'symbol': 'sh000300', 'change_pct': hs300}, {'symbol': 'sh000905', 'change_pct': zz500},
        {'symbol': 'sh000852', 'change_pct': zz1000}, {'symbol': 'sh000001', 'change_pct': sse}]}


def sina_sector(name, change_pct, flow_pct, net=1e8, code=None):
    """新浪的数值全是字符串，涨跌幅与净流入占比是小数（0.0879 = 8.79%）。"""
    return {'cate_type': '0', 'category': code or ('new_' + name), 'name': name,
            'avg_price': '10.0', 'avg_changeratio': str(change_pct / 100),
            'turnover': '100.0', 'inamount': '2e8', 'outamount': '1e8',
            'netamount': str(net), 'ratioamount': str(flow_pct / 100),
            'ts_symbol': 'sh600001', 'ts_name': '领涨', 'ts_trade': '10.0',
            'ts_changeratio': '0.1', 'ts_ratioamount': '0.5'}


def http_for(boards, stocks, sina_boards=None):
    """按主机分派：新浪 vip.stock... 走新浪形状，东财 push2 走 clist 形状。
    sina_boards=None 表示新浪取不到，用来测退回东财的兜底路径。"""
    def http(url):
        if 'sina' in url:
            if sina_boards is None:
                raise OSError('sina down')
            return json.dumps(sina_boards).encode()
        return json.dumps({'data': {'diff': boards if 'm%3A90' in url else stocks,
                                    'total': 496 if 'm%3A90' in url else 5561}}).encode()
    return http


def with_map(tmpdir, mapping, trade_date=None):
    """写一份个股→新浪行业的映射表，返回可传给 scan(sector_dir=) 的目录。"""
    import sina_sectors
    d = Path(tmpdir)
    sina_sectors.save_map({'trade_date': trade_date or datetime.now(CST).date().isoformat(),
                           'built_at': NOW.isoformat(), 'source': 'sina',
                           'sector_count': len(set(mapping.values())),
                           'symbol_count': len(mapping), 'by_symbol': mapping}, d)
    return d


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_follows_float_cap_tiers(self):
        self.assertEqual(rs.benchmark_for(800e8)[0], 'sh000300')
        self.assertEqual(rs.benchmark_for(200e8)[0], 'sh000905')
        self.assertEqual(rs.benchmark_for(30e8)[0], 'sh000852')

    def test_missing_cap_falls_back_to_primary_and_says_so(self):
        """市值缺失时退回沪深300，但必须把"没做体量匹配"标出来——
        拿微盘股比沪深300 会把"本来就不跟大盘"当成抗跌。"""
        symbol, matched = rs.benchmark_for(None)
        self.assertEqual(symbol, rs.PRIMARY)
        self.assertFalse(matched)

    def test_index_changes_ignores_unknown_symbols_and_missing_values(self):
        snap = {'quotes': [{'symbol': 'sh000300', 'change_pct': -1.5},
                           {'symbol': 'sh000001', 'change_pct': None},
                           {'symbol': 'sh600000', 'change_pct': 2.0}]}
        self.assertEqual(rs.index_changes(snap), {'sh000300': -1.5})


class BuildRowTests(unittest.TestCase):
    def rows(self, stocks, boards=(('半导体', 1.8, 6.0),), changes=None):
        parsed = mr.parse_stocks({'data': {'diff': stocks}}, 'x')
        scored = mr.score_sectors(mr.parse_sectors(
            {'data': {'diff': [sector(n, c, f) for n, c, f in boards]}}, 'industry'))
        by_name = {r['name']: r for r in scored}
        # 注意用 is None 而不是 or：传 {} 表示"指数一个都没取到"，是一个要测的真实场景。
        if changes is None:
            changes = {'sh000300': -1.5, 'sh000905': -1.2, 'sh000852': -1.0, 'sh000001': -1.4}
        # 东财口径的解析方式：个股归属用响应里自带的 f100
        resolve = lambda s: by_name.get(s.get('industry')) if s.get('industry') else None
        return rs.build_rows(parsed, resolve, changes, sector_source='eastmoney')

    def test_excess_is_measured_against_the_size_matched_index(self):
        """流通市值 80 亿 → 中证1000（-1.0%）。个股 +0.5% 的抗跌度应是 1.5pp，
        而不是拿沪深300（-1.5%）算出来的 2.0pp。"""
        row = self.rows([stock('300001', '小票', 0.5, 3e8, 8.0, cap=80e8)])[0]
        self.assertEqual(row['benchmark'], 'sh000852')
        self.assertTrue(row['benchmark_size_matched'])
        self.assertEqual(row['excess_pp'], 1.5)
        self.assertEqual(row['excess_hs300'], 2.0)      # 旧口径同时留档，便于前后对照

    def test_sector_columns_come_from_the_board_lookup(self):
        row = self.rows([stock('300001', '甲', 0.5, 3e8, 8.0, industry='半导体')])[0]
        self.assertTrue(row['sector_matched'])
        self.assertEqual(row['sector_change_pct'], 1.8)
        self.assertEqual(row['sector_main_net_pct'], 6.0)

    def test_unmatched_industry_degrades_instead_of_dropping_the_row(self):
        """板块名对不上（f100 与板块清单词表可能有出入）时，这一行仍要留档，
        只是板块列为空——静默丢弃会让留档里凭空少掉一批票，事后无从发现。"""
        row = self.rows([stock('300001', '甲', 0.5, 3e8, 8.0, industry='查无此板块')])[0]
        self.assertFalse(row['sector_matched'])
        self.assertIsNone(row['sector_change_pct'])
        self.assertEqual(row['name'], '甲')

    def test_rows_are_not_filtered_only_sorted(self):
        """build_rows 永远返回全量——过滤是页面的事，留档要全。"""
        rows = self.rows([stock('300001', '强', 1.0, 3e8, 8.0),
                          stock('300002', '弱', -3.0, -1e8, -5.0)])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['name'], '强')          # 按 score 降序

    def test_market_down_is_recorded_per_row(self):
        down = self.rows([stock('300001', '甲', 0.5, 3e8, 8.0)])[0]
        self.assertTrue(down['market_down'])
        up = self.rows([stock('300001', '甲', 0.5, 3e8, 8.0)],
                       changes={'sh000300': 1.2, 'sh000852': 1.0})[0]
        self.assertFalse(up['market_down'])

    def test_missing_index_leaves_excess_none_rather_than_zero(self):
        row = self.rows([stock('300001', '甲', 0.5, 3e8, 8.0)], changes={})[0]
        self.assertIsNone(row['excess_pp'])
        self.assertIsNone(row['score'])


class ExclusionTests(unittest.TestCase):
    def test_st_is_recognised_from_the_name(self):
        for name in ('*ST尔雅', 'ST明诚', '退市海润', 'ＳＴ 康美'.replace('Ｓ', 'S').replace('Ｔ', 'T')):
            self.assertTrue(rs.is_st(name), name)
        for name in ('国瓷材料', '中国海油', '华茂股份'):
            self.assertFalse(rs.is_st(name), name)

    def test_limit_threshold_differs_by_board_and_for_st(self):
        self.assertEqual(rs.limit_threshold('sz300001', '甲'), 19.5)
        self.assertEqual(rs.limit_threshold('sh688001', '甲'), 19.5)
        self.assertEqual(rs.limit_threshold('sh600001', '甲'), 9.8)
        self.assertEqual(rs.limit_threshold('sh600107', '*ST尔雅'), 4.8)

    def test_one_word_limit_needs_high_to_equal_low(self):
        """一字板 = 全天只有一个价，买不进。只是盘中封板（有过更低成交）不算。"""
        one_word = {'symbol': 'sh600001', 'name': '甲', 'change_pct': 10.0, 'high': 11.0, 'low': 11.0}
        self.assertEqual(rs.limit_state(one_word), (True, True))
        sealed = {'symbol': 'sh600001', 'name': '甲', 'change_pct': 10.0, 'high': 11.0, 'low': 10.2}
        self.assertEqual(rs.limit_state(sealed), (True, False))

    def test_missing_high_low_is_not_treated_as_one_word(self):
        """高低价缺失当成"满足条件"会凭空排除一批票——缺失就是缺失。"""
        self.assertEqual(rs.limit_state(
            {'symbol': 'sh600001', 'name': '甲', 'change_pct': 10.0, 'high': None, 'low': None}),
            (True, False))

    def test_st_and_one_word_are_kept_in_rows_but_never_hit(self):
        """结构性排除的票仍要留档并带标志位，否则事后无法回答
        "被排除的那些后来怎么样了"。"""
        parsed = mr.parse_stocks({'data': {'diff': [
            stock('600107', '*ST尔雅', 4.9, 3e8, 30.0, high=10.5, low=10.1),
            stock('600001', '一字板', 10.0, 9e8, 20.0, high=11.0, low=11.0),
            stock('600002', '正常票', 1.0, 9e8, 8.0),
        ]}}, 'x')
        scored = mr.score_sectors(mr.parse_sectors(
            {'data': {'diff': [sector('半导体', 1.8, 6.0)]}}, 'industry'))
        by_name = {r['name']: r for r in scored}
        rows = rs.build_rows(parsed, lambda s: by_name.get(s.get('industry')),
                             {'sh000300': -1.5, 'sh000852': -1.0}, sector_source='eastmoney')
        self.assertEqual(len(rows), 3)                                  # 全都留档
        by_name = {r['name']: r for r in rows}
        self.assertTrue(by_name['*ST尔雅']['is_st'])
        self.assertTrue(by_name['一字板']['one_word_limit'])
        self.assertFalse(by_name['正常票']['is_st'])
        hits = [r['name'] for r in rows if rs.passes(r)]
        self.assertEqual(hits, ['正常票'])                                # 但只有正常票命中

    def test_exclusions_can_be_turned_off_as_display_filters(self):
        row = {'is_st': True, 'one_word_limit': False, 'excess_pp': 5.0, 'main_net': 3e8,
               'main_net_pct': 30.0, 'holds_up': True, 'sector_matched': True, 'sector_change_pct': 1.8}
        self.assertFalse(rs.passes(row))
        self.assertTrue(rs.passes(row, {'exclude_st': False}))


class ThresholdTests(unittest.TestCase):
    def row(self, **kw):
        base = {'excess_pp': 3.0, 'main_net': 3e8, 'main_net_pct': 8.0, 'holds_up': True,
                'sector_matched': True, 'sector_change_pct': 1.8,
                'is_st': False, 'one_word_limit': False}
        return {**base, **kw}

    def test_default_thresholds_accept_a_clean_hit(self):
        self.assertTrue(rs.passes(self.row()))

    def test_outflow_is_rejected_even_with_strong_excess(self):
        self.assertFalse(rs.passes(self.row(main_net=-1e8)))

    def test_weak_excess_is_rejected(self):
        self.assertFalse(rs.passes(self.row(excess_pp=0.5)))

    def test_sector_requirement_needs_both_match_and_a_rising_board(self):
        self.assertFalse(rs.passes(self.row(sector_matched=False)))
        self.assertFalse(rs.passes(self.row(sector_change_pct=-0.4)))

    def test_sector_requirement_can_be_relaxed_as_a_display_filter(self):
        self.assertTrue(rs.passes(self.row(sector_matched=False), {'require_sector': False}))

    def test_holds_up_is_off_by_default_but_can_be_required(self):
        falling = self.row(holds_up=False)
        self.assertTrue(rs.passes(falling))
        self.assertFalse(rs.passes(falling, {'require_holds_up': True}))


class SectorSourceTests(unittest.TestCase):
    """板块数据源：新浪为主、东财兜底。两家行业名对不上，所以归属解析方式不同。"""

    def setUp(self):
        rs._cache.clear()

    def test_eastmoney_is_primary_and_resolves_sector_from_the_same_response(self):
        """东财是主力：每只股票的行业在响应里白送（f100），覆盖 100%，不需要映射表。"""
        with tempfile.TemporaryDirectory() as d:
            sector_dir = with_map(d, {'sh600001': '传媒娱乐'})
            http = http_for([sector('出版', 1.8, 6.0)],
                            [stock('600001', '甲', 1.0, 9e8, 8.0, industry='出版')],
                            sina_boards=[sina_sector('传媒娱乐', -0.24, 8.79)])
            out = rs.scan(NOW, http=http, quote_fn=lambda _s: quotes(), now_ts=100,
                          sector_dir=sector_dir)
            row = out['rows'][0]
            self.assertEqual(out['universe']['sector_source'], 'eastmoney')
            self.assertFalse(out['universe']['sector_degraded'])
            self.assertEqual(row['sector_name'], '出版')
            self.assertEqual(row['sector_change_pct'], 1.8)

    def test_falls_back_to_sina_when_eastmoney_sectors_are_down(self):
        """退到新浪时口径变粗（48 vs 496）且只覆盖约半数个股，所以必须标 degraded。"""
        with tempfile.TemporaryDirectory() as d:
            sector_dir = with_map(d, {'sh600001': '传媒娱乐'})

            def http(url):
                if 'sina' in url:
                    return json.dumps([sina_sector('传媒娱乐', -0.24, 8.79)]).encode()
                if 'm%3A90' in url:
                    raise OSError('东财板块挂了')
                return json.dumps({'data': {'diff': [stock('600001', '甲', 1.0, 9e8, 8.0,
                                                           industry='出版')], 'total': 5561}}).encode()
            out = rs.scan(NOW, http=http, quote_fn=lambda _s: quotes(), now_ts=100,
                          sector_dir=sector_dir)
            row = out['rows'][0]
            self.assertEqual(out['universe']['sector_source'], 'sina')
            self.assertTrue(out['universe']['sector_degraded'])
            self.assertEqual(row['sector_name'], '传媒娱乐')     # 新浪粗口径
            self.assertEqual(row['industry'], '出版')            # 东财口径仍原样留档
            self.assertEqual(row['sector_strength_basis'], 'sina-2dim')
            self.assertIn('科创板', out['universe']['sector_scope'])

    def test_sina_fallback_needs_the_membership_map(self):
        """新浪不在响应里给个股行业，没有映射表它就顶不了——此时应报缺失而不是假装可用。"""
        with tempfile.TemporaryDirectory() as d:
            def http(url):
                if 'sina' in url:
                    return json.dumps([sina_sector('传媒娱乐', -0.24, 8.79)]).encode()
                if 'm%3A90' in url:
                    raise OSError('东财板块挂了')
                return json.dumps({'data': {'diff': [stock('600001', '甲', 1.0, 9e8, 8.0)],
                                            'total': 5561}}).encode()
            out = rs.scan(NOW, http=http, quote_fn=lambda _s: quotes(), now_ts=100,
                          sector_dir=Path(d))                    # 目录里没有映射表
            self.assertFalse(out['universe']['sector_data_available'])
            self.assertIn('sectors_sina', out['errors'])

    def test_stale_membership_map_is_rejected(self):
        """过期的表比没有表更危险：成分股变了却照旧用，错得悄无声息。"""
        import sina_sectors
        with tempfile.TemporaryDirectory() as d:
            with_map(d, {'sh600001': '传媒娱乐'}, trade_date='2026-01-01')
            self.assertIsNone(sina_sectors.load_map(Path(d)))
            self.assertIsNotNone(sina_sectors.load_map(Path(d), max_age_days=None))

    def test_both_sources_down_leaves_rows_but_no_sector_columns(self):
        with tempfile.TemporaryDirectory() as d:
            def http(url):
                if 'sina' in url or 'm%3A90' in url:
                    raise OSError('板块源都挂了')
                return json.dumps({'data': {'diff': [stock('600001', '甲', 1.0, 9e8, 8.0)],
                                            'total': 5561}}).encode()
            out = rs.scan(NOW, http=http, quote_fn=lambda _s: quotes(), now_ts=100,
                          sector_dir=Path(d))
            self.assertEqual(len(out['rows']), 1)
            self.assertFalse(out['rows'][0]['sector_matched'])
            self.assertIsNone(out['rows'][0]['sector_source'])
            self.assertFalse(out['universe']['sector_data_available'])


class ScanTests(unittest.TestCase):
    def setUp(self):
        rs._cache.clear()

    def test_scan_merges_both_stock_rankings_and_deduplicates(self):
        """按金额和按占比两个榜单会重叠；同一只票只能出现一次。"""
        calls = []

        def http(url):
            calls.append(url)
            if 'm%3A90' in url:
                return json.dumps({'data': {'diff': [sector('半导体', 1.8, 6.0)], 'total': 496}}).encode()
            rows = ([stock('600001', '甲', 1.0, 9e8, 4.0)] if 'fid=f62' in url
                    else [stock('600001', '甲', 1.0, 9e8, 4.0), stock('300002', '乙', 0.8, 2e8, 12.0)])
            return json.dumps({'data': {'diff': rows, 'total': 5561}}).encode()

        out = rs.scan(NOW, http=http, quote_fn=lambda _s: quotes(), now_ts=100)
        self.assertEqual(sorted(r['symbol'] for r in out['rows']), ['sh600001', 'sz300002'])
        self.assertEqual(out['universe']['stocks_scanned'], 2)
        self.assertEqual(sum('fid=f62' in u for u in calls), 1)
        self.assertEqual(sum('fid=f184' in u and 'm%3A90' not in u for u in calls), 1)

    def test_scan_reports_market_state_from_the_primary_index(self):
        out = rs.scan(NOW, http=http_for([sector('半导体', 1.8, 6.0)], [stock('600001', '甲', 1.0, 9e8, 4.0)]),
                      quote_fn=lambda _s: quotes(hs300=-1.5), now_ts=100)
        self.assertTrue(out['market']['market_down'])
        self.assertEqual(out['market']['changes']['sh000300'], -1.5)

    def test_a_failing_source_falls_back_to_the_last_success_and_is_marked_stale(self):
        good = http_for([sector('半导体', 1.8, 6.0)], [stock('600001', '甲', 1.0, 9e8, 4.0)])
        first = rs.scan(NOW, http=good, quote_fn=lambda _s: quotes(), now_ts=100)
        self.assertFalse(first['stale'])
        boom = lambda _u: (_ for _ in ()).throw(OSError('502'))
        second = rs.scan(NOW, http=boom, quote_fn=lambda _s: quotes(), now_ts=160)
        # 板块源是两级（东财优先、新浪兜底），东财能沿用上一次成功值就不会走到新浪
        self.assertEqual(set(second['stale']), {'sectors_eastmoney', 'inflow_amount', 'inflow_ratio'})
        self.assertEqual(second['universe']['stocks_scanned'], 1)

    def test_stale_data_expires_instead_of_being_shown_forever(self):
        good = http_for([sector('半导体', 1.8, 6.0)], [stock('600001', '甲', 1.0, 9e8, 4.0)])
        rs.scan(NOW, http=good, quote_fn=lambda _s: quotes(), now_ts=100)
        boom = lambda _u: (_ for _ in ()).throw(OSError('502'))
        expired = rs.scan(NOW, http=boom, quote_fn=lambda _s: quotes(), now_ts=1100)
        self.assertEqual(expired['universe']['stocks_scanned'], 0)
        self.assertIn('inflow_amount', expired['errors'])

    def test_index_failure_leaves_rows_but_no_excess(self):
        """东财活着、腾讯挂了：票还在，但抗跌度算不出来，不能拿 0 冒充。"""
        good = http_for([sector('半导体', 1.8, 6.0)], [stock('600001', '甲', 1.0, 9e8, 4.0)])
        out = rs.scan(NOW, http=good, quote_fn=lambda _s: (_ for _ in ()).throw(OSError('腾讯挂了')),
                      now_ts=100)
        self.assertEqual(len(out['rows']), 1)
        self.assertIsNone(out['rows'][0]['excess_pp'])
        self.assertEqual(out['hits'], [])
        self.assertIn('indices', out['errors'])

    def test_missing_sector_data_is_distinguishable_from_no_qualifying_stocks(self):
        """板块那次请求挂掉时，所有行的 sector_matched 都是 False，命中必然 0。
        界面必须能分清"判断不了"和"没有符合条件的票"，所以要有一个显式的标志位。"""
        def http(url):
            if 'm%3A90' in url:
                raise OSError('板块 502')
            return json.dumps({'data': {'diff': [stock('600001', '甲', 1.0, 9e8, 8.0)],
                                        'total': 5561}}).encode()
        out = rs.scan(NOW, http=http, quote_fn=lambda _s: quotes(), now_ts=100)
        self.assertEqual(len(out['rows']), 1)           # 票还在
        self.assertEqual(out['hits'], [])               # 但默认阈值下命中 0
        self.assertFalse(out['universe']['sector_data_available'])
        relaxed = [r for r in out['rows'] if rs.passes(r, {'require_sector': False})]
        self.assertEqual(len(relaxed), 1)               # 放开板块条件就有

    def test_sector_data_available_is_true_on_a_normal_scan(self):
        out = rs.scan(NOW, http=http_for([sector('半导体', 1.8, 6.0)],
                                         [stock('600001', '甲', 1.0, 9e8, 8.0)]),
                      quote_fn=lambda _s: quotes(), now_ts=100)
        self.assertTrue(out['universe']['sector_data_available'])

    def test_scan_never_raises_even_when_everything_fails(self):
        boom = lambda _u: (_ for _ in ()).throw(OSError('502'))
        out = rs.scan(NOW, http=boom, quote_fn=lambda _s: (_ for _ in ()).throw(OSError('x')), now_ts=100)
        self.assertEqual(out['rows'], [])
        # 5 个取数点：东财板块、新浪板块（兜底）、按额个股、按占比个股、指数
        self.assertEqual(set(out['errors']),
                         {'sectors_eastmoney', 'sectors_sina', 'inflow_amount',
                          'inflow_ratio', 'indices'})


class RecordTests(unittest.TestCase):
    def setUp(self):
        rs._cache.clear()

    def test_record_keeps_every_row_not_just_the_hits(self):
        """阈值以后会改，原始值改不了——留档必须是全量。"""
        with tempfile.TemporaryDirectory() as d:
            out = rs.scan(NOW, http=http_for([sector('半导体', 1.8, 6.0)],
                                             [stock('600001', '强', 1.0, 9e8, 8.0),
                                              stock('600002', '弱', -3.0, -1e8, -5.0)]),
                          quote_fn=lambda _s: quotes(), now_ts=100)
            rs.record(d, out)
            rows = rs.read_rows(d, '2026-09-24')
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]['rows']), 2)
            self.assertEqual(rows[0]['hit_symbols'], ['sh600001'])

    def test_timeline_reports_first_hit_time_per_symbol(self):
        """没有推送，页面靠 timeline 回答"10:15 出现过谁"；同一只票只报第一次。"""
        with tempfile.TemporaryDirectory() as d:
            http = http_for([sector('半导体', 1.8, 6.0)], [stock('600001', '甲', 1.0, 9e8, 8.0)])
            for minute in (15, 20, 25):
                rs._cache.clear()
                out = rs.scan(datetime(2026, 9, 24, 10, minute, tzinfo=CST), http=http,
                              quote_fn=lambda _s: quotes(), now_ts=100)
                rs.record(d, out)
            line = rs.timeline(d, '2026-09-24')
            self.assertEqual(len(line), 1)
            self.assertTrue(line[0]['at'].endswith('10:15:00+08:00'))

    def test_timeline_recomputes_with_the_given_thresholds(self):
        """改阈值后历史跟着重算——留档存的是原始值，不是当时的判定。"""
        with tempfile.TemporaryDirectory() as d:
            out = rs.scan(NOW, http=http_for([sector('半导体', -0.5, 6.0)],
                                             [stock('600001', '甲', 1.0, 9e8, 8.0)]),
                          quote_fn=lambda _s: quotes(), now_ts=100)
            rs.record(d, out)
            self.assertEqual(rs.timeline(d, '2026-09-24'), [])          # 板块在跌，默认过滤掉
            relaxed = rs.timeline(d, '2026-09-24', {'require_sector': False})
            self.assertEqual([r['name'] for r in relaxed], ['甲'])


class NoPushTests(unittest.TestCase):
    def test_module_never_imports_a_push_path(self):
        """本需求的硬要求：这个模块不推送任何东西。用源码断言，而不是靠记得。"""
        source = Path(rs.__file__).read_text(encoding='utf-8')
        for forbidden in ('wecom', 'notify', 'push_', 'alert_ledger', 'sentinel'):
            self.assertNotIn(forbidden, source, '扫描器不得接入推送路径: ' + forbidden)


if __name__ == '__main__':
    unittest.main()
