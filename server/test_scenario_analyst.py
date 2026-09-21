import json
import unittest

import scenario_analyst as sa


def scenario(direction='up', trig=10.5, lo=10.7, hi=10.9, inv=9.8, label='x'):
    return {'label': label, 'direction': direction, 'trigger_price': trig, 'trigger_condition': 'c',
            'target_low': lo, 'target_high': hi, 'invalidate_price': inv}


def entry(scenarios, hint='wait', conf=3):
    return {'symbol': 'sz000001', 'current_read': 'r', 'scenarios': scenarios, 'watch_metrics': [],
            'action_hint': hint, 'confidence': conf, 'caveats': []}


LIMITS = dict(limit_up=11.0, limit_down=9.0)


class ScenarioValidationTests(unittest.TestCase):
    def ok(self, sc, last=10.0, **kw):
        return sa.validate_scenario(sc, last, **{**LIMITS, **kw})

    def test_a_coherent_up_and_down_scenario_pass(self):
        self.assertEqual(self.ok(scenario(trig=10.5, lo=10.7, hi=10.9, inv=9.8))[0], True)
        self.assertEqual(self.ok(scenario('down', trig=9.8, lo=9.3, hi=9.6, inv=10.2))[0], True)

    def test_trigger_that_is_already_reached_is_not_a_trigger(self):
        """"已经到达的价位不叫触发条件"——现价 10.0 时说"若站上 9.8"毫无意义。"""
        self.assertFalse(self.ok(scenario(trig=9.8, lo=10.7, hi=10.9, inv=9.5))[0])
        self.assertFalse(self.ok(scenario('down', trig=10.2, lo=9.3, hi=9.6, inv=10.4))[0])

    def test_invalidation_and_target_must_sit_on_the_correct_side_of_the_trigger(self):
        self.assertFalse(self.ok(scenario(trig=10.5, lo=10.7, hi=10.9, inv=10.6))[0])      # 失效价在触发价上方
        self.assertFalse(self.ok(scenario(trig=10.5, lo=10.2, hi=10.4, inv=10.1))[0])      # 目标在触发价下方
        self.assertFalse(self.ok(scenario('down', trig=9.8, lo=9.3, hi=9.9, inv=10.2))[0])  # 目标上沿高于触发价
        self.assertFalse(self.ok(scenario('down', trig=9.8, lo=9.3, hi=9.6, inv=9.7))[0])   # 失效价低于触发价

    def test_an_invalidation_price_on_the_wrong_side_of_the_current_price_is_already_invalid(self):
        """现价 10.0，"若站上 10.5，但跌破 10.2 则失效"——现价就已经低于 10.2 了，这个情景一发布就作废。
        早先只校验了"失效价低于触发价"，这种自相矛盾的情景会被放行推送给用户。"""
        r = self.ok(scenario(trig=10.5, lo=10.7, hi=10.9, inv=10.2))
        self.assertFalse(r[0])
        self.assertIn('发布时就已失效', r[1])
        self.assertFalse(self.ok(scenario('down', trig=9.8, lo=9.3, hi=9.6, inv=9.9))[0])

    def test_prices_beyond_the_limit_band_are_impossible(self):
        r = self.ok(scenario(trig=10.5, lo=11.5, hi=12.0, inv=10.2))                       # 涨停价 11.0
        self.assertFalse(r[0])
        self.assertIn('涨跌停', r[1])

    def test_target_zone_must_be_ordered(self):
        self.assertFalse(self.ok(scenario(lo=10.9, hi=10.7))[0])

    def test_non_numeric_or_bad_numbers_are_rejected(self):
        """价位必须是数值——写成"站稳1260附近"就没法收盘对账。"""
        for bad in ('10.5', None, -1, 0, float('nan'), True):
            self.assertFalse(self.ok({**scenario(), 'trigger_price': bad})[0], bad)

    def test_bad_direction(self):
        self.assertFalse(self.ok({**scenario(), 'direction': 'sideways'})[0])

    def test_without_limit_information_the_band_check_is_skipped_not_assumed(self):
        self.assertTrue(sa.validate_scenario(scenario(trig=10.5, lo=13, hi=14, inv=9.8), 10.0)[0])


class EntryValidationTests(unittest.TestCase):
    def clean(self, e, **kw):
        return sa.validate_entry(e, 10.0, is_holding=kw.pop('is_holding', True), **{**LIMITS, **kw})

    def test_invalid_scenarios_are_dropped_and_reported_not_silently_kept(self):
        e, problems = self.clean(entry([scenario(), scenario(trig=9.0, label='坏的')]))
        self.assertEqual(len(e['scenarios']), 1)
        self.assertTrue(any('坏的' in p for p in problems))

    def test_no_valid_scenario_is_called_out(self):
        e, problems = self.clean(entry([scenario(trig=9.0)]))
        self.assertEqual(e['scenarios'], [])
        self.assertTrue(any('没有任何合格' in p for p in problems))

    def test_t_hints_require_a_declared_base(self):
        e, problems = self.clean(entry([scenario()], hint='t_sell_high'), t_base=0)
        self.assertEqual(e['action_hint'], 'wait')
        self.assertTrue(any('底仓' in p for p in problems))
        e, _ = self.clean(entry([scenario()], hint='t_sell_high'), t_base=500)
        self.assertEqual(e['action_hint'], 't_sell_high')

    def test_holding_only_hints_are_downgraded_for_non_holdings(self):
        for hint in ('hold', 'reduce'):
            e, _ = self.clean(entry([scenario()], hint=hint), is_holding=False)
            self.assertEqual(e['action_hint'], 'wait')
        e, _ = self.clean(entry([scenario()], hint='add'), is_holding=False)
        self.assertEqual(e['action_hint'], 'add')

    def test_confidence_is_clamped_with_a_note(self):
        for bad in (0, 6, 3.5, '4', True):
            e, problems = self.clean(entry([scenario()], conf=bad))
            self.assertEqual(e['confidence'], 1, bad)
            self.assertTrue(any('confidence' in p for p in problems))


class KeyLevelTests(unittest.TestCase):
    def test_only_real_levels_are_offered_and_holdings_add_the_systems_own(self):
        q = {'previous_close': '10', 'open': '10.1'}
        lv = sa.key_levels(q, {'ma20': 10.4, 'ma60': None}, {'day_high': 10.8, 'day_low': 9.9, 'vwap': 10.2},
                           holding={'cost_price': 9.5, 'stop_price': 9.0, 'target_price': None},
                           limits={'limit_up': 11.0, 'limit_down': 9.0})
        self.assertEqual(lv['系统止损位'], 9.0)
        self.assertNotIn('系统止盈位', lv)              # 算不出来就不出现，不替用户造一个
        self.assertNotIn('你的止损价', lv)
        self.assertNotIn('MA60', lv)
        self.assertEqual(lv['分时均价线'], 10.2)
        self.assertNotIn('成本价', sa.key_levels(q, {}, {}, holding=None))


class PromptAndSchemaTests(unittest.TestCase):
    def test_prompt_keeps_candidate_strategy_rules_out_of_your_positions(self):
        p = sa.system_prompt()
        self.assertIn('不知道用户为什么买', p)
        self.assertNotIn('突破(breakout)', p)
        self.assertNotIn('最长持有', p)

    def test_prompt_forbids_news_and_path_prediction(self):
        p = sa.system_prompt()
        self.assertIn('没有任何新闻', p)
        self.assertIn('无法预测日内走势', p)

    def test_prompt_demands_numeric_levels_with_the_correct_sides(self):
        p = sa.system_prompt()
        self.assertIn('高于现价', p)
        self.assertIn('key_levels', p)

    def test_schema_uses_only_supported_keywords_and_numeric_levels(self):
        seen = set()

        def walk(node):
            if isinstance(node, dict):
                seen.update(node)
                for k, v in node.items():
                    if k in ('properties',):
                        [walk(c) for c in v.values()]
                    elif k in ('items',):
                        walk(v)
            elif isinstance(node, list):
                [walk(x) for x in node]
        walk(sa.SCHEMA)
        self.assertEqual(seen - sa.SUPPORTED_KEYWORDS, set())
        sc = sa.SCHEMA['properties']['stocks']['items']['properties']['scenarios']['items']['properties']
        for k in ('trigger_price', 'target_low', 'target_high', 'invalidate_price'):
            self.assertEqual(sc[k]['type'], 'number')

    def test_schema_round_trips_through_the_sdks_own_transform_when_available(self):
        try:
            from anthropic.lib._parse._transform import transform_schema
        except ImportError:
            self.skipTest('anthropic 未安装')
        self.assertEqual(transform_schema(json.loads(json.dumps(sa.SCHEMA))), sa.SCHEMA)

    def test_hints_exclude_swing_t_from_the_old_schema(self):
        self.assertEqual(sa.HINTS, ['hold', 'add', 'reduce', 'wait', 't_sell_high', 't_buy_low'])


if __name__ == '__main__':
    unittest.main()
