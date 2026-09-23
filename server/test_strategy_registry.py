"""server/strategies/ 骨架的接线测试：注册表完整性、运行期开关的存取、两个策略适配层各自
喂进真实 screen()/rank() 能不能跑通。不测选股打分本身——那是 test_shortterm_model.py /
test_reversal_0_1.py 的事。"""
import tempfile
import types
import unittest
from pathlib import Path

import strategies
from strategies import reversal_0_1, select_0_5
from test_shortterm_model import INDUSTRY_ROWS, breakout_fixture, screen_short_fixture


class RegistryIntegrityTests(unittest.TestCase):
    def test_registry_keys_match_module_strategy_id(self):
        for strategy_id, module in strategies.REGISTRY.items():
            self.assertEqual(module.STRATEGY_ID, strategy_id)

    def test_main_strategy_is_registered_and_tradable(self):
        self.assertIn(strategies.MAIN_STRATEGY, strategies.REGISTRY)
        self.assertTrue(strategies.REGISTRY[strategies.MAIN_STRATEGY].TRADABLE)

    def test_select_0_5_and_reversal_are_both_registered(self):
        self.assertEqual(set(strategies.REGISTRY), {'select-0.5', 'select-rev-0.1'})

    def test_reversal_is_not_tradable(self):
        self.assertFalse(strategies.REGISTRY['select-rev-0.1'].TRADABLE)

    def test_validate_accepts_a_complete_module(self):
        self.assertEqual(strategies.validate(select_0_5), [])
        self.assertEqual(strategies.validate(reversal_0_1), [])

    def test_validate_rejects_a_module_missing_required_pieces(self):
        stub = types.SimpleNamespace(STRATEGY_ID='x')
        problems = strategies.validate(stub)
        self.assertIn('缺少 NAME', problems)
        self.assertIn('缺少可调用的 screen', problems)
        self.assertIn('缺少可调用的 rank', problems)
        self.assertIn('缺少可调用的 track_of', problems)


class TogglesTests(unittest.TestCase):
    def setUp(self):
        self.history = Path(tempfile.mkdtemp())

    def test_missing_file_falls_back_to_default(self):
        self.assertEqual(strategies.load_toggles(self.history), strategies.default_toggles())

    def test_round_trip(self):
        strategies.save_toggles(self.history, ['select-0.5', 'select-rev-0.1'], 'select-0.5')
        loaded = strategies.load_toggles(self.history)
        self.assertEqual(loaded, {'enabled': ['select-0.5', 'select-rev-0.1'], 'main': 'select-0.5'})

    def test_unknown_strategy_id_is_rejected(self):
        with self.assertRaises(KeyError):
            strategies.save_toggles(self.history, ['select-0.5', 'not-a-real-strategy'], 'select-0.5')

    def test_main_must_be_in_enabled_list(self):
        with self.assertRaises(ValueError):
            strategies.save_toggles(self.history, ['select-rev-0.1'], 'select-0.5')

    def test_more_than_max_enabled_is_rejected(self):
        # Only 2 strategies are registered today, so testing the length cap in isolation
        # (without also tripping the unknown-id check) means repeating valid ids.
        with self.assertRaises(ValueError):
            strategies.save_toggles(self.history, ['select-0.5', 'select-rev-0.1', 'select-0.5', 'select-rev-0.1'],
                                     'select-0.5')

    def test_empty_enabled_list_is_rejected(self):
        with self.assertRaises(ValueError):
            strategies.save_toggles(self.history, [], 'select-0.5')

    def test_corrupt_file_falls_back_to_default_rather_than_crashing(self):
        path = self.history / 'strategies' / 'active.json'
        path.parent.mkdir(parents=True)
        path.write_text('not valid json envelope')
        self.assertEqual(strategies.load_toggles(self.history), strategies.default_toggles())

    def test_toggle_file_referencing_an_unknown_id_drops_it_instead_of_crashing(self):
        """比如某个策略以后被下线，旧的 active.json 里还留着它的 id——不该让整个读取失败。"""
        strategies.save_toggles(self.history, ['select-0.5'], 'select-0.5')
        path = self.history / 'strategies' / 'active.json'
        from tushare_sync import read, save
        data = read(path)
        data['enabled'].append('select-retired-9.9')
        save(path, data)
        loaded = strategies.load_toggles(self.history)
        self.assertEqual(loaded['enabled'], ['select-0.5'])


class Select05WrapperTests(unittest.TestCase):
    """select_0_5.py 只是个适配层——这里只验证接线对不对，不重复 test_shortterm_model.py
    已经测过的打分逻辑。"""

    def test_screen_and_rank_produce_the_same_shape_as_the_underlying_module(self):
        # screen_short_fixture (not breakout_fixture): world['mid']=None makes the wrapper
        # recompute alpha_model.screen() itself, which only forms a real 'IND' industry row
        # once >=5 same-industry stocks have valid features -- see that fixture's docstring.
        stocks, series, benchmark, cutoff = screen_short_fixture()
        world = {'stocks': stocks, 'series': series, 'benchmark': benchmark, 'cutoff': cutoff, 'mid': None}
        screened = select_0_5.screen(world, tuning=None, hotmoney=None)
        self.assertIn('breakout', screened)
        self.assertIn('pullback', screened)
        ranked = select_0_5.rank(screened, max_n=10)
        self.assertEqual([c['symbol'] for c in ranked], ['sz000001'])

    def test_track_of_matches_strategy_type(self):
        self.assertEqual(select_0_5.track_of({'strategy_type': 'breakout'}), 'breakout')

    def test_strategy_id_is_the_shortterm_model_selection_version(self):
        import shortterm_model
        self.assertEqual(select_0_5.STRATEGY_ID, shortterm_model.SELECTION_VERSION)


if __name__ == '__main__':
    unittest.main()
