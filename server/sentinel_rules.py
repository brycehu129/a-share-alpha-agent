"""自选股哨兵的触发规则。纯函数：输入报价与事实，输出 signal 列表；没有网络、没有 AI。

触发器必须是规则而不是 AI：省 token（AI 只在触发之后才调）、可回测、可审计。

**系统不知道你为什么买，就推导不出什么时候该卖。** 所以卖出/止损类信号只在你自己声明了
止损价/目标价时才触发；没填就不触发，绝不替你猜一个默认值。`hold_type` 是你对这笔
持仓的自我声明，这里只用它来**决定该推哪些提醒**（例如"套牢待解"的持仓，低于成本价是
常态、不该推，"回到成本价"才是事件；"长期"持仓不推 MA20 破位这类噪音），不据此发明
任何买卖规则。

信号的 key 里带股票代码和类型，引擎据此做边沿/冷却去重；带 carry 的信号是"持续状态"
（已跌破成本价、已跌破MA20），跨日继承，避免每天早上把同一个状态当成新事件再推一遍。
"""
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
# 你自己设的价位（成本、止损、目标）是最关心的位置，价格常常在它附近来回磨蹭。默认 5 分钟的重新武装
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
    return facts


def sig(key, symbol, kind, active, detail, severity='normal', carry=False, **kw):
    return {'key': '%s:%s' % (key, symbol), 'symbol': symbol, 'kind': 'sentinel.' + kind,
            'active': bool(active), 'detail': detail, 'severity': severity, 'carry': carry, **kw}


def holding_signals(h, q, facts, limits):
    """真实持仓的信号。h 是账本行，q 是新鲜报价，facts 是 live_check.price_facts 的结果。"""
    s, name = h['symbol'], h.get('name') or h['symbol']
    last, cost = float(q['last']), float(h['cost_price'])
    hold_type = h.get('hold_type')
    pct = (last / cost - 1) * 100
    out = []

    if hold_type == 'trapped':
        out.append(sig('back-to-cost', s, 'back_to_cost', last >= cost,
                       '%s 回到成本价 %.2f 上方（现价 %.2f，%+.2f%%）——你标记的是"套牢待解"' % (name, cost, last, pct),
                       carry=True, rearm_s=LEVEL_REARM_S))
    else:
        out.append(sig('below-cost', s, 'below_cost', last < cost,
                       '%s 跌破成本价 %.2f（现价 %.2f，浮亏 %.2f%%）' % (name, cost, last, -pct), carry=True,
                       rearm_s=LEVEL_REARM_S))

    stop, target = h.get('stop_price'), h.get('target_price')
    if stop:
        out.append(sig('stop', s, 'stop_hit', last <= stop,
                       '%s 触及你设的止损价 %.2f（现价 %.2f）' % (name, stop, last),
                       severity='urgent', carry=True, rearm_s=LEVEL_REARM_S,
                       level={'price': float(stop), 'direction': 'down'}))
    if target:
        out.append(sig('target', s, 'target_hit', last >= target,
                       '%s 触及你设的目标价 %.2f（现价 %.2f）' % (name, target, last),
                       carry=True, rearm_s=LEVEL_REARM_S, level={'price': float(target), 'direction': 'up'}))

    if facts.get('ma20') and hold_type != 'long':
        out.append(sig('ma20-break', s, 'ma20_break', last < facts['ma20'],
                       '%s 跌破 MA20 %.2f（现价 %.2f）' % (name, facts['ma20'], last), carry=True))
    if facts.get('ma60'):
        out.append(sig('ma60-break', s, 'ma60_break', last < facts['ma60'],
                       '%s 跌破 MA60 %.2f（现价 %.2f）' % (name, facts['ma60'], last), carry=True))

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
                       '%s 逼近涨停：距涨停 %.2f%%' % (name, limits['to_limit_up_pct'])))
    out.append(high20_signal(s, name, last, ratio, facts))
    return [x for x in out if x]


def watch_signals(w, q, facts, limits):
    """自选股（非持仓）的信号。"""
    s, name = w['symbol'], w.get('name') or w['symbol']
    last, out = float(q['last']), []
    lo, hi = w.get('buy_low'), w.get('buy_high')
    if lo and hi:      # 用户没填买入区间就不触发这一类，不猜
        out.append(sig('buy-zone', s, 'buy_zone', lo <= last <= hi,
                       '%s 进入你设的买入区间 %.2f–%.2f（现价 %.2f）' % (name, lo, hi, last),
                       level={'price': float(hi), 'direction': 'down'}))
    ratio, change = _f(q.get('volume_ratio')), _f(q.get('change_pct'))
    out.append(sig('vol-surge', s, 'volume_surge',
                   ratio is not None and ratio >= VOLUME_RATIO_SURGE and change is not None and abs(change) >= 3,
                   '%s 量比突增：量比 %s，涨跌 %.2f%%' % (name, ratio, change or 0), rearm_s=1800))
    if limits.get('to_limit_up_pct') is not None:
        out.append(sig('near-limit-up', s, 'near_limit_up', limits['to_limit_up_pct'] <= NEAR_LIMIT_PCT,
                       '%s 逼近涨停：距涨停 %.2f%%' % (name, limits['to_limit_up_pct'])))
    out.append(high20_signal(s, name, last, ratio, facts))
    return [x for x in out if x]


def high20_signal(symbol, name, last, ratio, facts):
    if not facts.get('high20_close'):
        return None
    return sig('high20-break', symbol, 'high20_break_volume',
               ratio is not None and ratio >= VOLUME_RATIO_BREAK and last > facts['high20_close'],
               '%s 放量突破 20 日高点 %.2f（现价 %.2f，量比 %s）' % (name, facts['high20_close'], last, ratio))


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


def t_signals(h, q, day):
    """做T的高抛/低吸位提示。**只是位置提示**：系统不知道你是否已执行，不跟踪、不闭环。

    A股 T+1：当日买入不能当日卖出，真正的日内 T 只能"先卖后买"——用底仓在高位卖出、
    低位买回，净持仓不变。所以前提是你声明了底仓（t_base_shares>0），并且当日振幅
    ≥3%（往返成本约0.31%，振幅不足没有空间）。任一不满足就不产生任何提示。"""
    base = h.get('t_base_shares') or 0
    s, name = h['symbol'], h.get('name') or h['symbol']
    amp, pos, vwap = day.get('amplitude_pct'), day.get('position_in_range'), day.get('vwap')
    last = float(q['last'])
    ready = bool(base) and amp is not None and amp >= T_MIN_AMPLITUDE_PCT and pos is not None and vwap
    note = '（底仓 %d 股，先卖后买；仅位置提示，系统不知道你是否已执行）' % base
    return [
        sig('t-sell-high', s, 't_sell_high', ready and pos >= T_SELL_POSITION and last > vwap,
            '%s 处于日内高位：区间位置 %.0f%%，高于均价线 %.2f（振幅 %.1f%%）%s' % (
                name, (pos or 0) * 100, vwap or 0, amp or 0, note), rearm_s=T_REARM_S),
        sig('t-buy-low', s, 't_buy_low', ready and pos <= T_BUY_POSITION and last < vwap,
            '%s 处于日内低位：区间位置 %.0f%%，低于均价线 %.2f（振幅 %.1f%%）%s' % (
                name, (pos or 0) * 100, vwap or 0, amp or 0, note), rearm_s=T_REARM_S),
    ]
