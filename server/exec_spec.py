"""exec-0.3 的入场/退出规格，按 track 分化，止损/止盈/追高上限按**每只股票自己的波动率（ATR）**定。
纯数据 + 几个纯函数，没有 I/O。

**为什么改成 ATR。** exec-0.2 用固定 −2.5% 止损。用本地 35 个交易日的历史数据回放（日线推演，见 STRATEGY.md）：
无论抽哪批股票，−2.5% 止损在 3 天内被触发的比例都是 **58%–66%**——它离日内噪声太近，一半以上的单子不是"看错了"
而是"被震出去了"。这类放量股日均振幅本来就有 3%–5%。所以止损宽度改成 `ATR14% × 倍数`（夹在 3%–8% 之间），
每只股按自己的波动定，止盈 = 止损 × 盈亏比 R，最长持有 5 个交易日（"不要持仓过久"）。

每条冻结计划在生成时把**自己那份已经解析好的规格原样拷进记录里**（forecast['exec_spec']，含解析出的
stop_pct/target_pct/max_pct/void_pct 和当时的 ATR），执行器只读记录里的这份，不读这里的常量。这样以后调整下面的数字，
不会悄悄改变已经冻结的计划的行为——和"预测记录不可改写"是同一个原则。改了规格就该 bump 版本号。

入场：
- 突破要"向上确认"才买（+0.5% 起），追高上限 = ATR 的 0.8 倍夹在 3%–5%（波动大的票允许多一点空间，否则
  它一开盘就"高于上限"永远进不来）；作废线 = 跌到参考价下方一个止损幅度。
- 回调"不追高"（≤ +1%）；作废线同上，且跌破 MA20 也作废（回调变成了破位）。
- 当日 09:30–14:30 有效（原来 14:00）；日线只能整体估计，实际触发以盘中观测为准。

盈亏平衡胜率会随 R 变：R=1.5、典型 ATR 下约 42%——比 exec-0.2 的突破 track（29.6%）高，这是为"少被震出去"付的代价，
不是免费午餐。
"""
import copy

EXEC_MODE = 'conditional-intraday-v1'
SPEC_VERSION = 'exec-spec-2'
ROUND_TRIP_COST_PCT = 0.31   # 与 STRATEGY.md 成本假设一致：滑点0.1%×2 + 佣金0.03%×2 + 过户费 + 卖出税0.05%
# 买卖两侧拆开：回本进度要知道现在卖出净剩多少，不能只有往返合计。
# 买入：滑点0.1% + 佣金0.03%（过户费0.001%记入四舍五入）；卖出：买入两项 + 印花税0.05%。
BUY_COST_PCT = 0.13
SELL_COST_PCT = ROUND_TRIP_COST_PCT - BUY_COST_PCT   # 0.18；两者之和必须等于 ROUND_TRIP_COST_PCT，有测试守住
BASE_EXECUTION_VERSION = 'exec-0.3'
NOMINAL_ATR_PCT = 0.035      # 没有具体股票时（提议页预览、测试）用的典型 ATR14%
ATR_CLAMP = (0.015, 0.08)    # 解析时对 ATR 本身也夹一下：数据异常不能生成离谱的止损

# 真实持仓的移动止损倍数（book_levels.effective_stop 用）。**不在 TUNABLE_PARAMS 白名单里**：
# 那份白名单是候选池策略的执行规格，走"提议→确认"、按 execution_version 冻结分组；这个只管
# 你自己账本里的持仓，性质和 sentinel_rules.py 里的经验阈值（T_MARKET_STRONG 等）一样——
# 起始默认值，没有回测，调整不需要走提议流程。
TRAIL_ATR_MULT = 2.0

TRADED_TRACKS = ('breakout', 'pullback')

SPECS = {
    'breakout': {
        'entry': {'kind': 'confirm_up', 'min_pct': 0.005, 'max_pct_floor': 0.03, 'max_atr_mult': 0.8, 'max_cap_pct': 0.05,
                  'max_pct': None, 'void_pct': None, 'void_below_ma20': False, 'window': ['09:30', '14:30']},
        'exit': {'stop_atr_mult': 1.5, 'stop_min_pct': 0.03, 'stop_max_pct': 0.08,
                 'target_r': 1.5, 'target_max_pct': 0.12,
                 'stop_pct': None, 'target_pct': None,
                 # 入场后第一个收盘若收益 ≤0 就走：假突破要快跑。
                 'day1_close_rule': True, 'hold_sessions': 5,
                 # 浮盈达到"止盈幅度的一半"后，止损上移到真保本价（含双边成本，见 conditional_exec）。
                 'breakeven_arm_frac': 0.5, 'breakeven_arm_pct': None, 'stop_floor': None},
    },
    'pullback': {
        'entry': {'kind': 'no_chase', 'min_pct': None, 'max_pct': 0.01, 'void_pct': None,
                  'void_below_ma20': True, 'window': ['09:30', '14:30']},
        # 回调不再用"MA20 当止损底"：ATR 止损已经比 MA20 更远时，取较高者会把止损又拉回到噪声里。
        'exit': {'stop_atr_mult': 1.5, 'stop_min_pct': 0.03, 'stop_max_pct': 0.08,
                 'target_r': 1.5, 'target_max_pct': 0.12,
                 'stop_pct': None, 'target_pct': None,
                 'day1_close_rule': False, 'hold_sessions': 5,
                 'breakeven_arm_frac': 0.5, 'breakeven_arm_pct': None, 'stop_floor': None},
    },
    # select-rev-0.1（超跌反弹，只留档不成交，见 server/strategies/reversal_0_1.py）用的规格。
    # 不追高，和 pullback 同形；但不设 void_below_ma20——这条 track 的候选按定义已经在 MA20
    # 下方（超跌），拿"跌破MA20"当作废条件没有意义。这套规格目前只喂合约模拟/展示用的
    # 止损止盈位，不进任何真实成交路径（TRADABLE=False）。
    'reversal': {
        'entry': {'kind': 'no_chase', 'min_pct': None, 'max_pct': 0.01, 'void_pct': None,
                  'void_below_ma20': False, 'window': ['09:30', '14:30']},
        'exit': {'stop_atr_mult': 1.5, 'stop_min_pct': 0.03, 'stop_max_pct': 0.08,
                 'target_r': 1.5, 'target_max_pct': 0.12,
                 'stop_pct': None, 'target_pct': None,
                 'day1_close_rule': False, 'hold_sessions': 5,
                 'breakeven_arm_frac': 0.5, 'breakeven_arm_pct': None, 'stop_floor': None},
    },
}


# 允许被"提议 + 人确认"调整的参数白名单。**不在这里的一律不可提议**——包括所有风控红线
# （本金、最大持仓数、单只上限、单笔风险、回撤暂停线、成本假设）和结构性字段
# （入场类型、时段、day1 规则）。白名单之外的名字由代码拒收，不靠提示词约束。
# floor/ceiling 是绝对边界，step_cap 是单次最大改动幅度，和 review_pipeline.TUNABLE 同一套思路。
# tracks：该参数适用于哪些 track（不适用的提议会被拒收，而不是悄悄改一个不起作用的值）。
TUNABLE_PARAMS = {
    'entry.min_pct':          {'floor': 0.0,   'ceiling': 0.02,  'step_cap': 0.0025, 'label': '向上确认下沿（相对参考价）', 'tracks': ('breakout',)},
    'entry.max_atr_mult':     {'floor': 0.3,   'ceiling': 1.5,   'step_cap': 0.1,    'label': '追高上限 = ATR × 该倍数', 'tracks': ('breakout',)},
    'entry.max_cap_pct':      {'floor': 0.03,  'ceiling': 0.08,  'step_cap': 0.005,  'label': '追高上限的封顶（相对参考价）', 'tracks': ('breakout',)},
    'entry.max_pct':          {'floor': 0.0,   'ceiling': 0.03,  'step_cap': 0.0025, 'label': '不追高上限（相对参考价）', 'tracks': ('pullback',)},
    'exit.stop_atr_mult':     {'floor': 1.0,   'ceiling': 3.0,   'step_cap': 0.25,   'label': '止损 = ATR × 该倍数', 'tracks': TRADED_TRACKS},
    'exit.stop_min_pct':      {'floor': 0.02,  'ceiling': 0.05,  'step_cap': 0.005,  'label': '止损幅度下限', 'tracks': TRADED_TRACKS},
    'exit.stop_max_pct':      {'floor': 0.05,  'ceiling': 0.10,  'step_cap': 0.005,  'label': '止损幅度上限', 'tracks': TRADED_TRACKS},
    'exit.target_r':          {'floor': 1.0,   'ceiling': 3.0,   'step_cap': 0.25,   'label': '止盈 = 止损 × 盈亏比 R', 'tracks': TRADED_TRACKS},
    'exit.breakeven_arm_frac': {'floor': 0.3,  'ceiling': 0.8,   'step_cap': 0.1,    'label': '保本止损启动点（止盈幅度的比例）', 'tracks': TRADED_TRACKS},
    'exit.hold_sessions':     {'floor': 2,     'ceiling': 7,     'step_cap': 1,      'label': '最长持有交易日', 'integer': True, 'tracks': TRADED_TRACKS},
}

# 明确列出来，只是为了给出"这是红线"的专门提示；即使不在这个列表里，不在白名单的也照样拒收。
RED_LINES = {'capital', 'max_positions', 'max_weight', 'risk_per_trade', 'drawdown_pause',
             'drawdown_hard_stop', 'slippage', 'commission', 'minimum_commission', 'transfer_fee',
             'sell_tax', 'label_cost_pct', 'coverage_required'}


def execution_version(revision=0):
    """修订号 0 就是 exec-0.3 本身；每批准一次参数改动 +1，样本按这个字符串分组，
    所以改了参数后新样本从零开始，不会和改之前的混池。"""
    return BASE_EXECUTION_VERSION if not revision else '%s.r%d' % (BASE_EXECUTION_VERSION, revision)


def get_param(spec, name):
    section, key = name.split('.')
    return spec[section].get(key)


def check_spec(spec):
    """调整之后的规格必须仍然自洽；返回问题列表（空 = 通过）。"""
    problems = []
    e, x = spec['entry'], spec['exit']
    if e['min_pct'] is not None and not e['min_pct'] < e['max_pct']:
        problems.append('向上确认下沿必须小于追高上限')
    if not e['void_pct'] < 0:
        problems.append('作废线必须在参考价下方')
    if e['min_pct'] is not None and e['void_pct'] >= e['min_pct']:
        problems.append('作废线必须低于确认下沿')
    if not x['stop_min_pct'] <= x['stop_max_pct']:
        problems.append('止损幅度下限不能大于上限')
    if not x['target_pct'] > x['stop_pct']:
        problems.append('止盈幅度必须大于止损幅度')
    if x['target_r'] < 1.0:
        problems.append('盈亏比 R 不能小于 1')
    if not x['breakeven_arm_pct'] < x['target_pct']:
        problems.append('保本启动浮盈必须小于止盈幅度')
    if x['breakeven_arm_pct'] <= ROUND_TRIP_COST_PCT / 100:
        problems.append('保本启动浮盈必须大于往返成本')
    return problems


def apply_overrides(spec, overrides):
    """把 {'exit.stop_atr_mult': 1.75, ...} 套到规格模板上（就地修改传入的拷贝）。必须在 resolve 之前调用。"""
    for name, value in (overrides or {}).items():
        if name not in TUNABLE_PARAMS:
            raise KeyError('不在白名单内的参数: ' + name)
        section, key = name.split('.')
        spec[section][key] = value
    return spec


def resolve_levels(spec, atr_pct):
    """把模板里的倍数解析成这只股票的具体数字。就地修改并返回 spec。

    atr_pct 是小数（0.035 = 3.5%）。数据异常（None/非正/非有限）时用典型值并在 spec 里标 nominal——
    但 alpha_engine 对没有 ATR 的候选会直接不出计划，这里的兜底只服务于预览和测试。"""
    e, x = spec['entry'], spec['exit']
    nominal = not (isinstance(atr_pct, (int, float)) and atr_pct == atr_pct and atr_pct > 0)
    atr = NOMINAL_ATR_PCT if nominal else min(ATR_CLAMP[1], max(ATR_CLAMP[0], float(atr_pct)))
    stop = min(x['stop_max_pct'], max(x['stop_min_pct'], x['stop_atr_mult'] * atr))
    target = min(x['target_max_pct'], x['target_r'] * stop)
    x['stop_pct'] = round(stop, 4)
    x['target_pct'] = round(target, 4)
    x['breakeven_arm_pct'] = round(max(0.02, x['breakeven_arm_frac'] * target), 4)
    if e['kind'] == 'confirm_up':
        e['max_pct'] = round(min(e['max_cap_pct'], max(e['max_pct_floor'], e['max_atr_mult'] * atr)), 4)
    e['void_pct'] = -round(stop, 4)
    spec['atr_pct'] = None if nominal else round(atr, 4)
    spec['levels'] = 'nominal' if nominal else 'atr'
    return spec


def build_spec(track, overrides=None, revision=0, atr_pct=None):
    """返回该 track 规格的深拷贝，并按 atr_pct 解析成具体数字。未知 track 直接 KeyError——宁可停下，也不套一个默认规格。

    overrides/revision 来自已批准的提议（见 proposals.py）；不传就是 exec-0.3 原始规格。
    本模块不读任何文件：由程序入口把它们传进来，测试因此不受服务器上已批准提议的影响。"""
    spec = copy.deepcopy(SPECS[track])
    apply_overrides(spec, overrides)
    resolve_levels(spec, atr_pct)
    spec['spec_version'] = SPEC_VERSION
    spec['revision'] = revision
    spec['track'] = track
    return spec


def neutral_exit(atr_pct, overrides=None):
    """随机基线/证据对照用的"不分 track"出场规格：同样的 ATR 止损/盈亏比/最长持有，没有 day1 规则、没有保本以外的特殊项。
    策略前 10 名和随机抽样都套它，才是同一把尺子。"""
    spec = build_spec('breakout', overrides, 0, atr_pct)
    exit_spec = spec['exit']
    exit_spec['day1_close_rule'] = False
    return exit_spec


def entry_zone(entry_spec, reference_price):
    """(下沿, 上沿, 作废线)。下沿为 None 表示没有向上确认要求。要求 spec 已解析。"""
    lo = None if entry_spec['min_pct'] is None else reference_price * (1 + entry_spec['min_pct'])
    return lo, reference_price * (1 + entry_spec['max_pct']), reference_price * (1 + entry_spec['void_pct'])


def breakeven_win_rate(exit_spec, cost_pct=ROUND_TRIP_COST_PCT):
    """盈亏平衡胜率(%)：每次亏 stop+成本、每次赚 target−成本 时，胜率至少多少才不亏。
    用来防止以后改止损止盈时无意破坏盈亏比结构。"""
    loss = exit_spec['stop_pct'] * 100 + cost_pct
    gain = exit_spec['target_pct'] * 100 - cost_pct
    return loss / (loss + gain) * 100


def sizing(exit_spec, policy):
    """这条计划的仓位到底怎么定：仓位 = min(单只上限, 单笔风险预算 ÷ 止损幅度)。哪个更紧，哪个说了算。

    返回 {'position_pct', 'max_loss_pct', 'binding'}，单位都是占净值的百分数。max_loss_pct 只含价格亏损（不含成本）。"""
    weight_cap = policy['max_weight'] * 100
    risk_cap = policy['risk_per_trade'] * 100 / (exit_spec['stop_pct'] * 100) * 100
    position = min(weight_cap, risk_cap)
    return {'position_pct': round(position, 2), 'max_loss_pct': round(position * exit_spec['stop_pct'], 3),
            'binding': 'weight_cap' if weight_cap <= risk_cap else 'risk_budget',
            'weight_cap_pct': round(weight_cap, 2), 'risk_cap_pct': round(risk_cap, 2)}
