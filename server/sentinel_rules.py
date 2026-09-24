"""自选股/持仓哨兵的触发规则。纯函数：输入报价与事实，输出 signal 列表；没有网络、没有 AI。

触发器必须是规则而不是 AI：省 token（AI 只在触发之后才调）、可回测、可审计。

**买卖判断由系统给出，不由用户声明。** 持仓的止损位/止盈位是系统按策略（ATR，见 book_levels.py）从
成本价算出来的，做T 底仓是今天可卖的老仓；自选股的买入信号复用策略两条 track（突破/回调反弹）的
实时门槛（live_check.breakout_gates / pullback_gates）。本模块不读任何用户"意愿"字段——持有类型、
止损价、目标价、做T底仓、买入区间都已取消录入。

信号的 key 里带股票代码和类型，引擎据此做边沿/冷却去重；带 carry 的信号是"持续状态"
（已跌破成本价、已跌破MA20），跨日继承，避免每天早上把同一个状态当成新事件再推一遍。
"""
import exec_spec
import intraday_formula
import live_check

NODES = ('0945', '1305', '1430')
NODE_LABEL = {'0945': '开盘方向确立', '1305': '午后开盘', '1430': '尾盘决策'}
NODE_MAX_LATE_S = 300           # 节点已经迟到超过 5 分钟就不再推：那时的"开盘方向"已不是开盘方向

VOLUME_RATIO_BREAK = 1.5        # 放量突破/跌破 20 日高低点所需的量比（腾讯口径，盘中偏小，只作提示）
VOLUME_RATIO_SURGE = 2.5
SURGE_DROP_PCT = -2.0
NEAR_LIMIT_PCT = 1.0

T_MIN_AMPLITUDE_PCT = 3.0       # 往返成本约 0.31%，日内振幅不足这个数就没有做T的空间
T_SELL_POSITION = 0.8           # 现价处在当日区间的 80% 以上 → 高抛位
T_BUY_POSITION = 0.2            # 20% 以下 → 低吸位
T_REARM_S = 900
# 做T 的环境阈值（%）。都是经验默认值，没有经过回测；集中放在这里，页面的「判断规则」也读这里。
T_MARKET_STRONG = 1.0           # 大盘涨幅≥1%：强势日
T_MARKET_WEAK = -0.5            # 大盘 ≤ -0.5%：偏弱
T_MARKET_CRASH = -1.5           # 大盘 ≤ -1.5%：系统性杀跌
T_SECTOR_HOT = 1.0              # 板块中位涨幅≥1%：板块走强
T_SECTOR_DUMP = -2.0            # 板块中位涨幅≤-2%：板块杀跌
T_SECTOR_FLAT = -0.5            # 板块没有明显走弱的下限
T_OUTPERFORM_PP = 1.5           # 个股比板块多涨这么多个百分点：个股脉冲
T_NEAR_LIMIT_PCT = 3.0          # 距涨停/跌停不足 3%：单边行情，不做T
INTRADAY_SIGNAL_REARM_S = 900   # 分时支撑/阻力穿越属于观察级提示，15 分钟内不重复刷屏
# 成本价、系统止损位、止盈位是最关心的位置，价格常常在它附近来回磨蹭。默认 5 分钟的重新武装
# 间隔下，茅台在止损位 1250 附近的一天里触发了 4 次紧急推送——都是真实的穿越，但已经是刷屏。
# 这类状态型信号放宽到 30 分钟：真的又跌回去了，半小时后你会再收到；磨蹭期间不会。
LEVEL_REARM_S = 1800


def _f(v):
    return float(v) if v not in (None, '') else None


def day_facts(quote, minute=None):
    """当日区间与均价线。极值取实时行情自带的 high/low（真实日内极值），分钟数据只提供
    VWAP——分钟数据只有分钟收盘价，其 high_close/low_close 会比真实极值窄。"""
    last, prev = _f(quote.get('last')), _f(quote.get('previous_close'))
    high, low = _f(quote.get('high')), _f(quote.get('low'))
    if minute:
        high = max(high, minute['high_close']) if high else minute['high_close']
        low = min(low, minute['low_close']) if low else minute['low_close']
    facts = {'day_high': high, 'day_low': low, 'vwap': minute['vwap'] if minute else None}
    if high and low and prev:
        facts['amplitude_pct'] = round((high - low) / prev * 100, 4)
        facts['position_in_range'] = round((last - low) / (high - low), 4) if high > low else None
    if facts['vwap']:
        facts['vs_vwap_pct'] = round((last / facts['vwap'] - 1) * 100, 4)
    if minute:
        facts.update(intraday_formula.intraday_facts(quote, minute))
    return facts


def sig(key, symbol, kind, active, detail, severity='normal', carry=False, **kw):
    return {'key': '%s:%s' % (key, symbol), 'symbol': symbol, 'kind': 'sentinel.' + kind,
            'active': bool(active), 'detail': detail, 'severity': severity, 'carry': carry, **kw}


def holding_signals(h, q, facts, limits):
    """真实持仓的信号。h 是账本行经 book_levels.enrich 补上系统止损/止盈位之后的样子，
    q 是新鲜报价，facts 是 live_check.price_facts 的结果。"""
    s, name = h['symbol'], h.get('name') or h['symbol']
    last, cost = float(q['last']), float(h['cost_price'])
    pct = (last / cost - 1) * 100
    out = [sig('below-cost', s, 'below_cost', last < cost,
               '%s 跌破成本价 %.2f（现价 %.2f，浮亏 %.2f%%）' % (name, cost, last, -pct), carry=True,
               severity='watch', rearm_s=LEVEL_REARM_S)]

    stop, target = h.get('stop_price'), h.get('target_price')
    levels = h.get('levels') or {}
    basis = '按波动率(ATR)计算' if not levels.get('nominal') else '按典型波动估算'
    if stop:
        # 止损位现在是三条（成本/保本/移动）里最紧的一条（book_levels.effective_stop），来源见
        # h['stop_source']。触及移动止损单独给一个 kind（sentinel.trail_stop_hit）——它和贴着
        # 成本价的止损是两件不同的事："越赚钱越紧"这件事本身就值得在留档里能单独筛出来看。
        source = h.get('stop_source', 'cost')
        kind = 'trail_stop_hit' if source == 'trail' else 'stop_hit'
        trail_atr = levels.get('atr_pct') or exec_spec.NOMINAL_ATR_PCT
        basis_text = {'cost': '成本 %.2f 下方 %.1f%%，%s' % (cost, levels.get('stop_pct', 0) * 100, basis),
                     'breakeven': '保本止损（浮盈曾达标后启动，价位已扣真实双边费用）',
                     'trail': '移动止损（跟随历史最高价 %.2f，回撤 %.1f%%）' % (
                         h.get('peak_price') or 0, exec_spec.TRAIL_ATR_MULT * trail_atr * 100)}[source]
        out.append(sig('stop', s, kind, last <= stop,
                       '%s 触及系统止损位 %.2f（%s；现价 %.2f）' % (name, stop, basis_text, last),
                       severity='urgent', carry=True, rearm_s=LEVEL_REARM_S,
                       level={'price': float(stop), 'direction': 'down'}))
    if target:
        out.append(sig('target', s, 'target_hit', last >= target,
                       '%s 触及系统止盈位 %.2f（成本 %.2f 上方 %.1f%%，%s；现价 %.2f）' % (
                           name, target, cost, levels.get('target_pct', 0) * 100, basis, last),
                       carry=True, rearm_s=LEVEL_REARM_S, level={'price': float(target), 'direction': 'up'}))

    if facts.get('ma20'):
        # carry 状态信号，统一用 LEVEL_REARM_S（30分钟）：默认 300s 的话价格贴着均线磨蹭
        # 一天能推 40+ 次，和止损位当初放宽到 1800s 是同一个理由（见 LEVEL_REARM_S 的注释）。
        out.append(sig('ma20-break', s, 'ma20_break', last < facts['ma20'],
                       '%s 跌破 MA20 %.2f（现价 %.2f）' % (name, facts['ma20'], last), carry=True, rearm_s=LEVEL_REARM_S))
    if facts.get('ma60'):
        out.append(sig('ma60-break', s, 'ma60_break', last < facts['ma60'],
                       '%s 跌破 MA60 %.2f（现价 %.2f）' % (name, facts['ma60'], last), carry=True, rearm_s=LEVEL_REARM_S))

    ratio, change = _f(q.get('volume_ratio')), _f(q.get('change_pct'))
    out.append(sig('vol-surge-down', s, 'volume_surge_down',
                   ratio is not None and ratio >= VOLUME_RATIO_SURGE and change is not None and change <= SURGE_DROP_PCT,
                   '%s 放量下跌：量比 %s，涨跌 %.2f%%' % (name, ratio, change or 0), severity='urgent', rearm_s=1800))
    if facts.get('low20_close'):
        out.append(sig('low20-break', s, 'low20_break_volume',
                       ratio is not None and ratio >= VOLUME_RATIO_BREAK and last < facts['low20_close'],
                       '%s 放量跌破 20 日低点 %.2f（现价 %.2f，量比 %s）' % (name, facts['low20_close'], last, ratio),
                       severity='urgent'))
    if limits.get('to_limit_down_pct') is not None:
        out.append(sig('near-limit-down', s, 'near_limit_down', limits['to_limit_down_pct'] <= NEAR_LIMIT_PCT,
                       '%s 逼近跌停：距跌停 %.2f%%' % (name, limits['to_limit_down_pct']), severity='urgent'))
    if limits.get('to_limit_up_pct') is not None:
        out.append(sig('near-limit-up', s, 'near_limit_up', limits['to_limit_up_pct'] <= NEAR_LIMIT_PCT,
                       '%s 逼近涨停：距涨停 %.2f%%' % (name, limits['to_limit_up_pct']), severity='watch'))
    out.append(high20_signal(s, name, last, ratio, facts))
    return [x for x in out if x]


def intraday_reversal_signals(h, q, day):
    """分时辅助信号：只做盘中观察，不替代主策略的止损/止盈规则。"""
    if not day or not day.get('support') or not day.get('resistance'):
        return []
    s, name = h['symbol'], h.get('name') or h['symbol']
    last = float(q['last'])
    macd_state = day.get('macd_state') or ''
    bullish = macd_state.startswith('bullish')
    bearish = macd_state.startswith('bearish')
    support = day['support']
    resistance = day['resistance']
    support_note = '可留意低吸回补观察' if h.get('t_base_shares') else '可留意分时企稳观察'
    sell_note = '可留意盘中减仓观察'
    return [
        sig('intraday-support-reclaim', s, 'intraday_support_reclaim',
            bool(day.get('buy_cross_support')) and bullish,
            '%s 上穿盘中支撑 %.2f（现价 %.2f，MACD 转多），%s' % (name, support, last, support_note),
            severity='watch', rearm_s=INTRADAY_SIGNAL_REARM_S),
        sig('intraday-resistance-reject', s, 'intraday_resistance_reject',
            bool(day.get('sell_cross_resistance')) and bearish,
            '%s 跌回盘中阻力 %.2f 下方（现价 %.2f，MACD 转弱），%s' % (name, resistance, last, sell_note),
            severity='watch', rearm_s=INTRADAY_SIGNAL_REARM_S),
    ]


TRACK_LABEL = {'breakout': '突破', 'pullback': '回调反弹'}
GATES_PER_TRACK = 3       # 两条 track 的实时价格门槛各有 3 个"判定项"（量比只作参考，不算）


def buy_setup(q, facts):
    """自选股的买入形态：用实时价把策略两条 track 的价格门槛各判一遍。

    返回 {'track', 'passed', 'passed_gates', 'failed_gates'}。passed 表示某条 track 的 3 个判定项**全部**通过；
    没通过时 track 是"离通过最近"的那条，failed_gates 给出还差什么。日线不足以算均线时 track 为 None——
    缺数据就说缺数据，不当作"没信号"。"""
    if not facts:
        return {'track': None, 'passed': False, 'passed_gates': [], 'failed_gates': []}
    results = {}
    for track, gates in (('breakout', live_check.breakout_gates(facts, q)),
                         ('pullback', live_check.pullback_gates(facts, q))):
        decisive = {n: (ok, d) for n, (ok, d) in gates.items() if ok is not None}
        results[track] = {'complete': len(decisive) == GATES_PER_TRACK,
                          'passed': [(n, d) for n, (ok, d) in decisive.items() if ok],
                          'failed': [(n, d) for n, (ok, d) in decisive.items() if not ok]}
    for track, r in results.items():
        if r['complete'] and not r['failed']:
            return {'track': track, 'passed': True, 'passed_gates': r['passed'], 'failed_gates': []}
    best = max(results, key=lambda t: (len(results[t]['passed']), t == 'pullback'))
    return {'track': best, 'passed': False, 'passed_gates': results[best]['passed'],
            'failed_gates': results[best]['failed']}


def buy_blocker(limits, market_pause):
    """形态满足也买不了/不该买的原因；没有就返回空串。"""
    if limits.get('at_limit_up'):
        return '已涨停，排不上队，买不进'
    if limits.get('at_limit_down'):
        return '已跌停，不是买点'
    if market_pause:
        return '大盘趋势评分低于暂停线，策略暂停新增买入'
    return ''


def watch_signals(w, q, facts, limits, market_pause=None):
    """自选股（非持仓）的信号。market_pause：策略的大盘评分是否低于暂停线（None = 读不到，不据此拦截）。"""
    s, name = w['symbol'], w.get('name') or w['symbol']
    last, out = float(q['last']), []
    setup = buy_setup(q, facts)
    if setup['track']:
        detail = '%s 具备买入信号（%s形态）：%s' % (
            name, TRACK_LABEL[setup['track']], '；'.join('%s %s' % g for g in setup['passed_gates']))
        active = setup['passed'] and not buy_blocker(limits, market_pause)
        out.append(sig('buy-signal', s, 'buy_signal', active, detail, rearm_s=LEVEL_REARM_S))
    ratio, change = _f(q.get('volume_ratio')), _f(q.get('change_pct'))
    out.append(sig('vol-surge', s, 'volume_surge',
                   ratio is not None and ratio >= VOLUME_RATIO_SURGE and change is not None and abs(change) >= 3,
                   '%s 量比突增：量比 %s，涨跌 %.2f%%' % (name, ratio, change or 0), severity='watch', rearm_s=1800))
    if limits.get('to_limit_up_pct') is not None:
        out.append(sig('near-limit-up', s, 'near_limit_up', limits['to_limit_up_pct'] <= NEAR_LIMIT_PCT,
                       '%s 逼近涨停：距涨停 %.2f%%' % (name, limits['to_limit_up_pct']), severity='watch'))
    out.append(high20_signal(s, name, last, ratio, facts))
    return [x for x in out if x]


def high20_signal(symbol, name, last, ratio, facts):
    if not facts.get('high20_close'):
        return None
    return sig('high20-break', symbol, 'high20_break_volume',
               ratio is not None and ratio >= VOLUME_RATIO_BREAK and last > facts['high20_close'],
               '%s 放量突破 20 日高点 %.2f（现价 %.2f，量比 %s）' % (name, facts['high20_close'], last, ratio),
               severity='watch')


def node_signals(symbol, name, node_due):
    """固定时间节点。node_due(hhmm) 返回迟到秒数或 None（引擎提供）。
    只在节点被跨过的那一轮发出，迟到太多就不发。"""
    out = []
    for hhmm in NODES:
        late = node_due(hhmm)
        if late is not None and late <= NODE_MAX_LATE_S:
            out.append(sig('node:' + hhmm, symbol, 'node_' + hhmm, True,
                           '%s %s:%s %s' % (name, hhmm[:2], hhmm[2:], NODE_LABEL[hhmm])))
    return out


def _t_sell_env(q, env, limits):
    """高抛位的环境：(支持理由, 否决理由)。缺数据的项直接跳过，不当成"平淡"。"""
    market, sector, flow = env.get('market'), env.get('sector'), env.get('flow')
    change = _f(q.get('change_pct'))
    support, veto = [], []
    up = limits.get('to_limit_up_pct')
    if up is not None and up <= T_NEAR_LIMIT_PCT:
        veto.append('距涨停仅 %.1f%%，强势单边行情，先卖容易卖飞' % up)
    if (market is not None and sector is not None and market >= T_MARKET_STRONG and sector >= T_SECTOR_HOT
            and (flow is None or flow['main_30m'] > 0)):
        veto.append('大盘（%+.2f%%）与板块（%+.2f%%）同步走强%s，顺势上行，不宜先卖' % (
            market, sector, '，主力仍在净流入' if flow else '（无资金流数据，按保守处理）'))
    if sector is not None and change is not None and change - sector >= T_OUTPERFORM_PP:
        support.append('个股涨 %+.2f%%、板块中位 %+.2f%%，比板块多涨 %.1f 个点，属个股冲高' % (change, sector, change - sector))
    if flow is not None and flow['main_30m'] < 0:
        support.append('价格在高位而近 30 分钟主力净流出 %s，量价背离' % _money(flow['main_30m']))
    if market is not None and market <= T_MARKET_WEAK:
        support.append('大盘偏弱（%+.2f%%），弱市里的冲高更容易回落' % market)
    return support, veto


def _t_buy_env(q, env, limits):
    market, sector, flow = env.get('market'), env.get('sector'), env.get('flow')
    support, veto = [], []
    down = limits.get('to_limit_down_pct')
    if down is not None and down <= T_NEAR_LIMIT_PCT:
        veto.append('距跌停仅 %.1f%%，单边下跌，不是买回点' % down)
    if market is not None and market <= T_MARKET_CRASH:
        veto.append('大盘大跌（%+.2f%%），系统性下跌，别急着买回' % market)
    if sector is not None and sector <= T_SECTOR_DUMP:
        veto.append('板块整体杀跌（中位 %+.2f%%），不是个股的日内波动' % sector)
    if flow is not None and flow['main'] < 0 and flow['main_30m'] < 0:
        veto.append('全天主力净流出 %s，近 30 分钟仍在流出，资金没有承接' % _money(flow['main']))
    if market is not None and market > T_MARKET_WEAK:
        support.append('大盘没有走弱（%+.2f%%），这次下探更像个股的日内波动' % market)
    if sector is not None and sector > T_SECTOR_FLAT:
        support.append('板块没有跟着跌（中位 %+.2f%%）' % sector)
    if flow is not None and flow['main_30m'] > 0:
        support.append('近 30 分钟主力净流入 %s，有资金承接' % _money(flow['main_30m']))
    return support, veto


def _money(v):
    return ('%+.2f亿' % (v / 1e8)) if abs(v) >= 1e8 else ('%+.0f万' % (v / 1e4))


def t_evaluate(h, q, day, env_fn=None, limits=None):
    """做T 的三层判断。**只是位置提示**：系统不知道你是否已执行，不跟踪、不闭环。

    1. 空间：今天有可卖的老仓（T+1：当天买入的卖不出去，只能先卖老仓、低位再买回），且日内振幅 ≥3%
       （往返成本约 0.31%，振幅不足没有空间）。
    2. 位置：现价在日内区间的 80% 以上（高抛）或 20% 以下（低吸）。
    3. 环境：大盘、板块、个股资金流。有**否决项**就不做（顺势上涨不先卖、系统性杀跌不接飞刀）；
       没有否决项时还要至少一个**支持项**（个股冲高、量价背离、弱市冲高 / 大盘与板块没走弱、资金承接）。
    环境数据只在 1、2 都满足时才通过 env_fn 懒取；取不到就明说"缺数据，暂不判断"，不退回只看价格。

    返回 {'side','ok','support','veto','env','unavailable','base','amplitude','position'}。"""
    base = h.get('t_base_shares') or 0
    amp, pos = day.get('amplitude_pct'), day.get('position_in_range')
    out = {'base': base, 'side': None, 'ok': False, 'support': [], 'veto': [], 'env': None, 'unavailable': '',
           'amplitude': amp, 'position': pos, 'vwap': day.get('vwap')}
    if not (base and amp is not None and amp >= T_MIN_AMPLITUDE_PCT and pos is not None):
        return out
    out['side'] = 'sell' if pos >= T_SELL_POSITION else 'buy' if pos <= T_BUY_POSITION else None
    if out['side'] is None:
        return out
    env = env_fn() if env_fn else None
    if not env or all(env.get(k) is None for k in ('market', 'sector', 'flow')):
        out['unavailable'] = '大盘、板块、资金流数据都取不到，做T暂不判断'
        return out
    limits = limits if limits is not None else live_check.limit_facts(q)
    support, veto = (_t_sell_env if out['side'] == 'sell' else _t_buy_env)(q, env, limits)
    out.update(env=env, support=support, veto=veto, ok=bool(support) and not veto)
    return out


def t_signals(h, q, day, evaluation=None, env_fn=None):
    """做T 信号。evaluation 可以传入已经算好的 t_evaluate 结果（页面要同时拿到理由，避免重复联网）。"""
    ev = evaluation if evaluation is not None else t_evaluate(h, q, day, env_fn)
    s, name = h['symbol'], h.get('name') or h['symbol']
    pos, amp, vwap = ev['position'], ev['amplitude'], ev['vwap']
    note = '（可卖老仓 %d 股，先卖后买；仅位置提示，系统不知道你是否已执行）' % ev['base']
    line = '区间位置 %.0f%%%s（振幅 %.1f%%）' % ((pos or 0) * 100, '，均价线 %.2f' % vwap if vwap else '', amp or 0)
    why = '｜'.join(ev['support'])
    return [
        sig('t-sell-high', s, 't_sell_high', ev['ok'] and ev['side'] == 'sell',
            '%s 处于日内高位：%s；%s%s' % (name, line, why, note), rearm_s=T_REARM_S),
        sig('t-buy-low', s, 't_buy_low', ev['ok'] and ev['side'] == 'buy',
            '%s 处于日内低位：%s；%s%s' % (name, line, why, note), rearm_s=T_REARM_S),
    ]
