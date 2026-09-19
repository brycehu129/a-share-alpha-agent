"""测试用的固定数字规格（已解析、不随 ATR 变化）。

执行器、合约模拟这些模块只读**冻结记录里已经解析好的规格**，它们的测试验证的是判定逻辑本身，
不该跟着 exec_spec 的 ATR 参数一起改。这里放一份旧的固定 2.5%/3% 止损、持有 3 天、14:00 截止的规格当夹具。
exec_spec 自己的数值行为由 test_conditional_exec.SpecTests 覆盖。"""
import copy

LEGACY_SPECS = {
    'breakout': {
        'entry': {'kind': 'confirm_up', 'min_pct': 0.005, 'max_pct': 0.03, 'void_pct': -0.03,
                  'void_below_ma20': False, 'window': ['09:30', '14:00']},
        'exit': {'stop_pct': 0.025, 'target_pct': 0.07, 'day1_close_rule': True, 'hold_sessions': 3,
                 'breakeven_arm_pct': 0.03, 'stop_floor': None}},
    'pullback': {
        'entry': {'kind': 'no_chase', 'min_pct': None, 'max_pct': 0.01, 'void_pct': -0.03,
                  'void_below_ma20': True, 'window': ['09:30', '14:00']},
        'exit': {'stop_pct': 0.03, 'target_pct': 0.05, 'day1_close_rule': False, 'hold_sessions': 3,
                 'breakeven_arm_pct': 0.03, 'stop_floor': 'ma20_at_fill'}},
}


def build_spec(track):
    if track not in LEGACY_SPECS:
        raise KeyError(track)
    spec = copy.deepcopy(LEGACY_SPECS[track])
    spec.update(spec_version='test-legacy', revision=0, track=track, atr_pct=None)
    return spec
