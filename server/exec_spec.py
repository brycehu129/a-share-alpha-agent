"""exec-0.2 的入场/退出规格，按 track 分化。纯数据 + 几个纯函数，没有 I/O。

每条冻结计划在生成时把**自己那份规格原样拷进记录里**（forecast['exec_spec']），
执行器只读记录里的这份，不读这里的常量。这样以后调整下面的数字，不会悄悄改变
已经冻结的计划的行为——和"预测记录不可改写"是同一个原则。改了规格就该 bump
EXECUTION_VERSION，新计划带新规格，旧计划照旧。

为什么按 track 分化，而不是沿用统一的 ±3% 对称入场带 / −3%+5% 止损止盈：

- 入场：突破候选次日低开 3% 说明突破失败，此时买入是接刀；回调候选低开反而可能是更好
  的买点。同一个对称带处理两种相反的情形，逻辑上说不通。所以突破要"向上确认"才买，
  回调"不追高"。
- 退出：按现有成本假设往返约 0.31%，统一 −3%/+5% 的盈亏平衡胜率是 41.4%。突破形态
  有延续性，止损收紧、止盈放宽（−2.5%/+7%）后降到 29.6%——同样的选股能力，赚钱概率
  完全不同。回调形态止盈空间本来就窄，硬拉到 +7% 大概率吃不到，保持 −3%/+5%，
  盈亏平衡胜率仍是 41.4%。**这一项本期没有改善**，留给证据回答。
"""
import copy

EXEC_MODE = 'conditional-intraday-v1'
SPEC_VERSION = 'exec-spec-1'
ROUND_TRIP_COST_PCT = 0.31   # 与 STRATEGY.md 成本假设一致：滑点0.1%×2 + 佣金0.03%×2 + 过户费 + 卖出税0.05%

SPECS = {
    'breakout': {
        # 现价在 [ref×1.005, ref×1.03] 内才买：低于下沿说明还没向上确认，高于上沿是追高。
        # 盘中跌到 ref×0.97 以下当日作废：突破已经失败。
        'entry': {'kind': 'confirm_up', 'min_pct': 0.005, 'max_pct': 0.03, 'void_pct': -0.03,
                  'void_below_ma20': False, 'window': ['09:30', '14:00']},
        'exit': {'stop_pct': 0.025, 'target_pct': 0.07,
                 # 入场后第一个收盘若收益 ≤0 就走：假突破要快跑。
                 'day1_close_rule': True, 'hold_sessions': 3,
                 # 浮盈达到 3% 后，止损上移到"真保本价"（含双边成本，见 conditional_exec）。
                 'breakeven_arm_pct': 0.03, 'stop_floor': None},
    },
    'pullback': {
        # 不追高：现价 ≤ ref×1.01 就买；跌破 ref×0.97 或跌破 MA20 当日作废（回调变成了破位）。
        'entry': {'kind': 'no_chase', 'min_pct': None, 'max_pct': 0.01, 'void_pct': -0.03,
                  'void_below_ma20': True, 'window': ['09:30', '14:00']},
        'exit': {'stop_pct': 0.03, 'target_pct': 0.05,
                 'day1_close_rule': False, 'hold_sessions': 3,
                 'breakeven_arm_pct': 0.03,
                 # 止损取 max(入场价×0.97, 入场时的 MA20)——MA20 在入场价的 3% 以内时更紧。
                 'stop_floor': 'ma20_at_fill'},
    },
}


def build_spec(track):
    """返回该 track 规格的深拷贝。未知 track 直接 KeyError——宁可停下，也不套一个默认规格。"""
    spec = copy.deepcopy(SPECS[track])
    spec['spec_version'] = SPEC_VERSION
    spec['track'] = track
    return spec


def entry_zone(entry_spec, reference_price):
    """(下沿, 上沿, 作废线)。下沿为 None 表示没有向上确认要求。"""
    lo = None if entry_spec['min_pct'] is None else reference_price * (1 + entry_spec['min_pct'])
    return lo, reference_price * (1 + entry_spec['max_pct']), reference_price * (1 + entry_spec['void_pct'])


def breakeven_win_rate(exit_spec, cost_pct=ROUND_TRIP_COST_PCT):
    """盈亏平衡胜率(%)：每次亏 stop+成本、每次赚 target−成本 时，胜率至少多少才不亏。
    用来防止以后改止损止盈时无意破坏盈亏比结构。"""
    loss = exit_spec['stop_pct'] * 100 + cost_pct
    gain = exit_spec['target_pct'] * 100 - cost_pct
    return loss / (loss + gain) * 100
