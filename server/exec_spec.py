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


# 允许被"提议 + 人确认"调整的参数白名单。**不在这里的一律不可提议**——包括所有风控红线
# （本金、最大持仓数、单只上限、单笔风险、回撤暂停线、成本假设）和结构性字段
# （入场类型、时段、day1 规则、止损底）。白名单之外的名字由代码拒收，不靠提示词约束。
# floor/ceiling 是绝对边界，step_cap 是单次最大改动幅度，和 review_pipeline.TUNABLE 同一套思路。
TUNABLE_PARAMS = {
    'entry.max_pct':          {'floor': 0.005, 'ceiling': 0.05,  'step_cap': 0.005, 'label': '追高上限（相对参考价）'},
    'entry.min_pct':          {'floor': 0.0,   'ceiling': 0.02,  'step_cap': 0.0025, 'label': '向上确认下沿（相对参考价，仅突破）'},
    'entry.void_pct':         {'floor': -0.06, 'ceiling': -0.01, 'step_cap': 0.005, 'label': '入场作废线（相对参考价）'},
    'exit.stop_pct':          {'floor': 0.015, 'ceiling': 0.04,  'step_cap': 0.005, 'label': '止损幅度'},
    'exit.target_pct':        {'floor': 0.03,  'ceiling': 0.10,  'step_cap': 0.01,  'label': '止盈幅度'},
    'exit.breakeven_arm_pct': {'floor': 0.02,  'ceiling': 0.05,  'step_cap': 0.005, 'label': '保本止损启动浮盈'},
    'exit.hold_sessions':     {'floor': 1,     'ceiling': 5,     'step_cap': 1,     'label': '最长持有交易日', 'integer': True},
}

# 明确列出来，只是为了给出"这是红线"的专门提示；即使不在这个列表里，不在白名单的也照样拒收。
RED_LINES = {'capital', 'max_positions', 'max_weight', 'risk_per_trade', 'drawdown_pause',
             'drawdown_hard_stop', 'slippage', 'commission', 'minimum_commission', 'transfer_fee',
             'sell_tax', 'label_cost_pct', 'coverage_required'}

BASE_EXECUTION_VERSION = 'exec-0.2'


def execution_version(revision=0):
    """修订号 0 就是 exec-0.2 本身；每批准一次参数改动 +1，样本按这个字符串分组，
    所以改了参数后新样本从零开始，不会和改之前的混池。"""
    return BASE_EXECUTION_VERSION if not revision else '%s.r%d' % (BASE_EXECUTION_VERSION, revision)


def get_param(spec, name):
    section, key = name.split('.')
    return spec[section][key]


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
    if not x['target_pct'] > x['stop_pct']:
        problems.append('止盈幅度必须大于止损幅度')
    if not x['breakeven_arm_pct'] < x['target_pct']:
        problems.append('保本启动浮盈必须小于止盈幅度')
    if x['breakeven_arm_pct'] <= ROUND_TRIP_COST_PCT / 100:
        problems.append('保本启动浮盈必须大于往返成本')
    return problems


def apply_overrides(spec, overrides):
    """把 {'exit.stop_pct': 0.02, ...} 套到规格上（就地修改传入的拷贝）。"""
    for name, value in (overrides or {}).items():
        if name not in TUNABLE_PARAMS:
            raise KeyError('不在白名单内的参数: ' + name)
        section, key = name.split('.')
        spec[section][key] = value
    return spec


def build_spec(track, overrides=None, revision=0):
    """返回该 track 规格的深拷贝。未知 track 直接 KeyError——宁可停下，也不套一个默认规格。

    overrides/revision 来自已批准的提议（见 proposals.py）；不传就是 exec-0.2 原始规格。
    本模块不读任何文件：由程序入口把它们传进来，测试因此不受服务器上已批准提议的影响。"""
    spec = copy.deepcopy(SPECS[track])
    apply_overrides(spec, overrides)
    spec['spec_version'] = SPEC_VERSION
    spec['revision'] = revision
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
