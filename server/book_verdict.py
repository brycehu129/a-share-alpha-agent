"""持仓与自选页每一行的「系统结论」：把哨兵规则的触发结果归并成一个动作。纯函数，没有网络。

结论**直接来自哨兵规则（sentinel_rules）算出的信号**，不另写一套判断——页面上看到的结论和企业微信里
收到的提醒因此永远一致。归并优先级：

  自选股  具备买入信号 (buy) > 暂不宜买入 (blocked，形态满足但买不了/不该买) > 暂无信号 (wait)
  持仓股  彻底卖出 (exit) > 可暂时卖出 (reduce) > 具备做T条件 (t) > 继续持有 (hold)

- 彻底卖出：触及系统止损位、放量跌破 20 日低点、逼近跌停，或「同时跌破 MA20 与 MA60 且已跌破成本价」
  （趋势走坏又已经亏损，不再等）。
- 可暂时卖出：触及系统止盈位、跌破 MA20 或 MA60、放量下跌，或同时跌破两条均线但仍在成本之上
  （趋势走坏，先落袋一部分）——先减仓，看后面怎么走。
- 具备做T条件：三层都满足——空间（今天有可卖老仓、日内振幅够大）、位置（日内区间高位先卖/低位买回）、
  环境（大盘、板块、个股资金流没有否决，且至少一个支持项）。细则见 sentinel_rules.t_evaluate。

这些是**规则给出的提示，不是投资建议**：所有阈值都来自策略里已有的口径，未经前瞻验证；系统不下单。
"""
import sentinel_rules as sr

EXIT_KINDS = ('stop_hit', 'low20_break_volume', 'near_limit_down')
REDUCE_KINDS = ('target_hit', 'ma20_break', 'ma60_break', 'volume_surge_down')
T_KINDS = ('t_sell_high', 't_buy_low')
INTRADAY_HINT_KINDS = ('intraday_support_reclaim', 'intraday_resistance_reject')

WATCH_LABEL = {'buy': '具备买入信号', 'blocked': '暂不宜买入', 'wait': '暂无买入信号', 'nodata': '数据不足'}
HOLDING_LABEL = {'exit': '建议彻底卖出', 'reduce': '可暂时卖出', 't': '具备做T条件', 'hold': '继续持有',
                 'nodata': '数据不足'}


def _active(signals):
    return {x['kind'].split('.', 1)[1]: x for x in signals if x['active']}


def watch_verdict(w, q, facts, limits, market_pause=None):
    """w：自选行；q：报价；facts：live_check.price_facts 的结果（空 = 日线不足）。"""
    if not facts:
        return {'action': 'nodata', 'label': WATCH_LABEL['nodata'],
                'reasons': ['本地日线不足 20 根，算不出均线与 20 日高点，无法判断买入形态']}
    setup = sr.buy_setup(q, facts)
    track = sr.TRACK_LABEL.get(setup['track'], '')
    blocker = sr.buy_blocker(limits, market_pause)
    if setup['passed']:
        reasons = ['%s %s' % g for g in setup['passed_gates']]
        if blocker:
            return {'action': 'blocked', 'label': WATCH_LABEL['blocked'], 'track': track,
                    'reasons': ['%s形态已满足，但%s' % (track, blocker)] + reasons}
        return {'action': 'buy', 'label': WATCH_LABEL['buy'], 'track': track, 'reasons': reasons}
    reasons = ['距%s形态还差：%s' % (track, '；'.join('%s %s' % g for g in setup['failed_gates']))]
    if blocker:
        reasons.append(blocker)
    return {'action': 'wait', 'label': WATCH_LABEL['wait'], 'track': track, 'reasons': reasons}


def holding_verdict(h, q, facts, limits, env_fn=None, day=None):
    """h：book_levels.enrich 之后的持仓行（带系统止损/止盈位与可做T底仓）。
    env_fn：做T 的环境（大盘/板块/资金流），只在价格位置满足时才会被调用（可能联网）。"""
    last = float(q['last'])
    day = day or sr.day_facts(q)
    t = sr.t_evaluate(h, q, day, env_fn, limits)
    active = _active(sr.holding_signals(h, q, facts, limits) + sr.intraday_reversal_signals(h, q, day)
                     + sr.t_signals(h, q, day, evaluation=t))
    details = lambda kinds: [active[k]['detail'] for k in kinds if k in active]

    both_ma = 'ma20_break' in active and 'ma60_break' in active
    if any(k in active for k in EXIT_KINDS) or (both_ma and 'below_cost' in active):
        action = 'exit'
        reasons = (['同时跌破 MA20 与 MA60，中短期趋势都已走坏，且已跌破成本价'] if both_ma else []) + details(EXIT_KINDS)
    elif any(k in active for k in REDUCE_KINDS):
        action, reasons = 'reduce', details(REDUCE_KINDS)
        if both_ma:
            reasons.insert(0, '同时跌破 MA20 与 MA60，趋势走坏；现价仍在成本价之上，可先落袋一部分')
    elif any(k in active for k in T_KINDS):
        action, reasons = 't', details(T_KINDS)
    else:
        action = 'hold'
        reasons = details(INTRADAY_HINT_KINDS) or ['未触发任何卖出或做T条件']
        reasons += _t_notes(t)
    if action in ('reduce', 't', 'hold'):
        for detail in details(INTRADAY_HINT_KINDS):
            if detail not in reasons:
                reasons.append(detail)
    if action != 'hold' and 'below_cost' in active:
        reasons.append(active['below_cost']['detail'])
    verdict = {'action': action, 'label': HOLDING_LABEL[action], 'reasons': reasons,
               'stop_price': h.get('stop_price'), 'target_price': h.get('target_price'),
               'to_stop_pct': round((last / h['stop_price'] - 1) * 100, 2) if h.get('stop_price') else None,
               'to_target_pct': round((h['target_price'] / last - 1) * 100, 2) if h.get('target_price') else None}
    caveats = []
    if t['unavailable'] and action in ('hold', 't'):
        caveats.append(t['unavailable'])
    if not facts:
        caveats.append('日线不足，均线类信号（跌破 MA20/MA60、放量破位）暂不可用')
    if (h.get('levels') or {}).get('nominal'):
        caveats.append('这只股票的波动率算不出来，止损/止盈位按典型波动（ATR 3.5%）估算')
    if t['env'] and action in ('hold', 't'):
        verdict['context'] = env_line(t['env'])
    if caveats:
        verdict['caveats'] = caveats
    return verdict


def env_line(env):
    """做T 环境的一行说明：只列取得到的项。"""
    parts = []
    if env.get('market') is not None:
        parts.append('大盘（%s）%+.2f%%' % (env['market_name'], env['market']))
    if env.get('sector') is not None:
        parts.append('板块（%s，抽样 %d 只中位）%+.2f%%' % (env['sector_name'], env['sector_n'], env['sector']))
    if env.get('flow'):
        parts.append('主力近30分钟 %s' % sr._money(env['flow']['main_30m']))
    return ' · '.join(parts)


def _t_notes(t):
    """位置已经到了高/低位、但环境不让做T时，告诉用户为什么——不然只会看到"没信号"而不知道差在哪。"""
    if not t['side'] or t['ok'] or t['unavailable']:
        return []
    where = '日内高位' if t['side'] == 'sell' else '日内低位'
    if t['veto']:
        return ['处于%s，但%s，暂不做T' % (where, t['veto'][0])]
    return ['处于%s，但环境上没有支持做T的信号（大盘/板块/资金流都不构成理由），暂不做T' % where]


def market_pause(history):
    """策略当前的大盘评分是否低于暂停线。读不到就返回 None（不据此拦截买入信号）。
    这只是给提示加一道闸，读取失败不该让页面或哨兵跟着失败。"""
    try:
        from dashboard_export import latest
        _, agent = latest(history, 'agent')
        screen = (agent or {}).get('screen') or {}
        score, line = screen.get('market_score'), screen.get('market_score_pause')
    except Exception:
        return None
    return None if score is None or line is None else score < line


# --- 页面上的「判断规则」说明 --------------------------------------------------------------
# 文字里的数字全部取自代码里真正在用的常量（sentinel_rules / shortterm_model / exec_spec），
# 所以改了阈值，页面说明自动跟着变，不会出现"页面写 3%、代码是 4%"。

def _pct(v, digits=0):
    return ('%.' + str(digits) + 'f%%') % v


def rules_doc():
    import exec_spec
    import t_context
    from shortterm_model import BREAKOUT, PULLBACK
    ex = exec_spec.SPECS['breakout']['exit']
    stop_lo, stop_hi = ex['stop_min_pct'] * 100, ex['stop_max_pct'] * 100
    atr_mult, r, cap = ex['stop_atr_mult'], ex['target_r'], ex['target_max_pct'] * 100
    nominal = exec_spec.NOMINAL_ATR_PCT * 100

    holding_groups = [
        {'action': 'exit', 'label': HOLDING_LABEL['exit'], 'when': '满足任意一条',
         'intro': '风险已经兑现，或趋势走坏且已经亏损：不再等，整笔卖出。',
         'rules': [
             {'text': '触及系统止损位：现价 ≤ 成本价 ×（1 − 止损幅度）。止损幅度 = 这只股票自己的 ATR14 波动率 × %s，'
                      '夹在 %s–%s 之间；波动率算不出来（日线不足）时按典型波动 %s 估算，并在页面标明。'
                      % (atr_mult, _pct(stop_lo), _pct(stop_hi), _pct(nominal, 1))},
             {'text': '放量跌破 20 日低点：现价低于近 20 日最低收盘价，且量比 ≥ %s。' % sr.VOLUME_RATIO_BREAK},
             {'text': '逼近跌停：距跌停价 ≤ %s。' % _pct(sr.NEAR_LIMIT_PCT)},
             {'text': '趋势走坏且已亏损：同时跌破 MA20 与 MA60，并且现价低于成本价。'},
         ]},
        {'action': 'reduce', 'label': HOLDING_LABEL['reduce'], 'when': '满足任意一条（且不满足上面任何一条）',
         'intro': '先卖一部分，留一部分看后面怎么走。',
         'rules': [
             {'text': '触及系统止盈位：现价 ≥ 成本价 ×（1 + 止盈幅度）。止盈幅度 = 止损幅度 × %s，封顶 %s。' % (r, _pct(cap))},
             {'text': '跌破 MA20，或跌破 MA60。'},
             {'text': '放量下跌：量比 ≥ %s，且当日跌幅 ≥ %s。' % (sr.VOLUME_RATIO_SURGE, _pct(abs(sr.SURGE_DROP_PCT)))},
             {'text': '同时跌破 MA20 与 MA60，但现价仍在成本价之上：趋势走坏，先落袋一部分。'},
         ]},
        {'action': 't', 'label': HOLDING_LABEL['t'], 'when': '三层全部满足（且不满足上面任何一条）',
         'intro': 'A 股 T+1：只能用今天之前买的老仓先卖、低位再买回，净持仓不变。这里只是位置提示，系统不知道你是否已执行。',
         'rules': [
             {'text': '① 空间：今天有可卖的老仓（当天买入的股当天不能卖），且日内振幅 ≥ %s（往返成本约 %s%%，振幅不够就没有空间）。'
                      % (_pct(sr.T_MIN_AMPLITUDE_PCT), exec_spec.ROUND_TRIP_COST_PCT)},
             {'text': '② 位置：现价在日内区间的 %s 以上（高抛，先卖）或 %s 以下（低吸，买回）。'
                      % (_pct(sr.T_SELL_POSITION * 100), _pct(sr.T_BUY_POSITION * 100))},
             {'text': '③ 环境：大盘、板块、个股资金流里没有「否决项」，并且至少有一个「支持项」。缺数据就写明「暂不判断」，不退回只看价格。',
              'sub': [
                  '高抛的否决项：距涨停 ≤ %s；大盘涨幅 ≥ %s 且板块 ≥ %s 且主力近 30 分钟仍净流入（顺势上涨，先卖容易卖飞；没有资金流数据时按保守处理）。'
                  % (_pct(sr.T_NEAR_LIMIT_PCT), _pct(sr.T_MARKET_STRONG), _pct(sr.T_SECTOR_HOT)),
                  '高抛的支持项（任一）：个股涨幅比板块多 %s 个点以上（个股冲高）；价格在高位但主力近 30 分钟净流出（量价背离）；大盘 ≤ %s（弱市冲高易回落）。'
                  % (sr.T_OUTPERFORM_PP, _pct(sr.T_MARKET_WEAK, 1)),
                  '低吸的否决项：距跌停 ≤ %s；大盘 ≤ %s（系统性杀跌）；板块 ≤ %s（板块杀跌）；全天主力净流出且近 30 分钟仍在流出。'
                  % (_pct(sr.T_NEAR_LIMIT_PCT), _pct(sr.T_MARKET_CRASH, 1), _pct(sr.T_SECTOR_DUMP)),
                  '低吸的支持项（任一）：大盘 > %s（没走弱）；板块 > %s（没跟着跌）；主力近 30 分钟净流入（有资金承接）。'
                  % (_pct(sr.T_MARKET_WEAK, 1), _pct(sr.T_SECTOR_FLAT, 1)),
                  '「大盘」= 上证与沪深300 的平均（创业板/科创板个股用创业板指）；「板块」= 同行业最多 %d 只股票当日涨跌幅的中位数（代理指标，不是官方板块指数）。'
                  % t_context.PEER_SAMPLE,
              ]},
         ]},
        {'action': 'hold', 'label': HOLDING_LABEL['hold'], 'when': '以上都不满足',
         'intro': '页面会写明现价离系统止损位、止盈位还有多远；价格位置已到做T的高/低位、但环境不允许时，会写明是哪一项否决。分时数据可用时，还会补充上穿支撑/跌回阻力下方这类盘中观察提示。',
         'rules': []},
    ]
    watch_groups = [
        {'action': 'buy', 'label': WATCH_LABEL['buy'], 'when': '突破或回调反弹任一形态的 3 个价格门槛全部通过，且没有被拦截',
         'intro': '用实时价，把选股策略里两条形态的价格门槛各判一遍。',
         'rules': [
             {'text': '突破形态：近 3 日涨幅 ≥ %s；现价 / 近 20 日最高收盘 ≥ 0.99（接近 20 日新高）；偏离 MA5 在 0–%s。'
                      % (_pct(BREAKOUT['return3_min']), _pct(BREAKOUT['ma5_deviation_max']))},
             {'text': '回调反弹形态：近 2 日涨跌 ≤ %s（没有在涨）；偏离 MA20 在 0–%s；当日收阳（现价高于今开）。'
                      % (_pct(PULLBACK['return2_max']), _pct(PULLBACK['deviation_max']))},
             {'text': '量比只作参考，不参与判定（腾讯量比盘中系统性偏小，和策略里按全日成交量算的量比口径不同）。'},
         ]},
        {'action': 'blocked', 'label': WATCH_LABEL['blocked'], 'when': '形态满足，但出现下列任一情况',
         'intro': '',
         'rules': [{'text': '已涨停：排不上队，买不进。'}, {'text': '已跌停：不是买点。'},
                   {'text': '策略的大盘趋势评分低于暂停线（默认 40）：策略此时暂停新增买入。'}]},
        {'action': 'wait', 'label': WATCH_LABEL['wait'], 'when': '两个形态都没有全部通过',
         'intro': '页面会写明离最接近的那个形态还差哪几项。', 'rules': []},
        {'action': 'nodata', 'label': WATCH_LABEL['nodata'], 'when': '本地日线不足 20 根',
         'intro': '算不出均线和 20 日高点，无法判断，不会当成「没信号」。', 'rules': []},
    ]
    return {
        'holding': {'title': '持仓：系统怎么判断卖出与做T', 'groups': holding_groups,
                    'notes': ['判断按优先级依次进行：彻底卖出 > 可暂时卖出 > 具备做T条件 > 继续持有，命中前一档就不再看后面的。',
                              '成本价 = 你买入时填的价格（多次买入取加权平均，不含手续费）；止损位、止盈位都从成本价算起。',
                              '跌破 MA20 / MA60、放量跌破 20 日低点需要本地日线；日线不足或疑似除权时这几项暂不可用，页面会注明。',
                              '页面上的结论和企业微信里的哨兵提醒用的是同一套规则，不会各说各话。']},
        'watch': {'title': '自选股：系统怎么判断买入信号', 'groups': watch_groups,
                  'notes': ['买入信号只看**价格形态**，不含选股层的行业强度、流动性和公告核查——所以「具备买入信号」不等于「进了候选池」。',
                            '买入只能从自选股发起；买入后这只股票仍留在自选里。']},
        'disclaimer': '以上阈值来自策略里已有的口径（止损/止盈、突破/回调）和经验默认值（做T 的大盘/板块/资金流阈值），'
                      '都没有经过前瞻验证，是规则给出的提示，不是投资建议；系统只提醒、不下单。',
    }
